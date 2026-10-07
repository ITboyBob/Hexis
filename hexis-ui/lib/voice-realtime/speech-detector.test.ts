import { SpeechDetector, type DetectorWorkerPort, type SpeechDetectorMessage } from "./speech-detector";
import * as ort from "onnxruntime-web/wasm";
import assetManifest from "./vad-assets-manifest.json";
import { createModelAdapter, createSileroV6Adapter } from "./speech-detector.worker";

class FakeWorker implements DetectorWorkerPort {
  onmessage: ((event: MessageEvent<SpeechDetectorMessage>) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  sent: unknown[] = [];
  terminated = false;

  postMessage(message: unknown): void {
    this.sent.push(message);
  }

  terminate(): void {
    this.terminated = true;
  }

  emit(message: SpeechDetectorMessage): void {
    this.onmessage?.({ data: message } as MessageEvent<SpeechDetectorMessage>);
  }
}

function readyMessage(generation = 0): SpeechDetectorMessage {
  return {
    type: "ready",
    sessionId: "session-1",
    mediaSessionId: "media-1",
    captureInstanceId: "capture-1",
    generation,
    model: "silero_v6",
  };
}

function serverReady(generation = 0, bargeInSupported = true) {
  return {
    sessionId: "session-1",
    mediaSessionId: "media-1",
    captureInstanceId: "capture-1",
    generation,
    bargeInSupported,
  };
}

describe("SpeechDetector dual-ready lifecycle", () => {
  it("does not allow capture until worker and matching server are both ready", () => {
    const worker = new FakeWorker();
    const detector = new SpeechDetector({
      sessionId: "session-1",
      mediaSessionId: "media-1",
      captureInstanceId: "capture-1",
      workerFactory: () => worker,
    });

    detector.start();
    worker.emit(readyMessage());
    expect(detector.canOpenCapture).toBe(false);
    expect(detector.noteServerReady(serverReady())).toBe(true);
    expect(detector.canOpenCapture).toBe(true);
  });

  it("accepts server ready first and rejects a mismatched identity or missing capability", () => {
    const mismatchWorker = new FakeWorker();
    const mismatch = new SpeechDetector({
      sessionId: "session-1",
      mediaSessionId: "media-1",
      captureInstanceId: "capture-1",
      workerFactory: () => mismatchWorker,
    });
    mismatch.start();
    expect(mismatch.noteServerReady({ ...serverReady(), mediaSessionId: "other-media" })).toBe(false);
    expect(mismatch.failure?.code).toBe("server_identity_mismatch");

    const unsupportedWorker = new FakeWorker();
    const unsupported = new SpeechDetector({
      sessionId: "session-1",
      mediaSessionId: "media-1",
      captureInstanceId: "capture-1",
      workerFactory: () => unsupportedWorker,
    });
    unsupported.start();
    expect(unsupported.noteServerReady(serverReady(0, false))).toBe(false);
    expect(unsupported.failure?.code).toBe("barge_in_capability_unavailable");

    const worker = new FakeWorker();
    const detector = new SpeechDetector({
      sessionId: "session-1",
      mediaSessionId: "media-1",
      captureInstanceId: "capture-1",
      workerFactory: () => worker,
    });
    detector.start();
    expect(detector.noteServerReady(serverReady())).toBe(true);
    expect(detector.canOpenCapture).toBe(false);
    worker.emit(readyMessage());
    expect(detector.canOpenCapture).toBe(true);
  });

  it("requires a matching server resume and worker reset acknowledgement for a new generation", async () => {
    const worker = new FakeWorker();
    const detector = new SpeechDetector({
      sessionId: "session-1",
      mediaSessionId: "media-1",
      captureInstanceId: "capture-1",
      workerFactory: () => worker,
    });
    detector.start();
    worker.emit(readyMessage());
    detector.noteServerReady(serverReady());
    expect(detector.canOpenCapture).toBe(true);

    const reset = detector.reset(1, "control-1");
    expect(detector.canOpenCapture).toBe(false);
    worker.emit({ type: "reset_ack", captureInstanceId: "capture-1", generation: 0, controlId: "control-1" });
    expect(detector.canOpenCapture).toBe(false);
    worker.emit({ type: "reset_ack", captureInstanceId: "capture-1", generation: 1, controlId: "control-1" });
    await reset;
    expect(detector.canOpenCapture).toBe(false);
    expect(detector.noteServerReady(serverReady(1))).toBe(true);
    expect(detector.canOpenCapture).toBe(true);
  });

  it("does not revive a closed detector when a late ready arrives", () => {
    const worker = new FakeWorker();
    const detector = new SpeechDetector({
      sessionId: "session-1",
      mediaSessionId: "media-1",
      captureInstanceId: "capture-1",
      workerFactory: () => worker,
    });
    detector.start();
    detector.close();
    worker.emit(readyMessage());
    expect(detector.canOpenCapture).toBe(false);
    expect(worker.terminated).toBe(true);
  });

  it("turns worker errors into a closed, diagnosable component state", () => {
    const worker = new FakeWorker();
    const detector = new SpeechDetector({
      sessionId: "session-1",
      mediaSessionId: "media-1",
      captureInstanceId: "capture-1",
      workerFactory: () => worker,
    });
    detector.start();
    worker.emit({ type: "error", captureInstanceId: "capture-1", generation: 0, code: "model_load_failed", message: "model unavailable" });
    expect(detector.canOpenCapture).toBe(false);
    expect(detector.failure).toMatchObject({ code: "model_load_failed", message: "model unavailable" });
  });
});

describe("Silero v6 tensor adapter", () => {
  class FakeTensor {
    disposed = false;
    constructor(
      readonly type: string,
      readonly data: Float32Array | BigInt64Array,
      readonly dims: number[],
    ) {}
    dispose(): void { this.disposed = true; }
  }

  function fakeSession() {
    const outputs: Array<{ output: FakeTensor; stateN: FakeTensor }> = [];
    const session = {
      inputNames: ["input", "state", "sr"],
      outputNames: ["output", "stateN"],
      run: vi.fn<(feeds: Record<string, FakeTensor>) => Promise<{ output: FakeTensor; stateN: FakeTensor }>>(async () => {
        const value = {
          output: new FakeTensor("float32", new Float32Array([0.75]), [1, 1]),
          stateN: new FakeTensor("float32", new Float32Array(256).fill(1), [2, 1, 128]),
        };
        outputs.push(value);
        return value;
      }),
      release: vi.fn(async () => undefined),
    };
    return { session, outputs };
  }

  it("loads the manifest model with the paired WASM factory instead of the runtime bundle", async () => {
    const previousFlags = {
      numThreads: ort.env.wasm.numThreads,
      proxy: ort.env.wasm.proxy,
      wasmPaths: ort.env.wasm.wasmPaths,
    };
    const { session } = fakeSession();
    const create = vi.spyOn(ort.InferenceSession, "create").mockResolvedValue(session as unknown as ort.InferenceSession);
    const fetchModel = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(new Uint8Array([1, 2, 3])));
    let adapter: Awaited<ReturnType<typeof createModelAdapter>> | undefined;
    try {
      adapter = await createModelAdapter();
      const factory = assetManifest.assets.find((asset) => asset.id === "ort-wasm-simd-threaded.mjs")!;
      const binary = assetManifest.assets.find((asset) => asset.id === "ort-wasm-simd-threaded.wasm")!;
      const model = assetManifest.assets.find((asset) => asset.id === assetManifest.primary_model)!;
      expect(factory.package_version).toBe(binary.package_version);
      expect(ort.env.wasm.wasmPaths).toEqual({
        mjs: new URL(factory.url_path, self.location.origin).href,
        wasm: new URL(binary.url_path, self.location.origin).href,
      });
      expect(ort.env.wasm.numThreads).toBe(1);
      expect(ort.env.wasm.proxy).toBe(false);
      expect(fetchModel).toHaveBeenCalledWith(new URL(model.url_path, self.location.origin), { cache: "force-cache" });
      expect(create).toHaveBeenCalledWith(expect.any(ArrayBuffer), { executionProviders: ["wasm"] });
    } finally {
      await adapter?.release();
      create.mockRestore();
      fetchModel.mockRestore();
      Object.assign(ort.env.wasm, previousFlags);
    }
  });

  it("preserves Int16 samples and applies the exact model tensor shape and scaling", async () => {
    const created: FakeTensor[] = [];
    class TrackingTensor extends FakeTensor {
      constructor(type: string, data: Float32Array | BigInt64Array, dims: number[]) {
        super(type, data, dims);
        created.push(this);
      }
    }
    const runtime = { Tensor: TrackingTensor } as unknown as typeof import("onnxruntime-web/wasm");
    const { session } = fakeSession();
    const adapter = createSileroV6Adapter(runtime, session as unknown as import("onnxruntime-web/wasm").InferenceSession);
    const samples = new Int16Array(512).fill(16384);
    samples[0] = -32768;
    samples[1] = 32767;
    const probability = await adapter.processFrame(samples);
    const feeds = session.run.mock.calls[0][0] as Record<string, FakeTensor>;

    expect(probability).toBe(0.75);
    expect(samples[0]).toBe(-32768);
    expect(samples[1]).toBe(32767);
    expect(feeds.input.dims).toEqual([1, 576]);
    expect(feeds.input.data[64]).toBe(-1);
    expect(feeds.input.data[65]).toBeCloseTo(32767 / 32768);
    expect(feeds.input.data[66]).toBe(0.5);
    expect(feeds.state.dims).toEqual([2, 1, 128]);
    expect(feeds.sr.type).toBe("int64");
    expect(feeds.sr.data).toEqual(new BigInt64Array([BigInt(16000)]));
    expect(created.some((tensor) => tensor.type === "int64")).toBe(true);
  });

  it("retains 64-sample context between frames and resets model state explicitly", async () => {
    const runtime = { Tensor: FakeTensor } as unknown as typeof import("onnxruntime-web/wasm");
    const { session } = fakeSession();
    const adapter = createSileroV6Adapter(runtime, session as unknown as import("onnxruntime-web/wasm").InferenceSession);
    await adapter.processFrame(new Int16Array(512).fill(8192));
    await adapter.processFrame(new Int16Array(512).fill(-8192));
    const secondFeeds = session.run.mock.calls[1][0] as Record<string, FakeTensor>;
    expect(secondFeeds.input.data[0]).toBe(0.25);
    expect(secondFeeds.input.data[63]).toBe(0.25);
    adapter.reset();
    await adapter.processFrame(new Int16Array(512));
    const thirdFeeds = session.run.mock.calls[2][0] as Record<string, FakeTensor>;
    expect(thirdFeeds.input.data.slice(0, 64)).toEqual(new Float32Array(64));
    expect(thirdFeeds.state.data).toEqual(new Float32Array(256));
    await adapter.release();
    expect(session.release).toHaveBeenCalledOnce();
  });

  it("rejects malformed model outputs instead of continuing with corrupt state", async () => {
    const runtime = { Tensor: FakeTensor } as unknown as typeof import("onnxruntime-web/wasm");
    const { session } = fakeSession();
    session.run.mockImplementationOnce(async () => ({
      output: new FakeTensor("float32", new Float32Array([1.2]), [1, 1]),
      stateN: new FakeTensor("float32", new Float32Array(256), [2, 1, 128]),
    }));
    const adapter = createSileroV6Adapter(runtime, session as unknown as import("onnxruntime-web/wasm").InferenceSession);
    await expect(adapter.processFrame(new Int16Array(511))).rejects.toThrow(/512/);
    await expect(adapter.processFrame(new Int16Array(512))).rejects.toThrow(/probability/);
  });
});
