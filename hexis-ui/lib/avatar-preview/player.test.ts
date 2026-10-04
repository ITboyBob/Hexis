import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LocalPlayer } from "./player";

class FakeAudio extends EventTarget {
  currentTime = 3; volume = 1; muted = false; src = ""; paused = true;
  play = vi.fn(async () => { this.paused = false; });
  pause = vi.fn(() => { this.paused = true; this.dispatchEvent(new Event("pause")); });
  load = vi.fn(); removeAttribute = vi.fn((name: string) => { if (name === "src") this.src = ""; });
}

describe("本地播放器反馈", () => {
  beforeEach(() => {
    const NativeURL = URL;
    vi.stubGlobal("URL", Object.assign(class URLMock extends NativeURL {}, {
      createObjectURL: vi.fn(() => "blob:clip"), revokeObjectURL: vi.fn(),
    }));
  });
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
  it("Promise受理不冒充playing，真实事件归属到当前epoch，终态阻止旧playing复活", async () => {
    const audio = new FakeAudio();
    const emitted: unknown[] = [];
    const player = new LocalPlayer((value) => emitted.push(value), { audio: audio as unknown as HTMLAudioElement,
      createContext: () => fakeContext() as unknown as AudioContext });
    await player.start(new File(["audio"], "clip.wav"), { id: "p1", epoch: 4 });
    expect(emitted).toEqual([]);
    audio.dispatchEvent(new Event("playing"));
    expect(emitted.at(-1)).toMatchObject({ playback_id: "p1", output_epoch: 4, phase: "playing", source: "real" });
    audio.dispatchEvent(new Event("ended"));
    const count = emitted.length;
    audio.dispatchEvent(new Event("playing"));
    expect(emitted).toHaveLength(count);
    await player.destroy();
  });

  it("携带response_id/media_session_id/played_ms/interval_ms且终态后迟到反馈无效", async () => {
    const audio = new FakeAudio();
    const emitted: unknown[] = [];
    const player = new LocalPlayer((value) => emitted.push(value), {
      audio: audio as unknown as HTMLAudioElement,
      createContext: () => fakeContext() as unknown as AudioContext,
    });
    await player.start(new File(["audio"], "clip.wav"), {
      id: "play-1",
      epoch: 7,
      response_id: "resp-1",
      media_session_id: "media-1",
    });
    audio.dispatchEvent(new Event("playing"));
    expect(emitted.at(-1)).toMatchObject({
      playback_id: "play-1",
      output_epoch: 7,
      response_id: "resp-1",
      media_session_id: "media-1",
      phase: "playing",
      played_ms: 3000,
    });
    expect((emitted.at(-1) as { interval_ms: number }).interval_ms).toBeGreaterThanOrEqual(0);
    audio.dispatchEvent(new Event("ended"));
    const count = emitted.length;
    audio.dispatchEvent(new Event("playing"));
    expect(emitted).toHaveLength(count);
    await player.destroy();
  });

  it("destroy释放object URL与AudioContext，旧元素事件不再发出", async () => {
    const audio = new FakeAudio();
    const close = vi.fn(async () => undefined);
    const context = { resume: vi.fn(async () => undefined), close, createAnalyser: vi.fn(() => ({ fftSize: 32, connect: vi.fn(), getByteTimeDomainData: vi.fn() })), createMediaElementSource: vi.fn(() => ({ connect: vi.fn() })), destination: {} };
    vi.stubGlobal("AudioContext", vi.fn(() => context));
    vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    const revoke = vi.mocked(URL.revokeObjectURL);
    const emit = vi.fn();
    const player = new LocalPlayer(emit, { audio: audio as unknown as HTMLAudioElement,
      createContext: () => context as unknown as AudioContext });
    await player.start(new File(["audio"], "clip.wav"), { id: "p1", epoch: 4 });
    await player.destroy();
    const count = emit.mock.calls.length;
    audio.dispatchEvent(new Event("playing"));
    expect(emit).toHaveBeenCalledTimes(count);
    expect(revoke).toHaveBeenCalledWith("blob:clip");
    expect(close).toHaveBeenCalledOnce();
  });

  it("静音保留playing事实但幅度为零，destroy后的旧RAF和旧元素事件无效", async () => {
    const audio = new FakeAudio(); audio.volume = 0;
    const frames: FrameRequestCallback[] = [];
    const pendingFrames = new Set<number>();
    let frameId = 0;
    vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => { frames.push(callback); frameId += 1; pendingFrames.add(frameId); return frameId; }));
    vi.stubGlobal("cancelAnimationFrame", vi.fn((id: number) => { pendingFrames.delete(id); }));
    const emit = vi.fn();
    const player = new LocalPlayer(emit, { audio: audio as unknown as HTMLAudioElement,
      createContext: () => fakeContext() as unknown as AudioContext });
    await player.start(new File(["audio"], "clip.wav"), { id: "p2", epoch: 5 });
    audio.dispatchEvent(new Event("playing"));
    expect(emit).toHaveBeenCalledWith(expect.objectContaining({ phase: "playing", amplitude: 0 }));
    player.setVolume(0.5);
    player.setMuted(true);
    expect(pendingFrames.size).toBe(1);
    expect(emit).toHaveBeenCalledWith(expect.objectContaining({ phase: "playing", amplitude: 0 }));
    const staleFrame = frames.at(-1)!;
    await player.destroy();
    expect(pendingFrames.size).toBe(0);
    const count = emit.mock.calls.length;
    staleFrame(0);
    audio.dispatchEvent(new Event("playing"));
    expect(emit).toHaveBeenCalledTimes(count);
  });

  it("resume尚未完成时stop/destroy会阻止之后调用play", async () => {
    const audio = new FakeAudio();
    let resolveResume!: () => void;
    const context = fakeContext();
    context.resume = vi.fn(() => new Promise<void>((resolve) => { resolveResume = resolve; }));
    const player = new LocalPlayer(vi.fn(), { audio: audio as unknown as HTMLAudioElement,
      createContext: () => context as unknown as AudioContext });
    const starting = player.start(new File(["audio"], "clip.wav"), { id: "p3", epoch: 6 });
    player.stop();
    await player.destroy();
    resolveResume();
    await starting;
    expect(audio.play).not.toHaveBeenCalled();
  });

  it("resume Promise在stop后才完成时会再次暂停实际Audio", async () => {
    const audio = new FakeAudio();
    const player = new LocalPlayer(vi.fn(), { audio: audio as unknown as HTMLAudioElement,
      createContext: () => fakeContext() as unknown as AudioContext });
    await player.start(new File(["audio"], "clip.wav"), { id: "p5", epoch: 8 });
    let resolvePlay!: () => void;
    audio.play = vi.fn(() => new Promise<void>((resolve) => { resolvePlay = resolve; }));
    const resuming = player.resume();
    const pausesBeforeStop = audio.pause.mock.calls.length;
    player.stop();
    const pausesAfterStop = audio.pause.mock.calls.length;
    resolvePlay();
    await resuming;
    expect(pausesAfterStop).toBeGreaterThan(pausesBeforeStop);
    expect(audio.pause.mock.calls.length).toBeGreaterThan(pausesAfterStop);
    await player.destroy();
  });

  it("AudioContext关闭失败给出可恢复诊断", async () => {
    const context = fakeContext();
    context.close = vi.fn(async () => { throw new Error("closed"); });
    const player = new LocalPlayer(vi.fn(), { audio: new FakeAudio() as unknown as HTMLAudioElement,
      createContext: () => context as unknown as AudioContext });
    await player.start(new File(["audio"], "clip.wav"), { id: "p4", epoch: 7 });
    await expect(player.destroy()).rejects.toThrow(/刷新预览后重试/);
  });
});

function fakeContext() {
  return {
    resume: vi.fn(async () => undefined), close: vi.fn(async () => undefined), destination: {},
    createAnalyser: vi.fn(() => ({ fftSize: 32, connect: vi.fn(), getByteTimeDomainData: vi.fn() })),
    createMediaElementSource: vi.fn(() => ({ connect: vi.fn() })),
  };
}
