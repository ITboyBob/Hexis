import { describe, expect, it } from "vitest";
import { eventSchema, parseInjected, visualWindowSchema } from "./validation";

const identity = {
  session_id: "session-a", input_turn_id: "turn-a", input_order: 1, stimulus_revision: 1,
};
const event = (payload: Record<string, unknown>) => ({
  event_id: "event-a", session_id: "session-a", stream_id: "manual", producer_seq: 1, payload,
});

describe("Avatar预览运行时事件校验", () => {
  it("校验结构化视觉窗口所有者、终态与裁定序号", () => {
    expect(visualWindowSchema.parse({ status: "open", owner: "local", decision_seq: 0 })).toMatchObject({ status: "open" });
    expect(visualWindowSchema.parse({ status: "accepted", owner: "service", decision_seq: 1 })).toMatchObject({ status: "accepted" });
    expect(visualWindowSchema.parse({ status: "closed", owner: "service", decision_seq: 1, reason: "total_deadline" })).toMatchObject({ status: "closed" });
    expect(() => visualWindowSchema.parse({ status: "open", owner: "local", decision_seq: -1 })).toThrow();
    expect(() => visualWindowSchema.parse({ status: "closed", owner: "local", decision_seq: 1 })).toThrow();
    expect(() => visualWindowSchema.parse({ status: "accepted", owner: "local", decision_seq: 1, reason: "model_timeout" })).toThrow();
  });

  it("接受与identity一致且含owner的input、emotion和close事实", () => {
    expect(eventSchema.parse(event({ type: "input_committed", identity, visual_owner: "local" }))).toBeDefined();
    expect(eventSchema.parse(event({
      type: "emotion", identity, wordset_version: "v1", status: "valid", label: "neutral", source: "manual",
      visual_window: { status: "accepted", owner: "local", decision_seq: 1 },
    }))).toBeDefined();
    expect(eventSchema.parse(event({
      type: "visual_window", identity,
      fact: { status: "closed", owner: "service", decision_seq: 1, reason: "model_timeout" },
    }))).toBeDefined();
  });

  it("拒绝错session、缺少valid标签和缺少控制参数", () => {
    expect(() => eventSchema.parse(event({ type: "input_committed", identity: { ...identity, session_id: "other" } }))).toThrow();
    expect(() => eventSchema.parse(event({ type: "emotion", identity, wordset_version: "v1", status: "valid", source: "manual" }))).toThrow();
    expect(() => eventSchema.parse(event({ type: "control", action: "stop_effect" }))).toThrow();
  });

  it("手工注入拒绝真实来源和伪造服务端资格事实", () => {
    expect(() => parseInjected(JSON.stringify(event({
      type: "task", task: { activity_id: "a", task_id: "a", kind: "tool", foreground: true, phase: "running", source: "real" },
    })))).toThrow("不能伪装真实来源");
    expect(() => parseInjected(JSON.stringify(event({ type: "input_committed", identity, visual_owner: "service" })))).toThrow("不能指定资格所有者");
    expect(() => parseInjected(JSON.stringify(event({
      type: "emotion", identity, wordset_version: "v1", status: "valid", label: "happy", source: "simulated",
      visual_window: { status: "accepted", owner: "service", decision_seq: 1 },
    })))).toThrow("不能伪造视觉资格事实");
  });

  it("坏JSON可恢复并拒绝注入服务端视觉裁定", () => {
    expect(() => parseInjected("{" )).toThrow("JSON");
    expect(() => parseInjected("{}" )).toThrow("必需字段");
    expect(() => parseInjected(JSON.stringify(event({
      type: "visual_window", identity,
      fact: { status: "closed", owner: "service", decision_seq: 1, reason: "model_timeout" },
    })))).toThrow("不能注入服务端资格事实");
  });
});
