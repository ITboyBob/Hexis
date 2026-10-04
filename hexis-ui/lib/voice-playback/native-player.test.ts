import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  NativeOutputPlayer,
  type NativePlaybackEventRequest,
  type NativePlaybackIdentity,
} from "./native-player";

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

    expect(events).toHaveLength(3);
    expect(events.at(count - 1)).toEqual(events.at(-1));
    expect(events.map((event) => event.producer_seq)).toEqual([1, 2, 3]);
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
      action: "progress",
      producer_seq: 2,
      played_ms: 3000,
      interval_ms: expect.any(Number),
      phase: "playing",
    });
    expect(events[2]).toMatchObject({
      action: "end",
      producer_seq: 3,
      scope: "complete",
      played_ms: 3000,
    });
    expect(player.accepts({ epoch: 2, source: "real" })).toBe(false);
    await player.destroy();
  });

  it("显式停止映射为interrupt，播放错误映射为unknown终态", async () => {
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
      action: "end",
      producer_seq: 2,
      scope: "unknown",
      played_ms: 3000,
    });
    await failed.destroy();
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
