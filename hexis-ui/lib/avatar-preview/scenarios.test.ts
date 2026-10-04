import { describe, expect, it } from "vitest";
import { sameValueUpdates, scenarios, transitions } from "./scenarios";
import type { Identity } from "./contracts";

const base: Identity = {
  session_id: "session-test",
  input_turn_id: "turn-test",
  input_order: 1,
  stimulus_revision: 1,
};

describe("预览固定场景矩阵", () => {
  it("生成182个唯一有序不同标签转换和14个同值更新", () => {
    expect(transitions).toHaveLength(182);
    expect(new Set(transitions.map(({ from, to }) => `${from}->${to}`)).size).toBe(182);
    expect(transitions.every(({ from, to }) => from !== to)).toBe(true);
    expect(sameValueUpdates).toHaveLength(14);
    expect(sameValueUpdates.every(({ from, to }) => from === to)).toBe(true);
  });

  it("保留C01-C11各自的事件或明确的外部调用限制", () => {
    expect(scenarios.slice(0, 11).map(({ id }) => id)).toEqual(Array.from({ length: 11 }, (_, i) => `C${String(i + 1).padStart(2, "0")}`));
    expect(scenarios.map(({ id }) => id)).toHaveLength(new Set(scenarios.map(({ id }) => id)).size);
    for (const scenario of scenarios) {
      expect(scenario.build(base).length, scenario.id).toBeGreaterThan(0);
    }
    expect(scenarios.find(({ id }) => id === "C11")?.limitation).toMatch(/显式配置/);
  });

  it("C08覆盖销毁后晚到回调，C10覆盖来源混合和坏数据", () => {
    expect(scenarios.find(({ id }) => id === "C08")?.build(base)).toEqual(expect.arrayContaining([
      expect.objectContaining({ kind: "lifecycle", action: "destroy" }),
      expect.objectContaining({ kind: "lifecycle", action: "late_model_result" }),
    ]));
    expect(scenarios.find(({ id }) => id === "C10")?.build(base)).toEqual(expect.arrayContaining([
      expect.objectContaining({ kind: "invalid_payload" }),
    ]));
  });

  it("C12通过本地资格宿主触发超时，不伪造视觉资格事件", () => {
    const steps = scenarios.find(({ id }) => id === "C12")?.build(base) ?? [];
    expect(steps).toContainEqual(expect.objectContaining({ kind: "local_open", identity: base, timeout_ms: 1 }));
    expect(steps).toContainEqual(expect.objectContaining({ kind: "local_timeout", identity: base }));
    expect(steps).not.toContainEqual(expect.objectContaining({ kind: "event", payload: expect.objectContaining({ type: "visual_window" }) }));
  });
});
