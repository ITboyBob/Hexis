import { afterEach, describe, expect, it, vi } from "vitest";
import type { DetectorWorkerCommand, DetectorWorkerPort, ReleasedPcmPrefix, SpeechDetectorMessage } from "./speech-detector";

import { AudioCaptureController, StreamingPcmResampler } from "./capture";
import { PcmWatermark, type PcmTarget } from "./speech-detector";

class FakeDetectorWorker implements DetectorWorkerPort {
  onmessage: ((event: MessageEvent<SpeechDetectorMessage>) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  sent: DetectorWorkerCommand[] = [];
  terminated = false;
  postMessage(message: DetectorWorkerCommand) { this.sent.push(message); }
  terminate() { this.terminated = true; }
  emit(message: SpeechDetectorMessage) { this.onmessage?.({ data: message } as MessageEvent<SpeechDetectorMessage>); }
}

class FakeAudioNode {
  connect = vi.fn();
  disconnect = vi.fn();
}

class FakeCaptureAudioContext extends EventTarget {
  readonly sampleRate = 48_000;
  readonly state = "running";
  readonly destination = {};
  readonly audioWorklet = { addModule: vi.fn(async () => undefined) };
  createMediaStreamSource = vi.fn(() => new FakeAudioNode());
  createGain = vi.fn(() => Object.assign(new FakeAudioNode(), { gain: { value: 1 } }));
  createScriptProcessor = vi.fn(() => Object.assign(new FakeAudioNode(), { onaudioprocess: null as ((event: AudioProcessingEvent) => void) | null }));
  resume = vi.fn(async () => undefined);
  close = vi.fn(async () => undefined);
}

class FakeCaptureWorkletNode extends FakeAudioNode {
  readonly port = { onmessage: null as ((event: MessageEvent<unknown>) => void) | null, close: vi.fn() };
  constructor(...args: unknown[]) { super(); void args; }
}

afterEach(() => vi.unstubAllGlobals());

function signal(length: number, sampleRate: number, frequency: number) {
  return Float32Array.from(
    { length },
    (_, index) => 0.5 * Math.sin((2 * Math.PI * frequency * index) / sampleRate),
  );
}

describe("StreamingPcmResampler", () => {
  it.each([44_100, 48_000])(
    "preserves output length and phase across arbitrary %i Hz chunks",
    (inputRate) => {
      const input = signal(inputRate + 16, inputRate, 440);
      const whole = new StreamingPcmResampler(inputRate, 16_000).push(input);
      const split = new StreamingPcmResampler(inputRate, 16_000);
      const chunks: Int16Array[] = [];
      for (let offset = 0; offset < input.length; offset += 997) {
        chunks.push(split.push(input.slice(offset, offset + 997)));
      }
      const streamed = Int16Array.from(chunks.flatMap((chunk) => [...chunk]));

      expect(streamed).toHaveLength(16_000);
      expect(whole).toHaveLength(streamed.length);
      expect(streamed).toEqual(whole);
      expect(Math.max(...streamed)).toBeGreaterThan(10_000);
      expect(Math.min(...streamed)).toBeLessThan(-10_000);
    },
  );

  it("does not emit frames after close and fails loudly on a full bounded queue", async () => {
    const resampler = new StreamingPcmResampler(48_000, 16_000);
    resampler.close();
    expect(() => resampler.push(new Float32Array([0.2]))).toThrow(/closed/i);

    const { BoundedAudioQueue } = await import("./capture");
    const queue = new BoundedAudioQueue<number>(1);
    queue.push(1);
    expect(() => queue.push(2)).toThrow(/full/i);
    expect(queue.shift()).toBe(1);
  });
});

describe("AudioCaptureController transport ordering", () => {
  it("does not send capture_open until both the WS server and Module Worker are ready", async () => {
    const worker = new FakeDetectorWorker();
    const openCapture = vi.fn();
    const track = {
      id: "track-ready",
      readyState: "live",
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      getSettings: vi.fn(() => ({})),
    } as unknown as MediaStreamTrack;
    const controller = new AudioCaptureController(
      {
        activeSessionId: "session-1",
        activeMediaSessionId: "media-1",
        activeBargeInCapability: { supported: true },
        openCapture,
        suspendCapture: async () => undefined,
        resumeCapture: async () => undefined,
        sendAudioChunk: () => undefined,
        endUtterance: () => undefined,
      },
      "capture-ready",
      () => undefined,
      undefined,
      undefined,
      () => worker,
    );
    vi.stubGlobal("AudioContext", FakeCaptureAudioContext);
    vi.stubGlobal("AudioWorkletNode", FakeCaptureWorkletNode);
    const starting = controller.start({ getAudioTracks: () => [track] } as unknown as MediaStream);
    await vi.waitFor(() => expect(worker.sent.some((command) => command.type === "initialize")).toBe(true));
    expect(openCapture).not.toHaveBeenCalled();
    worker.emit({
      type: "ready",
      sessionId: "session-1",
      mediaSessionId: "media-1",
      captureInstanceId: "capture-ready",
      generation: 0,
      model: "silero_v6",
    });
    await starting;
    expect(openCapture).toHaveBeenCalledOnce();
    await controller.close();
    expect(worker.terminated).toBe(true);
  });

  it("retains the fallback queue target chosen before the response changes", () => {
    const targetA = { response_id: "response-a", playback_id: "play-a", output_epoch: 1 };
    let currentTarget = targetA;
    const appendPcm = vi.fn();
    const controller = new AudioCaptureController(
      {
        openCapture: () => undefined, suspendCapture: async () => undefined,
        resumeCapture: async () => undefined, sendAudioChunk: () => undefined, endUtterance: () => undefined,
      },
      "capture-1", () => undefined, () => undefined, () => currentTarget,
    );
    type Block = { samples: Float32Array; startFrame: number; observedAtMs: number; captureGeneration: number; target: unknown };
    const internal = controller as unknown as {
      snapshotBlock: (block: Omit<Block, "captureGeneration" | "target">) => Block;
      fallbackQueue: { push: (block: Block) => void; shift: () => Block | undefined };
      detector: unknown; resampler: unknown; consumeBlock: (block: Block) => void;
    };
    internal.detector = { generation: 0, appendPcm };
    internal.resampler = { push: () => new Int16Array(512) };
    internal.fallbackQueue.push(internal.snapshotBlock({ samples: new Float32Array(512), startFrame: 0, observedAtMs: 32 }));
    currentTarget = { response_id: "response-b", playback_id: "play-b", output_epoch: 2 };
    internal.consumeBlock(internal.fallbackQueue.shift()!);
    expect(appendPcm.mock.calls[0][4]).toEqual({ ...targetA, captureGeneration: 0 });
  });

  it("never uses delayed old-response probabilities to interrupt the new response", () => {
    const targetA = { response_id: "response-a", playback_id: "play-a", output_epoch: 1 };
    const targetB = { response_id: "response-b", playback_id: "play-b", output_epoch: 2 };
    const confirmed: unknown[] = [];
    const controller = new AudioCaptureController(
      {
        openCapture: () => undefined, suspendCapture: async () => undefined,
        resumeCapture: async () => undefined, sendAudioChunk: () => undefined, endUtterance: () => undefined,
      },
      "capture-1", () => undefined, (target) => confirmed.push(target), () => targetB,
    );
    const internal = controller as unknown as { handleReleasedPcm: (prefix: ReleasedPcmPrefix) => void };
    internal.handleReleasedPcm({
      generation: 0, startSample: 0, endSample: 2048, startMonotonicMs: 0, endMonotonicMs: 128,
      samples: new Int16Array(2048), probabilities: [0.99, 0.99, 0.99, 0.99],
      target: { ...targetA, captureGeneration: 0 },
    });
    expect(confirmed).toEqual([]);
    for (let index = 0; index < 4; index += 1) {
      internal.handleReleasedPcm({
        generation: 0, startSample: 2048 + index * 512, endSample: 2560 + index * 512,
        startMonotonicMs: 128 + index * 32, endMonotonicMs: 160 + index * 32,
        samples: new Int16Array(512), probabilities: [0.99], target: { ...targetB, captureGeneration: 0 },
      });
      if (index < 3) expect(confirmed).toEqual([]);
    }
    expect(confirmed).toEqual([{ ...targetB, captureGeneration: 0 }]);
  });

  it("reports a disconnected input track and a suspended audio context", () => {
    const failures: string[] = [];
    const controller = new AudioCaptureController(
      {
        openCapture: () => undefined,
        suspendCapture: async () => undefined,
        resumeCapture: async () => undefined,
        sendAudioChunk: () => undefined,
        endUtterance: () => undefined,
      },
      "capture-1",
      (message) => failures.push(message),
    );
    const internal = controller as unknown as {
      handleTrackEnded: () => void;
      handleContextStateChange: () => void;
      context: { state: string } | null;
    };

    internal.handleTrackEnded();
    expect(failures).toEqual([
      "The microphone device disconnected. Reconnect it, then restart Talk.",
    ]);

    const suspendedController = new AudioCaptureController(
      {
        openCapture: () => undefined,
        suspendCapture: async () => undefined,
        resumeCapture: async () => undefined,
        sendAudioChunk: () => undefined,
        endUtterance: () => undefined,
      },
      "capture-2",
      (message) => failures.push(message),
    );
    const suspended = suspendedController as unknown as {
      handleContextStateChange: () => void;
      context: {
        state: string;
        removeEventListener: () => void;
        close: () => Promise<void>;
      } | null;
    };
    suspended.context = {
      state: "suspended",
      removeEventListener: () => undefined,
      close: async () => undefined,
    };
    suspended.handleContextStateChange();

    expect(failures[1]).toBe(
      "The microphone audio pipeline was suspended. Restart Talk to continue.",
    );
  });

  it("only calls the local stop callback after the sampled target clears the barge-in gate", () => {
    const actions: string[] = [];
    const target = { response_id: "response-a", playback_id: "playback-a", output_epoch: 1 };
    const controller = new AudioCaptureController(
      {
        openCapture: () => undefined,
        suspendCapture: async () => undefined,
        resumeCapture: async () => undefined,
        sendControl: (action, reason) => actions.push(`${action}:${reason}`),
        sendAudioChunk: () => actions.push("audio_chunk"),
        endUtterance: () => undefined,
      },
      "capture-1",
      () => undefined,
      () => actions.push("local_stop"),
      () => target,
    );
    const handle = (controller as unknown as {
      handleReleasedPcm: (prefix: ReleasedPcmPrefix) => void;
    }).handleReleasedPcm.bind(controller);
    const sampledTarget: PcmTarget = { ...target, captureGeneration: 0 };
    const sendFrame = (index: number) => handle({
      generation: 0,
      startSample: index * 512,
      endSample: (index + 1) * 512,
      startMonotonicMs: index * 32,
      endMonotonicMs: (index + 1) * 32,
      samples: new Int16Array(512),
      probabilities: [0.9],
      target: sampledTarget,
    });
    for (let index = 0; index < 5; index += 1) sendFrame(index);
    expect(actions).toEqual([]);
    sendFrame(5);
    expect(actions).toEqual(["local_stop"]);
    expect((controller as unknown as { closed: boolean }).closed).toBe(false);
  });

  it("sends an utterance end before PCM from the next utterance", async () => {
    const messages: string[] = [];
    const controller = new AudioCaptureController(
      {
        openCapture: () => undefined,
        suspendCapture: async () => undefined,
        resumeCapture: async () => undefined,
        sendAudioChunk: () => messages.push("audio_chunk"),
        endUtterance: () => messages.push("utterance_end"),
      },
      "capture-1",
      () => undefined,
    );
    let finishDigest: (value: ArrayBuffer) => void = () => undefined;
    const digest = vi.spyOn(crypto.subtle, "digest").mockImplementation(
      () => new Promise((resolve) => { finishDigest = resolve; }),
    );
    const handle = (controller as unknown as {
      handleSpeechGateEvent: (event: unknown) => void;
    }).handleSpeechGateEvent.bind(controller);
    const firstFrame = { startSample: 0, endSample: 2, startMonotonicMs: 0, endMonotonicMs: 1, samples: new Int16Array([100, 200]) };
    handle({ type: "input_start", frame: firstFrame });
    handle({ type: "input_end", startSample: 0, endSample: 2, endMonotonicMs: 1 });
    await vi.waitFor(() => expect(digest).toHaveBeenCalledOnce());

    handle({
      type: "input_start",
      frame: { startSample: 3_000, endSample: 4_600, startMonotonicMs: 200, endMonotonicMs: 300, samples: new Int16Array(1_600).fill(300) },
    });
    await Promise.resolve();
    expect(messages).toEqual(["audio_chunk"]);

    finishDigest(new Uint8Array([1, 2, 3]).buffer);
    await (controller as unknown as { outboundChain: Promise<void> }).outboundChain;

    expect(messages).toEqual(["audio_chunk", "utterance_end", "audio_chunk"]);
    digest.mockRestore();
    await controller.close();
  });

  it("hashes exactly the approved PCM bytes after flushing the utterance range", async () => {
    const chunks: Uint8Array[] = [];
    const ended: Array<{ startFrame: number; endFrame: number; audioSha256: string }> = [];
    const controller = new AudioCaptureController(
      {
        openCapture: () => undefined,
        suspendCapture: async () => undefined,
        resumeCapture: async () => undefined,
        sendAudioChunk: (_capture, range) => chunks.push(Uint8Array.from(atob(range.pcmBase64), (value) => value.charCodeAt(0))),
        endUtterance: (_capture, range) => ended.push(range),
      },
      "capture-hash",
      () => undefined,
    );
    const handle = (controller as unknown as {
      handleSpeechGateEvent: (event: unknown) => void;
    }).handleSpeechGateEvent.bind(controller);
    const frames = [
      { startSample: 240, endSample: 242, startMonotonicMs: 15, endMonotonicMs: 15.125, samples: new Int16Array([0x1234, -2]) },
      { startSample: 242, endSample: 244, startMonotonicMs: 15.125, endMonotonicMs: 15.25, samples: new Int16Array([7, -9]) },
    ];
    handle({ type: "input_start", frame: frames[0] });
    handle({ type: "input_audio", frame: frames[1] });
    handle({ type: "input_end", startSample: 240, endSample: 244, endMonotonicMs: 15.25 });
    await (controller as unknown as { outboundChain: Promise<void> }).outboundChain;

    const approved = new Uint8Array(8);
    const view = new DataView(approved.buffer);
    [0x1234, -2, 7, -9].forEach((sample, index) => view.setInt16(index * 2, sample, true));
    const digest = await crypto.subtle.digest("SHA-256", approved);
    const expectedHash = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
    expect(chunks).toEqual([approved]);
    expect(ended).toEqual([{
      startFrame: 240,
      endFrame: 244,
      startMonotonicMs: 15,
      endMonotonicMs: 15,
      audioSha256: expectedHash,
    }]);
    await controller.close();
  });

  it("pauses the same input track and waits for server suspend/resume facts", async () => {
    const actions: string[] = [];
    const track = { id: "track-1", enabled: true } as MediaStreamTrack;
    const transport = {
      activeSessionId: "session-1",
      activeMediaSessionId: "media-1",
      activeBargeInCapability: { supported: true },
      openCapture: () => actions.push("capture_open"),
      suspendCapture: async (captureId: string) => { actions.push(`suspend:${captureId}`); },
      resumeCapture: async (captureId: string) => { actions.push(`resume:${captureId}`); },
      sendAudioChunk: () => actions.push("audio_chunk"),
      endUtterance: () => actions.push("utterance_end"),
    };
    const controller = new AudioCaptureController(
      transport,
      "capture-1",
      () => undefined,
      () => actions.push("speech_start"),
    );
    let completeWorkerReset: () => void = () => undefined;
    const detector = {
      generation: 0,
      reset: (generation: number) => new Promise<void>((resolve) => {
        completeWorkerReset = () => {
          detector.generation = generation;
          actions.push("worker_reset");
          resolve();
        };
      }),
      noteServerReady: () => { actions.push("server_ready"); return true; },
      waitUntilReady: async () => { actions.push("worker_ready"); },
    };
    const internals = controller as unknown as {
      context: { sampleRate: number };
      audioTrack: MediaStreamTrack;
      detector: typeof detector;
    };
    internals.context = { sampleRate: 48_000 };
    internals.audioTrack = track;
    internals.detector = detector;

    const token = controller.pause();
    expect(token).not.toBeNull();
    expect(track.enabled).toBe(false);
    let pauseReady = false;
    void token!.ready.then(() => { pauseReady = true; });
    await Promise.resolve();
    expect(actions).toContain("suspend:capture-1");
    expect(actions).not.toContain("worker_reset");
    expect(pauseReady).toBe(false);
    completeWorkerReset();

    await token?.ready;
    await controller.resume(token!);

    expect(track.enabled).toBe(true);
    expect(actions).toEqual([
      "suspend:capture-1",
      "worker_reset",
      "resume:capture-1",
      "server_ready",
      "worker_ready",
    ]);
  });

  it("keeps PCM ranges contiguous across long streams of small worklet blocks", async () => {
    const messages: Array<
      | { type: "audio_chunk"; start: number; end: number }
      | { type: "utterance_end" }
    > = [];
    const controller = new AudioCaptureController(
      {
        openCapture: () => undefined,
        suspendCapture: async () => undefined,
        resumeCapture: async () => undefined,
        sendAudioChunk: (_capture, range) => messages.push({
          type: "audio_chunk",
          start: range.startFrame,
          end: range.endFrame,
        }),
        endUtterance: () => messages.push({ type: "utterance_end" }),
      },
      "capture-stream",
      () => undefined,
    );
    const internals = controller as unknown as {
      resampler: StreamingPcmResampler;
      detector: { generation: number; appendPcm: (samples: Int16Array, start: number, startMs: number, endMs: number, target: PcmTarget | null) => void; close: () => void };
      consumeBlock: (block: { samples: Float32Array; startFrame: number; observedAtMs: number; captureGeneration: number; target: PcmTarget | null }) => void;
      outboundChain: Promise<void>;
    };
    internals.resampler = new StreamingPcmResampler(48_000, 16_000);
    const waterline = new PcmWatermark({ sampleRate: 16_000, frameSamples: 512, maxBufferedMs: 256 });
    internals.detector = {
      generation: 0,
      appendPcm: (samples, start, startMs, endMs, target) => {
        for (const frame of waterline.append(samples, start, startMs, endMs, target)) {
          // 仅给合成夹具标记语音/静音，用于验证帧范围与传输顺序；不代表真实 VAD 验收。
          const probability = frame.samples.some((sample) => Math.abs(sample) > 1000) ? 0.9 : 0.1;
          for (const prefix of waterline.resolve(frame.frameId, 0, probability)) {
            (controller as unknown as { handleReleasedPcm: (value: ReleasedPcmPrefix) => void }).handleReleasedPcm(prefix);
          }
        }
      },
      close: () => undefined,
    };
    let inputFrame = 0;
    let observedAtMs = 0;
    for (let segment = 0; segment < 3; segment += 1) {
      for (let index = 0; index < 120; index += 1) {
        const samples = Float32Array.from(
          { length: 128 },
          (_, offset) => 0.4 * Math.sin((2 * Math.PI * 440 * (inputFrame + offset)) / 48_000),
        );
        internals.consumeBlock({ samples, startFrame: inputFrame, observedAtMs, captureGeneration: 0, target: null });
        inputFrame += samples.length;
        observedAtMs += (samples.length / 48_000) * 1_000;
      }
      for (let index = 0; index < 280; index += 1) {
        const samples = new Float32Array(128);
        internals.consumeBlock({ samples, startFrame: inputFrame, observedAtMs, captureGeneration: 0, target: null });
        inputFrame += samples.length;
        observedAtMs += (samples.length / 48_000) * 1_000;
      }
    }
    await internals.outboundChain;

    let expectedStart: number | null = null;
    for (const message of messages) {
      if (message.type === "utterance_end") {
        expectedStart = null;
        continue;
      }
      if (expectedStart !== null) expect(message.start).toBe(expectedStart);
      expectedStart = message.end;
    }
    expect(messages.filter((message) => message.type === "utterance_end")).toHaveLength(3);
    await controller.close();
  });


});
