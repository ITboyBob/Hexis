import type { Effect } from "./effects";
import { EMOTIONS } from "./contracts";
import type { Emotion, Identity, Payload } from "./contracts";

export type ScenarioStep =
  | { kind: "event"; payload: Payload }
  | { kind: "effect_enqueue"; effect: Effect }
  | { kind: "lifecycle"; action: "destroy" | "late_callback" | "late_model_result" }
  | { kind: "invalid_payload"; raw: string }
  | { kind: "local_open"; identity: Identity; timeout_ms: number }
  | { kind: "local_timeout"; identity: Identity; timeout_ms: number }
  | { kind: "service_evaluation"; logical_calls: 1 };

export type PreviewScenario = {
  id: `C${string}`;
  title: string;
  limitation?: string;
  build(identity: Identity): ScenarioStep[];
};

export const transitions: ReadonlyArray<{ from: Emotion; to: Emotion }> = EMOTIONS.flatMap((from) =>
  EMOTIONS.filter((to) => to !== from).map((to) => ({ from, to })),
);

export const sameValueUpdates: ReadonlyArray<{ from: Emotion; to: Emotion }> =
  EMOTIONS.map((emotion) => ({ from: emotion, to: emotion }));

const identityAt = (identity: Identity, order: number, turn: string): Identity => ({
  ...identity,
  input_order: order,
  input_turn_id: turn,
  stimulus_revision: 1,
});
const event = (payload: Payload): ScenarioStep => ({ kind: "event", payload });
const task = (activity_id: string, kind: string, foreground: boolean, phase: "running" | "completed" = "running", source: "manual" | "simulated" = "simulated"): ScenarioStep => event({
  type: "task",
  task: { activity_id, task_id: activity_id, kind, foreground, phase, source },
});
const input = (identity: Identity): ScenarioStep => event({ type: "input_committed", identity });
const playback = (playback_id: string, phase: "playing" | "buffering" | "drained", progress: number, amplitude: number): ScenarioStep => event({
  type: "playback",
  feedback: { playback_id, output_epoch: 1, phase, progress, played_ms: Math.round(progress * 1000), interval_ms: 0, amplitude, source: "simulated" },
});

export const scenarios: readonly PreviewScenario[] = [
  {
    id: "C01", title: "happy、倾听评估、两工具、后台提示与播放",
    build: (identity) => [
      input(identity),
      event({ type: "emotion", identity, wordset_version: "v1", status: "pending", source: "simulated" }),
      event({ type: "emotion", identity, wordset_version: "v1", status: "valid", label: "happy", source: "simulated" }),
      event({ type: "input_activity", activity_id: "voice-c01", speaking: true, source: "simulated" }),
      task("tool-a", "tool", false), task("tool-b", "tool", false), task("notification-a", "notification", false),
      event({ type: "playback_open", playback_id: "play-c01", output_epoch: 1, source: "simulated" }),
      playback("play-c01", "playing", 0.3, 0.6),
    ],
  },
  {
    id: "C02", title: "两个工具中仅结束指定实例",
    build: () => [task("tool-a", "tool", true), task("tool-b", "tool", true), task("tool-a", "tool", true, "completed")],
  },
  {
    id: "C03", title: "新输入pending/error后拒绝旧输入valid晚到",
    build: (base) => {
      const older = identityAt(base, base.input_order, `${base.input_turn_id}-old`);
      const newer = identityAt(base, base.input_order + 1, `${base.input_turn_id}-new`);
      return [input(older), event({ type: "emotion", identity: older, wordset_version: "v1", status: "valid", label: "sad", source: "simulated" }),
        input(newer), event({ type: "emotion", identity: newer, wordset_version: "v1", status: "pending", source: "simulated" }),
        event({ type: "emotion", identity: newer, wordset_version: "v1", status: "error", source: "simulated", reason: "模拟评估失败；最近有效情绪保留" }),
        event({ type: "emotion", identity: older, wordset_version: "v1", status: "valid", label: "happy", source: "simulated" })];
    },
  },
  {
    id: "C04", title: "播放缓冲、恢复与终止后拒绝旧进度",
    build: () => [
      event({ type: "playback_open", playback_id: "play-c04", output_epoch: 1, source: "simulated" }),
      playback("play-c04", "playing", 0.2, 0.8),
      playback("play-c04", "buffering", 0.2, 0.8),
      playback("play-c04", "playing", 0.4, 0.4),
      playback("play-c04", "drained", 1, 0),
      playback("play-c04", "playing", 0.5, 0.8),
    ],
  },
  {
    id: "C05", title: "新播放epoch拒绝旧epoch进度",
    build: () => [
      event({ type: "playback_open", playback_id: "play-new", output_epoch: 2, source: "simulated" }),
      event({ type: "playback_open", playback_id: "play-old", output_epoch: 1, source: "simulated" }),
      playback("play-old", "playing", 0.5, 1),
    ],
  },
  {
    id: "C06", title: "停止效果A并保留排队效果B",
    build: () => [
      { kind: "effect_enqueue", effect: { id: "effect-a", kind: "bounce", duration_ms: 250 } },
      { kind: "effect_enqueue", effect: { id: "effect-b", kind: "burst", duration_ms: 250 } },
      event({ type: "control", action: "stop_effect", effect_id: "effect-a" }),
    ],
  },
  {
    id: "C07", title: "reset后原视觉回调晚到",
    build: (identity) => [input(identity), event({ type: "control", action: "reset" }), { kind: "lifecycle", action: "late_callback" }],
  },
  {
    id: "C08", title: "销毁/卸载后模型、资源、RAF和播放器回调晚到",
    build: (identity) => [input(identity), event({ type: "control", action: "destroy" }), { kind: "lifecycle", action: "destroy" }, { kind: "lifecycle", action: "late_model_result" }],
  },
  {
    id: "C09", title: "运行时切换减少动态且保留业务事实",
    build: (identity) => [input(identity), event({ type: "control", action: "reduced_motion", enabled: true }),
      event({ type: "control", action: "reduced_motion", enabled: false })],
  },
  {
    id: "C10", title: "混合手工/模拟来源并恢复坏payload",
    build: (identity) => [input(identity), task("manual-task", "tool", true, "running", "manual"), task("simulated-task", "tool", false),
      { kind: "invalid_payload", raw: "{\"type\":\"emotion\",\"status\":\"valid\",\"source\":\"real\"}" }],
  },
  {
    id: "C11", title: "逻辑评估与底层传输重试边界",
    limitation: "需显式配置预览服务后由用户发起一次逻辑评估；页面不会自动请求。",
    build: () => [{ kind: "service_evaluation", logical_calls: 1 }],
  },
  {
    id: "C12", title: "本地截止先裁定后记录晚到结果",
    build: (identity) => [{ kind: "local_open", identity, timeout_ms: 1 }, input(identity),
      { kind: "local_timeout", identity, timeout_ms: 1 },
      event({ type: "emotion", identity, wordset_version: "v1", status: "valid", label: "happy", source: "simulated", reason: "截止后收到结果" })],
  },
];

export function scenarioById(id: string): PreviewScenario | undefined {
  return scenarios.find((scenario) => scenario.id === id);
}
