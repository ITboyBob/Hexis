import * as ort from "onnxruntime-web/wasm";
import config from "./speech-detection-config.json";
import assetManifest from "./vad-assets-manifest.json";
import type { DetectorWorkerCommand, SpeechDetectorMessage } from "./speech-detector";

const STATE_DIMS = [2, 1, 128] as const;
const CONTEXT_SAMPLES = config.audio.model_context_samples;
const FRAME_SAMPLES = config.audio.model_frame_samples;

type SpeechDetectorWorkerScope = {
  postMessage: (message: SpeechDetectorMessage) => void;
  addEventListener: (type: "message", listener: (event: MessageEvent<DetectorWorkerCommand>) => void) => void;
  close: () => void;
  importScripts: (...urls: string[]) => void;
};

function matchesDims(actual: readonly number[], expected: readonly number[]): boolean {
  return actual.length === expected.length && actual.every((size, index) => size === expected[index]);
}

export type SileroV6Adapter = {
  processFrame: (samples: Int16Array) => Promise<number>;
  reset: () => void;
  release: () => Promise<void>;
};

export function createSileroV6Adapter(
  runtime: typeof ort,
  session: ort.InferenceSession,
): SileroV6Adapter {
  const requiredInputs = ["input", "state", "sr"];
  const requiredOutputs = ["output", "stateN"];
  if (requiredInputs.some((name) => !session.inputNames.includes(name))) {
    throw new Error("Silero v6 model is missing an expected input tensor.");
  }
  if (requiredOutputs.some((name) => !session.outputNames.includes(name))) {
    throw new Error("Silero v6 model is missing an expected output tensor.");
  }

  const createState = () => new runtime.Tensor("float32", new Float32Array(2 * 128), [...STATE_DIMS]);
  let state: ort.Tensor = createState();
  const sampleRate = new runtime.Tensor("int64", new BigInt64Array([BigInt(16000)]), [1]);
  let context = new Float32Array(CONTEXT_SAMPLES);
  let released = false;

  return {
    async processFrame(samples) {
      if (released) throw new Error("Silero v6 model has already been released.");
      if (!(samples instanceof Int16Array) || samples.length !== FRAME_SAMPLES) {
        throw new Error(`Silero v6 expects exactly ${FRAME_SAMPLES} raw Int16 samples per frame.`);
      }
      const modelFrame = new Float32Array(CONTEXT_SAMPLES + FRAME_SAMPLES);
      modelFrame.set(context, 0);
      for (let index = 0; index < FRAME_SAMPLES; index += 1) {
        modelFrame[CONTEXT_SAMPLES + index] = samples[index] / 32768;
      }
      context = modelFrame.slice(-CONTEXT_SAMPLES);
      const input = new runtime.Tensor("float32", modelFrame, [1, modelFrame.length]);
      let result: ort.InferenceSession.ReturnType;
      try {
        result = await session.run({ input, state, sr: sampleRate });
      } finally {
        input.dispose();
      }
      const nextState = result.stateN;
      const output = result.output;
      if (!nextState || !matchesDims(nextState.dims, STATE_DIMS)) {
        throw new Error("Silero v6 returned an invalid recurrent state tensor.");
      }
      if (!output || !output.data || typeof output.data[0] !== "number") {
        throw new Error("Silero v6 returned no speech probability.");
      }
      const probability = output.data[0];
      if (!Number.isFinite(probability) || probability < 0 || probability > 1) {
        throw new Error("Silero v6 returned a probability outside the [0, 1] range.");
      }
      state.dispose();
      state = nextState;
      output.dispose();
      return probability;
    },

    reset() {
      if (released) throw new Error("Cannot reset a released Silero v6 model.");
      state.dispose();
      state = createState();
      context = new Float32Array(CONTEXT_SAMPLES);
    },

    async release() {
      if (released) return;
      released = true;
      state.dispose();
      sampleRate.dispose();
      await session.release();
    },
  };
}

function requiredAsset(id: string): (typeof assetManifest.assets)[number] {
  const asset = assetManifest.assets.find((candidate) => candidate.id === id);
  if (!asset) throw new Error(`Asset manifest is missing ${id}.`);
  return asset;
}

export async function createModelAdapter(): Promise<SileroV6Adapter> {
  const model = requiredAsset(assetManifest.primary_model);
  const wasmModule = requiredAsset("ort-wasm-simd-threaded.mjs");
  const wasmBinary = requiredAsset("ort-wasm-simd-threaded.wasm");
  const wasmModuleUrl = new URL(wasmModule.url_path, self.location.origin).href;
  const wasmBinaryUrl = new URL(wasmBinary.url_path, self.location.origin).href;
  ort.env.wasm.numThreads = config.worker.num_threads;
  ort.env.wasm.proxy = config.worker.proxy;
  ort.env.wasm.wasmPaths = { mjs: wasmModuleUrl, wasm: wasmBinaryUrl };
  const modelUrl = new URL(model.url_path, self.location.origin);
  const response = await fetch(modelUrl, { cache: "force-cache" });
  if (!response.ok) throw new Error(`Model request failed with HTTP ${response.status}.`);
  const modelBuffer = await response.arrayBuffer();
  const session = await ort.InferenceSession.create(modelBuffer, {
    executionProviders: [config.worker.execution_provider],
  });
  try {
    return createSileroV6Adapter(ort, session);
  } catch (error) {
    await session.release();
    throw error;
  }
}

