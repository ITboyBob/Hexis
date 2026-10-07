import { describe, expect, it } from "vitest";

import { AudioCaptureController, StreamingPcmResampler } from "./capture";
import { StreamingVad, type VadEvent } from "./vad";

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

  it("stops local output before signaling barge-in and keeps capture active", () => {
    const actions: string[] = [];
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
    );
    const handle = (
      controller as unknown as { handleVadEvent: (event: VadEvent) => void }
    ).handleVadEvent.bind(controller);

    handle({
      type: "speech_start",
      samples: new Int16Array([100, 200]),
      startFrame: 0,
      endFrame: 2,
      startMonotonicMs: 0,
      endMonotonicMs: 1,
    });

    expect(actions).toEqual(["local_stop", "barge_in:speech_start"]);
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
    const handle = (
      controller as unknown as { handleVadEvent: (event: VadEvent) => void }
    ).handleVadEvent.bind(controller);

    handle({
      type: "speech_start",
      samples: new Int16Array([100, 200]),
      startFrame: 0,
      endFrame: 2,
      startMonotonicMs: 0,
      endMonotonicMs: 1,
    });
    handle({
      type: "utterance_end",
      startFrame: 0,
      endFrame: 2,
      startMonotonicMs: 0,
      endMonotonicMs: 1,
    });
    await vi.waitFor(() => expect(digest).toHaveBeenCalledOnce());

    handle({
      type: "speech_start",
      samples: new Int16Array(1_600).fill(300),
      startFrame: 3_000,
      endFrame: 4_600,
      startMonotonicMs: 200,
      endMonotonicMs: 300,
    });
    await Promise.resolve();
    expect(messages).toEqual(["audio_chunk"]);

    finishDigest(new Uint8Array([1, 2, 3]).buffer);
    await (controller as unknown as { outboundChain: Promise<void> }).outboundChain;

    expect(messages).toEqual(["audio_chunk", "utterance_end", "audio_chunk"]);
    digest.mockRestore();
    await controller.close();
  });

  it("pauses the same input track and waits for server suspend/resume facts", async () => {
    const actions: string[] = [];
    const track = { id: "track-1", enabled: true } as MediaStreamTrack;
    const transport = {
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
    const internals = controller as unknown as {
      context: { sampleRate: number };
      audioTrack: MediaStreamTrack;
      vad: { reset: () => void };
      handleVadEvent: (event: VadEvent) => void;
    };
    internals.context = { sampleRate: 48_000 };
    internals.audioTrack = track;
    internals.vad = { reset: () => actions.push("vad_reset") };

    const token = controller.pause();
    expect(token).not.toBeNull();
    expect(track.enabled).toBe(false);
    expect(actions).toContain("vad_reset");
    internals.handleVadEvent({
      type: "speech_start",
      samples: new Int16Array([100, 200]),
      startFrame: 0,
      endFrame: 2,
      startMonotonicMs: 0,
      endMonotonicMs: 1,
    });
    expect(actions).not.toContain("speech_start");

    await token?.ready;
    await controller.resume(token!);

    expect(track.enabled).toBe(true);
    expect(actions).toEqual([
      "vad_reset",
      "suspend:capture-1",
      "resume:capture-1",
      "vad_reset",
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
      vad: StreamingVad;
      consumeBlock: (block: { samples: Float32Array; startFrame: number; observedAtMs: number }) => void;
      outboundChain: Promise<void>;
    };
    internals.resampler = new StreamingPcmResampler(48_000, 16_000);
    internals.vad = new StreamingVad({ sampleRate: 16_000 });
    let inputFrame = 0;
    let observedAtMs = 0;
    for (let segment = 0; segment < 3; segment += 1) {
      for (let index = 0; index < 120; index += 1) {
        const samples = Float32Array.from(
          { length: 128 },
          (_, offset) => 0.4 * Math.sin((2 * Math.PI * 440 * (inputFrame + offset)) / 48_000),
        );
        internals.consumeBlock({ samples, startFrame: inputFrame, observedAtMs });
        inputFrame += samples.length;
        observedAtMs += (samples.length / 48_000) * 1_000;
      }
      for (let index = 0; index < 280; index += 1) {
        const samples = new Float32Array(128);
        internals.consumeBlock({ samples, startFrame: inputFrame, observedAtMs });
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
