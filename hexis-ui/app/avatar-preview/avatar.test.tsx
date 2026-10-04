import { StrictMode } from "react";
import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Avatar } from "./avatar";
import type { Character, Composition } from "@/lib/avatar-preview/engine";

const engine = vi.hoisted(() => ({ mountEngine: vi.fn() }));
vi.mock("@/lib/avatar-preview/engine", () => engine);

function character(): Character {
  return {
    setComposition: vi.fn(),
    resetVisual: vi.fn(),
    stopVisualEffect: vi.fn(),
    destroy: vi.fn(),
    bounceOnce: vi.fn(),
    burstOnce: vi.fn(),
  };
}

const value: Composition = {
  eye: "happy", pose: "idle", playing: false, amplitude: 0,
  motion_limit_px: 0, reduced_motion: false,
};

describe("Avatar 引擎生命周期桥", () => {
  beforeEach(() => engine.mountEngine.mockReset());

  it("Strict Mode卸载迟到实例，只有存活宿主应用最新组合并在卸载时销毁", async () => {
    const instances: Character[] = [];
    engine.mountEngine.mockImplementation(async () => {
      const instance = character();
      instances.push(instance);
      await Promise.resolve();
      return instance;
    });
    const onReady = vi.fn();
    const view = render(
      <StrictMode><Avatar value={value} size={180} generation={0} onReady={onReady} /></StrictMode>,
    );

    await act(async () => { await Promise.resolve(); });
    expect(instances).toHaveLength(2);
    const live = instances[1];
    expect(live.setComposition).toHaveBeenCalledWith(value);
    expect(instances[0].destroy).toHaveBeenCalledTimes(1);
    expect(live.destroy).not.toHaveBeenCalled();
    view.unmount();
    expect(live.destroy).toHaveBeenCalledTimes(1);
  });

  it("generation只reset视觉后重放当前composition", async () => {
    const instance = character();
    engine.mountEngine.mockResolvedValue(instance);
    const view = render(<Avatar value={value} size={180} generation={0} />);
    await act(async () => { await Promise.resolve(); });
    view.rerender(<Avatar value={value} size={180} generation={1} />);
    expect(instance.resetVisual).toHaveBeenCalledTimes(1);
    expect(instance.setComposition).toHaveBeenCalledTimes(2);
  });

  it("卸载后到达的异步实例立即销毁且不触发ready", async () => {
    let resolveMount!: (instance: Character) => void;
    engine.mountEngine.mockReturnValue(new Promise<Character>((resolve) => { resolveMount = resolve; }));
    const onReady = vi.fn();
    const view = render(<Avatar value={value} size={180} generation={0} onReady={onReady} />);
    view.unmount();
    const late = character();
    await act(async () => { resolveMount(late); await Promise.resolve(); });
    expect(late.destroy).toHaveBeenCalledTimes(1);
    expect(late.setComposition).not.toHaveBeenCalled();
    expect(onReady).not.toHaveBeenCalledWith(late);
  });

  it("引擎失败显示下一步并由用户选择重试", async () => {
    const first = new Error("静态角色资源加载失败");
    engine.mountEngine.mockRejectedValueOnce(first);
    engine.mountEngine.mockResolvedValueOnce(character());
    render(<Avatar value={value} size={180} generation={0} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("静态角色资源加载失败");
    await act(async () => { screen.getByRole("button", { name: "重试加载角色" }).click(); });
    expect(engine.mountEngine).toHaveBeenCalledTimes(2);
  });
});