function installWorkerProtocol(scope: SpeechDetectorWorkerScope): void {
  let sessionId: string | null = null;
  let mediaSessionId: string | null = null;
  let captureInstanceId: string | null = null;
  let generation = 0;
  let adapter: SileroV6Adapter | null = null;
  let initializationPromise: Promise<void> | null = null;
  let inferenceQueue: Promise<void> = Promise.resolve();
  let queuedAudioSamples = 0;
  let failed = false;
  let closed = false;

  const post = (message: SpeechDetectorMessage) => scope.postMessage(message);
  const fail = (code: string, error: unknown) => {
    if (failed || closed || !captureInstanceId) return;
    failed = true;
    generation += 1;
    post({
      type: "error",
      captureInstanceId,
      generation: generation - 1,
      code,
      message: error instanceof Error ? error.message : "Speech detector failed unexpectedly.",
    });
    inferenceQueue = inferenceQueue.then(async () => {
      await adapter?.release();
      adapter = null;
    }).catch(() => undefined);
  };

  const initialize = (message: Extract<DetectorWorkerCommand, { type: "initialize" }>) => {
    if (initializationPromise || adapter || captureInstanceId) {
      captureInstanceId = message.captureInstanceId;
      fail("duplicate_initialize", new Error("Speech detector Worker can initialize only once."));
      return;
    }
    sessionId = message.sessionId;
    mediaSessionId = message.mediaSessionId;
    captureInstanceId = message.captureInstanceId;
    generation = message.generation;
    initializationPromise = (async () => {
      let timeoutId: ReturnType<typeof setTimeout> | undefined;
      let timedOut = false;
      const timeout = new Promise<never>((_, reject) => {
        timeoutId = setTimeout(() => {
          timedOut = true;
          reject(new Error(`Speech detector model initialization exceeded ${config.worker.initialization_timeout_ms}ms.`));
        }, config.worker.initialization_timeout_ms);
      });
      const modelPromise = createModelAdapter();
      try {
        const loaded = await Promise.race([modelPromise, timeout]);
        if (timedOut || failed || closed) {
          await loaded.release();
          return;
        }
        adapter = loaded;
        post({
          type: "ready",
          sessionId: sessionId!,
          mediaSessionId: mediaSessionId!,
          captureInstanceId: captureInstanceId!,
          generation,
          model: assetManifest.primary_model,
        });
      } catch (error) {
        if (timedOut) {
          void modelPromise.then((loaded) => loaded.release()).catch(() => undefined);
        }
        fail(timedOut ? "model_initialization_timeout" : "model_load_failed", error);
      } finally {
        if (timeoutId !== undefined) clearTimeout(timeoutId);
      }
    })();
    void initializationPromise.catch(() => undefined);
  };

  const handleMessage = (message: DetectorWorkerCommand) => {
    if (message.type === "initialize") {
      initialize(message);
      return;
    }
    if (closed || failed || !captureInstanceId || message.captureInstanceId !== captureInstanceId) return;
    if (message.type === "frame") {
      if (message.generation !== generation || !adapter) return;
      if (
        !(message.samples instanceof Int16Array)
        || message.samples.length !== FRAME_SAMPLES
        || message.endSample - message.startSample !== FRAME_SAMPLES
      ) {
        fail("invalid_frame", new Error("Speech detector received an invalid raw PCM frame range."));
        return;
      }
      if (queuedAudioSamples + message.samples.length > (config.worker.maximum_queued_audio_ms * config.audio.sample_rate) / 1000) {
        fail("worker_queue_overflow", new Error(`Speech detector input queue exceeded ${config.worker.maximum_queued_audio_ms}ms.`));
        return;
      }
      queuedAudioSamples += message.samples.length;
      inferenceQueue = inferenceQueue.then(async () => {
        try {
          if (failed || closed || message.generation !== generation || !adapter) return;
          const probability = await adapter.processFrame(message.samples);
          if (failed || closed || message.generation !== generation) return;
          post({
            type: "frame_result",
            captureInstanceId: captureInstanceId!,
            generation: message.generation,
            frameId: message.frameId,
            startSample: message.startSample,
            endSample: message.endSample,
            probability,
          });
        } catch (error) {
          fail("inference_failed", error);
        } finally {
          queuedAudioSamples = Math.max(0, queuedAudioSamples - message.samples.length);
        }
      });
      return;
    }
    if (message.type === "reset") {
      if (message.generation <= generation) return;
      generation = message.generation;
      queuedAudioSamples = 0;
      inferenceQueue = inferenceQueue.then(() => {
        if (failed || closed || !adapter || !captureInstanceId) return;
        adapter.reset();
        post({ type: "reset_ack", captureInstanceId, generation, controlId: message.controlId });
      }).catch((error: unknown) => fail("reset_failed", error));
      return;
    }
    if (message.type === "close") {
      if (message.generation !== generation) return;
      closed = true;
      generation += 1;
      inferenceQueue = inferenceQueue.then(async () => {
        await adapter?.release();
        adapter = null;
        if (captureInstanceId) post({ type: "closed", captureInstanceId, generation: message.generation });
        scope.close();
      }).catch(() => scope.close());
    }
  };

  scope.addEventListener("message", (event: MessageEvent<DetectorWorkerCommand>) => handleMessage(event.data));
}

const candidate = globalThis as unknown as Partial<SpeechDetectorWorkerScope>;
if (typeof candidate.importScripts === "function" && typeof candidate.postMessage === "function") {
  installWorkerProtocol(candidate as SpeechDetectorWorkerScope);
}
