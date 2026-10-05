import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  NativeOutputPlayer,
  type NativePlaybackEventRequest,
  type NativePlaybackIdentity,
} from "./native-player";
import type { NativePcmSource } from "./native-output-source";

class FakeAudio extends EventTarget {
  currentTime = 3;
  volume = 1;
  muted = false;
  src = "";
  paused = true;
  play = vi.fn(async () => {
    this.paused = false;
  });
  pause = vi.fn(() => {
    this.paused = true;
    this.dispatchEvent(new Event("pause"));
  });
  load = vi.fn();
  removeAttribute = vi.fn((name: string) => {
    if (name === "src") this.src = "";
  });
}

const identity: NativePlaybackIdentity = {
  playback_id: "play-2",
  output_epoch: 2,
  response_id: "resp-2",
  media_session_id: "media-2",
};

describe("NativeOutputPlayer", () => {
  beforeEach(() => {
    const NativeURL = URL;
    vi.stubGlobal(
      "URL",
      Object.assign(class URLMock extends NativeURL {}, {
        createObjectURL: vi.fn(() => "blob:clip"),
        revokeObjectURL: vi.fn(),
      }),
    );
    vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("拒绝旧epoch和错配来源，只接受当前real输出", () => {
    const player = new NativeOutputPlayer({
      identity,
      session_id: "11111111-1111-1111-1111-111111111111",
      emit: vi.fn(),
    });

    expect(player.accepts({ epoch: 1, source: "real" })).toBe(false);
    expect(player.accepts({ epoch: 2, source: "simulated" })).toBe(false);
    expect(player.accepts({ epoch: 2, source: "real" })).toBe(true);
    expect(() => player.attach({ epoch: 1, source: "real" })).toThrow(
      /旧epoch或错配来源/,
    );
    expect(() => player.attach({ epoch: 2, source: "network" })).toThrow(
      /旧epoch或错配来源/,
    );
  });

  it("桥接LocalPlayer真实反馈并在终态后拒绝迟到反馈", async () => {
    const audio = new FakeAudio();
    const events: NativePlaybackEventRequest[] = [];
    const player = new NativeOutputPlayer({
      identity,
      session_id: "11111111-1111-1111-1111-111111111111",
      emit: (event) => events.push(event),
      audio: audio as unknown as HTMLAudioElement,
      createContext: () => fakeContext() as unknown as AudioContext,
    });

    player.attach({ epoch: 2, source: "real" });
    await player.start(new File(["audio"], "clip.wav"), 100);
    audio.dispatchEvent(new Event("playing"));
    audio.dispatchEvent(new Event("ended"));
    const count = events.length;
    audio.dispatchEvent(new Event("playing"));

    expect(events).toHaveLength(4);
    expect(events.at(count - 1)).toEqual(events.at(-1));
    expect(events.map((event) => event.producer_seq)).toEqual([1, 2, 3, 4]);
    expect(events[0]).toMatchObject({
      action: "open",
      session_id: "11111111-1111-1111-1111-111111111111",
      playback_id: "play-2",
      response_id: "resp-2",
      producer_seq: 1,
      output_epoch: 2,
      media_session_id: "media-2",
      start_ms: 100,
    });
    expect(events[1]).toMatchObject({
      action: "audible",
      producer_seq: 2,
      audible_ms: 3000,
    });
    expect(events[2]).toMatchObject({
      action: "progress",
      producer_seq: 3,
      played_ms: 3000,
      interval_ms: expect.any(Number),
      phase: "playing",
    });
    expect(events[3]).toMatchObject({
      action: "end",
      producer_seq: 4,
      scope: "complete",
      played_ms: 3000,
    });
    expect(player.accepts({ epoch: 2, source: "real" })).toBe(false);
    await player.destroy();
  });

  it("显式停止和播放错误都映射为interrupt", async () => {
    const stoppedAudio = new FakeAudio();
    const stoppedEvents: NativePlaybackEventRequest[] = [];
    const stopped = new NativeOutputPlayer({
      identity,
      session_id: "11111111-1111-1111-1111-111111111111",
      emit: (event) => stoppedEvents.push(event),
      audio: stoppedAudio as unknown as HTMLAudioElement,
      createContext: () => fakeContext() as unknown as AudioContext,
    });
    stopped.attach({ epoch: 2, source: "real" });
    await stopped.start(new File(["audio"], "clip.wav"), 0);
    stopped.stop();

    expect(stoppedEvents).toHaveLength(2);
    expect(stoppedEvents[1]).toMatchObject({
      action: "interrupt",
      producer_seq: 2,
      reason: "player-stop",
      played_ms: 3000,
    });
    await stopped.destroy();

    const errorAudio = new FakeAudio();
    const errorEvents: NativePlaybackEventRequest[] = [];
    const failed = new NativeOutputPlayer({
      identity,
      session_id: "11111111-1111-1111-1111-111111111111",
      emit: (event) => errorEvents.push(event),
      audio: errorAudio as unknown as HTMLAudioElement,
      createContext: () => fakeContext() as unknown as AudioContext,
    });
    failed.attach({ epoch: 2, source: "real" });
    await failed.start(new File(["audio"], "clip.wav"), 0);
    errorAudio.dispatchEvent(new Event("error"));

    expect(errorEvents).toHaveLength(2);
    expect(errorEvents[1]).toMatchObject({
      action: "interrupt",
      producer_seq: 2,
      reason: "error",
      played_ms: 3000,
    });
    await failed.destroy();
  });

  it("destroy后迟到反馈不产生事件", async () => {
    const audio = new FakeAudio();
    const events: NativePlaybackEventRequest[] = [];
    const player = new NativeOutputPlayer({
      identity,
      session_id: "11111111-1111-1111-1111-111111111111",
      emit: (event) => events.push(event),
      audio: audio as unknown as HTMLAudioElement,
      createContext: () => fakeContext() as unknown as AudioContext,
    });

    player.attach({ epoch: 2, source: "real" });
    await player.start(new File(["audio"], "clip.wav"), 0);
    await player.destroy();
    audio.dispatchEvent(new Event("playing"));
    audio.dispatchEvent(new Event("error"));

    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ action: "open", producer_seq: 1 });
  });

  it("接受PCM流并记录起播首可听和完成终态", async () => {
    vi.useFakeTimers();
    const context = fakePcmContext();
    const events: NativePlaybackEventRequest[] = [];
    const source: NativePcmSource = {
      sample_rate: 24000,
      channels: 1,
      chunks: (async function* () {
        yield {
          data: new Uint8Array([0x01, 0x00, 0x02, 0x00]).buffer,
          segment_id: "item-1",
          chunk_seq: 1,
        };
        yield {
          data: new Uint8Array([0x03, 0x00, 0x04, 0x00, 0x05, 0x00, 0x06, 0x00])
            .buffer,
          segment_id: "item-1",
          chunk_seq: 2,
        };
      })(),
    };
    const player = new NativeOutputPlayer({
      identity,
      session_id: "11111111-1111-1111-1111-111111111111",
      emit: (event) => events.push(event),
      createContext: () => context as unknown as AudioContext,
    });

    try {
      player.attach({ epoch: 2, source: "real" });
      await player.start(source, 0);
      await vi.advanceTimersByTimeAsync(20);
      context.sources.at(-1)?.onended?.();

      expect(events.map((event) => event.action)).toEqual([
        "open",
        "audible",
        "progress",
        "end",
      ]);
      expect(events[1]).toMatchObject({
        producer_seq: 2,
        audible_ms: 0,
      });
      expect(events[2]).toMatchObject({
        action: "progress",
        producer_seq: 3,
        played_ms: 0,
        phase: "playing",
      });
      expect(events[3]).toMatchObject({
        scope: "complete",
        producer_seq: 4,
        played_ms: 0,
      });
      expect(context.buffers.map((buffer) => buffer.length)).toEqual([2, 4]);
      expect(context.sources).toHaveLength(2);
      await player.destroy();
    } finally {
      vi.useRealTimers();
    }
  });
});

