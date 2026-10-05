import { describe, expect, it } from "vitest";

import { AudioCaptureController, StreamingPcmResampler } from "./capture";
import type { VadEvent } from "./vad";

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
});
