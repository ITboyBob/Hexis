import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  NativeOutputPlayer,
  type NativePlaybackEventRequest,
  type NativePlaybackIdentity,
} from "./native-player";
import type { NativePcmSource } from "./native-output-source";
import { NativeOutputQueue } from "../voice-realtime/output-queue";

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

  it("在流结束前起播，且仅在实际最后帧结束后报告drained", async () => {
    const context = fakePcmContext();
    const events: NativePlaybackEventRequest[] = [];
    const queue = new NativeOutputQueue();
    const player = new NativeOutputPlayer({
      identity,
      session_id: "11111111-1111-1111-1111-111111111111",
      emit: (event) => events.push(event),
      createContext: () => context as unknown as AudioContext,
    });
    queue.push({
      data: new Uint8Array([1, 0, 2, 0]).buffer,
      segment_id: "segment-1",
      chunk_seq: 1,
    });
    player.attach({ epoch: 2, source: "real" });
    const starting = player.start(
      { sample_rate: 24_000, channels: 1, chunks: queue },
      100,
    );
    await vi.waitFor(() => expect(events[0]?.action).toBe("open"));
    expect(events.some((event) => event.action === "end")).toBe(false);

    queue.push({
      data: new Uint8Array([3, 0, 4, 0]).buffer,
      segment_id: "segment-1",
      chunk_seq: 2,
    });
    queue.close();
    await starting;
    expect(events.some((event) => event.action === "end")).toBe(false);
    context.sources.at(-1)?.onended?.();
    expect(events.at(-1)).toMatchObject({ action: "end", scope: "complete" });
    await player.destroy();
  });

  it("barge-in终止本地PCM并回报有界已播时长", async () => {
    const context = fakePcmContext();
    const events: NativePlaybackEventRequest[] = [];
    const player = new NativeOutputPlayer({
      identity,
      session_id: "11111111-1111-1111-1111-111111111111",
      emit: (event) => events.push(event),
      createContext: () => context as unknown as AudioContext,
    });
    const source: NativePcmSource = {
      sample_rate: 24_000,
      channels: 1,
      chunks: (async function* () {
        yield {
          data: new Uint8Array(48_000).buffer,
          segment_id: "segment-1",
          chunk_seq: 1,
        };
      })(),
    };
    player.attach({ epoch: 2, source: "real" });
    await player.start(source, 0);
    context.currentTime = 0.42;
    player.stop("barge_in");

    expect(events.at(-1)).toMatchObject({
      action: "interrupt",
      reason: "barge_in",
      played_ms: 400,
    });
    await player.destroy();
  });

  it("空PCM流报告错误且不伪造播放open", async () => {
    const events: NativePlaybackEventRequest[] = [];
    const player = new NativeOutputPlayer({
      identity,
      session_id: "11111111-1111-1111-1111-111111111111",
      emit: (event) => events.push(event),
      createContext: () => fakePcmContext() as unknown as AudioContext,
    });
    player.attach({ epoch: 2, source: "real" });
    await expect(
      player.start(
        { sample_rate: 24_000, channels: 1, chunks: (async function* () {})() },
        0,
      ),
    ).rejects.toThrow(/不包含音频帧/);
    expect(events.map((event) => event.action)).toEqual([]);
    await player.destroy();
  });

  it("快速到达的长回答完整播放，调度窗口与存活节点保持有界", async () => {
    vi.useFakeTimers();
    const context = fakePcmContext();
    const began = Date.now();
    Object.defineProperty(context, "currentTime", { get: () => (Date.now() - began) / 1000 });
    let alive = 0;
    let maxAlive = 0;
    let maxLookahead = 0;
    const createSource = context.createBufferSource.getMockImplementation()!;
    context.createBufferSource.mockImplementation(() => {
      const node = createSource();
      node.start.mockImplementation((at: number) => {
        alive++;
        maxAlive = Math.max(maxAlive, alive);
        const buffer = node.buffer as { length: number; sampleRate: number };
        const duration = buffer.length / buffer.sampleRate;
        maxLookahead = Math.max(maxLookahead, at + duration - context.currentTime);
        setTimeout(() => { alive--; node.onended?.(); }, Math.ceil((at + duration - context.currentTime) * 1000));
      });
      return node;
    });
    const queue = new NativeOutputQueue();
    const events: NativePlaybackEventRequest[] = [];
    const player = new NativeOutputPlayer({ identity, session_id: "session-1", emit: (event) => events.push(event), createContext: () => context as unknown as AudioContext });
    try {
      player.attach({ epoch: 2, source: "real" });
      const starting = player.start({ sample_rate: 24_000, channels: 1, chunks: queue }, 0);
      // 120 秒合法 PCM 在一个网络突发中到达；一秒大块还需被拆分调度。
      for (let chunk_seq = 0; chunk_seq < 120; chunk_seq++) {
        queue.push({ data: new ArrayBuffer(48_000), segment_id: "segment-1", chunk_seq });
      }
      queue.close();
      const result = starting.then(() => null, (error: unknown) => error);
      await vi.advanceTimersByTimeAsync(500);
      expect(events[0]?.action).toBe("open");
      expect(events.some((event) => event.action === "end")).toBe(false);
      await vi.advanceTimersByTimeAsync(121_000);
      expect(await result).toBeNull();
      expect(events.at(-1)).toMatchObject({ action: "end", scope: "complete", played_ms: 120_000 });
      expect(context.buffers.reduce((sum, buffer) => sum + buffer.length, 0)).toBe(120 * 24_000);
      expect(maxLookahead).toBeLessThanOrEqual(1.101);
      expect(maxAlive).toBeLessThanOrEqual(12);
      expect(alive).toBe(0);
      expect(queue.bufferedMs).toBe(0);
    } finally {
      await player.destroy();
      vi.useRealTimers();
    }
  });

  it("流失败保留原始原因，立即停播并只报告一次error终态", async () => {
    const context = fakePcmContext();
    const events: NativePlaybackEventRequest[] = [];
    const queue = new NativeOutputQueue();
    const player = new NativeOutputPlayer({ identity, session_id: "session-1", emit: (event) => events.push(event), createContext: () => context as unknown as AudioContext });
    player.attach({ epoch: 2, source: "real" });
    const starting = player.start({ sample_rate: 24_000, channels: 1, chunks: queue }, 0);
    const result = starting.catch((error: unknown) => error);
    queue.push({ data: new ArrayBuffer(4800), segment_id: "segment-1", chunk_seq: 1 });
    await vi.waitFor(() => expect(events[0]?.action).toBe("open"));
    const cause = new Error("音频播放缓冲已达到上限");
    queue.fail(cause);
    const error = await result as Error;
    expect(error.cause).toBe(cause);
    expect(error.message).toContain(cause.message);
    expect(error.message).not.toContain("检查音频格式");
    expect(context.sources.every((node) => node.stop.mock.calls.length === 1)).toBe(true);
    expect(events.filter((event) => event.action === "interrupt")).toEqual([
      expect.objectContaining({ reason: "error" }),
    ]);
    await player.destroy();
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
    disconnect: ReturnType<typeof vi.fn>;
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
        onended: null as (() => void) | null,
        buffer: undefined as unknown,
        disconnect: vi.fn(),
      };
      sources.push(source);
      return source;
    }),
    buffers,
    sources,
  };
}
