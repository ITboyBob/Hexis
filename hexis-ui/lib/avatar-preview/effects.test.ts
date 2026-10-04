import { afterEach, describe, expect, it, vi } from "vitest";
import { Effects } from "./effects";
import type { Character } from "./engine";

function fixture(gap = 0) {
  const character = {
    bounceOnce: vi.fn(),
    burstOnce: vi.fn(),
    stopVisualEffect: vi.fn(),
    resetVisual: vi.fn(),
    destroy: vi.fn(),
  } as unknown as Character;
  return { character, effects: new Effects(character, gap) };
}

describe("Avatar 视觉效果队列", () => {
  afterEach(() => vi.useRealTimers());

  it("串行执行并忽略重复 effect id", () => {
    vi.useFakeTimers();
    const { character, effects } = fixture();
    effects.enqueue({ id: "a", kind: "bounce", duration_ms: 100 });
    effects.enqueue({ id: "a", kind: "burst", duration_ms: 100 });
    effects.enqueue({ id: "b", kind: "burst", duration_ms: 100 });
    expect(character.bounceOnce).toHaveBeenCalledTimes(1);
    expect(character.burstOnce).not.toHaveBeenCalled();
    vi.advanceTimersByTime(100);
    expect(character.stopVisualEffect).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(0);
    expect(character.burstOnce).toHaveBeenCalledTimes(1);
  });

  it("停止指定排队效果时保留其他 ID", () => {
    vi.useFakeTimers();
    const { character, effects } = fixture();
    effects.enqueue({ id: "active", kind: "bounce", duration_ms: 100 });
    effects.enqueue({ id: "keep", kind: "burst", duration_ms: 100 });
    effects.enqueue({ id: "remove", kind: "burst", duration_ms: 100 });
    effects.stop("remove");
    effects.stop("active");
    expect(character.stopVisualEffect).toHaveBeenCalledTimes(1);
    expect(character.burstOnce).toHaveBeenCalledTimes(1);
  });

  it("停止活动效果后仍遵守开始间隔", () => {
    vi.useFakeTimers();
    const { character, effects } = fixture(50);
    effects.enqueue({ id: "active", kind: "bounce", duration_ms: 100 });
    effects.enqueue({ id: "next", kind: "burst", duration_ms: 100 });
    effects.stop("active");
    expect(character.burstOnce).not.toHaveBeenCalled();
    vi.advanceTimersByTime(49);
    expect(character.burstOnce).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(character.burstOnce).toHaveBeenCalledTimes(1);
  });

  it("reset 后已排队的旧回调不能清掉新效果timer", () => {
    vi.useFakeTimers();
    const setTimeoutSpy = vi.spyOn(globalThis, "setTimeout");
    const clearTimeoutSpy = vi.spyOn(globalThis, "clearTimeout");
    const { character, effects } = fixture();
    effects.enqueue({ id: "old", kind: "bounce", duration_ms: 100 });
    const oldCall = setTimeoutSpy.mock.calls.at(-1)!;
    const oldCallback = oldCall[0] as () => void;

    effects.reset();
    effects.enqueue({ id: "new", kind: "burst", duration_ms: 100 });
    const newTimer = setTimeoutSpy.mock.results.at(-1)!.value;
    oldCallback();
    effects.stop("new");

    expect(clearTimeoutSpy).toHaveBeenCalledWith(newTimer);
    expect(character.burstOnce).toHaveBeenCalledTimes(1);
  });

  it("拒绝无效 duration 与 gap", () => {
    expect(() => fixture(-1)).toThrow("间隔无效");
    const { effects } = fixture();
    expect(() => effects.enqueue({ id: "bad", kind: "bounce", duration_ms: 0 })).toThrow("持续时间无效");
  });

  it("reset 取消旧 timeout 并清空队列", () => {
    vi.useFakeTimers();
    const { character, effects } = fixture();
    effects.enqueue({ id: "active", kind: "bounce", duration_ms: 100 });
    effects.enqueue({ id: "queued", kind: "burst", duration_ms: 100 });
    effects.reset();
    vi.advanceTimersByTime(1000);
    expect(character.resetVisual).toHaveBeenCalledTimes(1);
    expect(character.stopVisualEffect).not.toHaveBeenCalled();
    expect(character.burstOnce).not.toHaveBeenCalled();
  });

  it("切换到 reduced motion 时停效并拒绝新效果", () => {
    vi.useFakeTimers();
    const { character, effects } = fixture();
    effects.enqueue({ id: "active", kind: "bounce", duration_ms: 100 });
    effects.setReduced(true);
    effects.enqueue({ id: "blocked", kind: "burst", duration_ms: 100 });
    expect(character.resetVisual).toHaveBeenCalledTimes(1);
    expect(character.burstOnce).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1000);
    expect(character.stopVisualEffect).not.toHaveBeenCalled();
  });

  it("destroy 幂等且销毁后忽略队列与reset", () => {
    vi.useFakeTimers();
    const { character, effects } = fixture();
    effects.enqueue({ id: "active", kind: "bounce", duration_ms: 100 });
    effects.destroy();
    effects.destroy();
    effects.enqueue({ id: "late", kind: "burst", duration_ms: 100 });
    effects.reset();
    vi.advanceTimersByTime(1000);
    expect(character.destroy).toHaveBeenCalledTimes(1);
    expect(character.burstOnce).not.toHaveBeenCalled();
  });

  it("cancelEffects清队列但不改变角色，由Avatar generation统一重置", () => {
    vi.useFakeTimers();
    const { character, effects } = fixture();
    effects.enqueue({ id: "active", kind: "bounce", duration_ms: 100 });
    effects.enqueue({ id: "queued", kind: "burst", duration_ms: 100 });
    effects.cancelEffects();
    expect(character.stopVisualEffect).not.toHaveBeenCalled();
    expect(character.resetVisual).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1000);
    expect(character.burstOnce).not.toHaveBeenCalled();
  });

  it("更新间隔会安全重设等待窗口并报告实际配置", () => {
    vi.useFakeTimers();
    const { character } = fixture(10);
    const snapshots: Array<{ active?: string; queued: string[]; min_gap_ms: number }> = [];
    const effects = new Effects(character, 10, (snapshot) => snapshots.push(snapshot));
    effects.enqueue({ id: "first", kind: "bounce", duration_ms: 20 });
    effects.enqueue({ id: "next", kind: "burst", duration_ms: 20 });
    vi.advanceTimersByTime(20);
    effects.setGap(50);
    vi.advanceTimersByTime(49);
    expect(character.burstOnce).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(character.burstOnce).toHaveBeenCalledOnce();
    expect(snapshots.at(-1)).toMatchObject({ active: "next", min_gap_ms: 50 });
  });
});
