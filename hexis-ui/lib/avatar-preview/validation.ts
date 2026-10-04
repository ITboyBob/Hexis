import { z } from "zod";
import { EMOTIONS } from "./contracts";

const sourceSchema = z.enum(["manual", "simulated", "real"]);
export const identitySchema = z.object({
  session_id: z.string().min(1),
  input_turn_id: z.string().min(1),
  input_order: z.number().int().nonnegative(),
  stimulus_revision: z.number().int().nonnegative(),
}).strict();

const visualWindowOwnerSchema = z.enum(["local", "service"]);
export const visualWindowSchema = z.union([
  z.object({ status: z.literal("open"), owner: visualWindowOwnerSchema, decision_seq: z.literal(0) }).strict(),
  z.object({ status: z.literal("accepted"), owner: visualWindowOwnerSchema, decision_seq: z.number().int().positive() }).strict(),
  z.object({
    status: z.literal("closed"), owner: visualWindowOwnerSchema,
    decision_seq: z.number().int().positive(), reason: z.enum(["model_timeout", "total_deadline"]),
  }).strict(),
]);

const taskSchema = z.object({
  activity_id: z.string().min(1),
  task_id: z.string().nullable(),
  kind: z.string().min(1),
  foreground: z.boolean(),
  phase: z.enum(["queued", "running", "waiting_user", "cancelling", "unknown", "completed", "failed", "cancelled"]),
  source: sourceSchema,
}).strict();
const playbackSchema = z.object({
  playback_id: z.string().min(1),
  output_epoch: z.number().int().nonnegative(),
  response_id: z.string().min(1).optional(),
  media_session_id: z.string().min(1).optional(),
  phase: z.enum(["playing", "buffering", "drained", "cancel", "error"]),
  progress: z.number().finite().min(0),
  played_ms: z.number().finite().nonnegative(),
  interval_ms: z.number().finite().nonnegative(),
  amplitude: z.number().finite().min(0).max(1),
  source: sourceSchema,
}).strict();

const controlVariants = [
  z.object({ type: z.literal("control"), action: z.literal("reset") }).strict(),
  z.object({ type: z.literal("control"), action: z.literal("destroy") }).strict(),
  z.object({ type: z.literal("control"), action: z.literal("reduced_motion"), enabled: z.boolean() }).strict(),
  z.object({ type: z.literal("control"), action: z.literal("stop_effect"), effect_id: z.string().min(1) }).strict(),
] as const;

const payloadSchema = z.union([
  z.object({ type: z.literal("input_committed"), identity: identitySchema, visual_owner: visualWindowOwnerSchema.optional() }).strict(),
  z.object({
    type: z.literal("emotion"), identity: identitySchema, wordset_version: z.string().min(1),
    status: z.enum(["pending", "valid", "error"]), label: z.enum(EMOTIONS).optional(),
    source: sourceSchema, reason: z.string().optional(), visual_window: visualWindowSchema.optional(),
  }).strict().superRefine((value, ctx) => {
    if (value.status === "valid" && value.label === undefined) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: "valid情绪必须包含词表标签", path: ["label"] });
    }
  }),
  z.object({ type: z.literal("visual_window"), identity: identitySchema, fact: visualWindowSchema }).strict(),
  z.object({ type: z.literal("input_activity"), activity_id: z.string().min(1), speaking: z.boolean(), source: sourceSchema }).strict(),
  z.object({ type: z.literal("task"), task: taskSchema }).strict(),
  z.object({ type: z.literal("playback_open"), playback_id: z.string().min(1), output_epoch: z.number().int().nonnegative(), source: sourceSchema }).strict(),
  z.object({ type: z.literal("playback"), feedback: playbackSchema }).strict(),
  ...controlVariants,
]);

export const eventSchema = z.object({
  event_id: z.string().min(1),
  session_id: z.string().min(1),
  stream_id: z.string().min(1),
  producer_seq: z.number().int().nonnegative(),
  payload: payloadSchema,
}).strict().superRefine((event, ctx) => {
  const { payload } = event;
  const identity = payload.type === "input_committed" || payload.type === "emotion" || payload.type === "visual_window"
    ? payload.identity
    : undefined;
  if (identity && identity.session_id !== event.session_id) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, message: "事件会话与输入身份不一致", path: ["payload", "identity", "session_id"] });
  }
});

export type ValidatedEvent = z.infer<typeof eventSchema>;

export function parseInjected(text: string): ValidatedEvent {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    throw new Error("JSON格式无效；修正语法后可重新注入");
  }
  let event: ValidatedEvent;
  try {
    event = eventSchema.parse(raw);
  } catch {
    throw new Error("事件内容无效；检查必需字段、字段类型和当前会话后重试");
  }
  const payload = event.payload;
  const origin = payload.type === "task" ? payload.task.source
    : payload.type === "playback" ? payload.feedback.source
      : "source" in payload ? payload.source : undefined;
  if (origin === "real") throw new Error("事件编辑器不能伪装真实来源；请选择手工或模拟来源");
  if (payload.type === "input_committed" && payload.visual_owner !== undefined) {
    throw new Error("资格所有者由本地宿主绑定，事件编辑器不能指定资格所有者");
  }
  if (payload.type === "emotion" && payload.visual_window !== undefined) {
    throw new Error("视觉资格事实由宿主裁定；事件编辑器不能伪造视觉资格事实");
  }
  if (payload.type === "visual_window") throw new Error("事件编辑器不能注入服务端资格事实");
  return event;
}