function fakeContext() {
  return {
    resume: vi.fn(async () => undefined),
    close: vi.fn(async () => undefined),
    destination: {},
    createAnalyser: vi.fn(() => ({
      fftSize: 32,
      connect: vi.fn(),
      getByteTimeDomainData: vi.fn(),
    })),
    createMediaElementSource: vi.fn(() => ({ connect: vi.fn() })),
  };
}

function fakePcmContext() {
  const buffers: Array<{ length: number; sampleRate: number; getChannelData: () => Float32Array }> = [];
  const sources: Array<{
    buffer?: unknown;
    connect: ReturnType<typeof vi.fn>;
    start: ReturnType<typeof vi.fn>;
    stop: ReturnType<typeof vi.fn>;
    onended?: (() => void) | null;
  }> = [];
  return {
    currentTime: 0,
    resume: vi.fn(async () => undefined),
    close: vi.fn(async () => undefined),
    destination: {},
    createAnalyser: vi.fn(() => ({
      fftSize: 32,
      connect: vi.fn(),
      getByteTimeDomainData: vi.fn(),
    })),
    createBuffer: vi.fn((channels: number, length: number, sampleRate: number) => {
      const buffer = {
        length,
        sampleRate,
        getChannelData: () => new Float32Array(length),
      };
      buffers.push(buffer);
      return buffer;
    }),
    createBufferSource: vi.fn(() => {
      const source = {
        connect: vi.fn(),
        start: vi.fn(),
        stop: vi.fn(),
        onended: null,
      };
      sources.push(source);
      return source;
    }),
    buffers,
    sources,
  };
}
