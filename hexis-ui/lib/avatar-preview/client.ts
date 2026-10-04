import { z } from "zod";
import { EMOTIONS, WORDSET_VERSION } from "./contracts";
import type { Identity } from "./contracts";

const identitySchema = z.object({
  session_id: z.string().uuid(),
  input_turn_id: z.string().uuid(),
  input_order: z.number().int().nonnegative(),
  stimulus_revision: z.number().int().nonnegative(),
}).strict();
const windowSchema = z.discriminatedUnion("status", [
  z.object({ status: z.literal("open"), owner: z.enum(["local", "service"]), decision_seq: z.literal(0) }).strict(),
  z.object({ status: z.literal("accepted"), owner: z.enum(["local", "service"]), decision_seq: z.number().int().positive() }).strict(),
  z.object({ status: z.literal("closed"), owner: z.enum(["local", "service"]), decision_seq: z.number().int().positive(), reason: z.enum(["model_timeout", "total_deadline"]) }).strict(),
]);
const common = {
  identity: identitySchema,
  wordset_version: z.literal(WORDSET_VERSION),
  emotion_source: z.literal("real"),
  context_source: z.enum(["fixture", "db"]),
  logical_call_count: z.number().int().min(0).max(1).optional(),
  transport_attempts: z.number().int().nonnegative().nullable().optional(),
  visual_window: windowSchema.optional(),
  raw_business_value: z.unknown().optional(),
  rejection_reason: z.string().optional(),
  stage: z.string().optional(),
  transport_status: z.enum(["unknown"]).optional(),
  late_diagnostics: z.array(z.record(z.unknown())).optional(),
  cleanup_diagnostics: z.array(z.record(z.unknown())).optional(),
  visual_owner: z.literal("service").optional(),
};
const resultUnion = z.discriminatedUnion("status", [
  z.object({ ...common, status: z.literal("valid"), label: z.enum(EMOTIONS), visual_window: windowSchema }).strict(),
  z.object({ ...common, status: z.literal("error"), error_code: z.string().min(1), message: z.string().min(1) }).strict(),
  z.object({ ...common, status: z.literal("pending"), visual_owner: z.literal("service"), visual_window: z.object({
    status: z.literal("open"), owner: z.literal("service"), decision_seq: z.literal(0),
  }).strict() }).strict(),
  z.object({ status: z.literal("unknown"), message: z.string().min(1) }).strict(),
]);
export const evaluationResultSchema = resultUnion.superRefine((value, ctx) => {
  if (value.status === "unknown") return;
  if (value.visual_window && value.visual_window.owner !== "service") {
    ctx.addIssue({ code: "custom", message: "资格事实必须来自服务端" });
  }
  if (value.status === "valid" && (value.visual_window.status !== "accepted"
    || value.visual_window.owner !== "service" || value.visual_window.decision_seq !== 1)) {
    ctx.addIssue({ code: "custom", message: "合法结果缺少匹配的服务端accepted资格" });
  }
  if (value.status === "valid" && value.logical_call_count !== 1) {
    ctx.addIssue({ code: "custom", message: "合法结果的逻辑评估次数必须为1" });
  }
  if (value.transport_attempts !== undefined
    && value.transport_attempts !== null && value.transport_attempts < 1) {
    ctx.addIssue({ code: "custom", message: "传输尝试次数只接受正数或未知" });
  }
  if (value.status === "error") {
    const closes = value.error_code === "ModelEvaluationTimeout" || value.error_code === "TotalDeadlineExceeded";
    if (closes && (value.visual_window?.status !== "closed"
      || value.visual_window.reason !== (value.error_code === "ModelEvaluationTimeout" ? "model_timeout" : "total_deadline"))) {
      ctx.addIssue({ code: "custom", message: "超时错误缺少匹配的结构化关闭事实" });
    }
    if (!closes && value.visual_window?.status === "closed") {
      ctx.addIssue({ code: "custom", message: "普通错误不能关闭视觉资格" });
    }
  }
});
export type EvaluationResult = z.infer<typeof evaluationResultSchema>;
export const serviceStatusSchema = z.object({
  configured: z.boolean(), provider: z.string().nullable(), model: z.string().nullable(),
  credential_available: z.boolean(), context_sources: z.object({ fixture: z.boolean(), db: z.boolean() }).strict(),
  wordset_version: z.string(), prompt_version: z.string(), business_budget: z.literal("none"), message: z.string(),
}).strict();
export type ServiceStatus = z.infer<typeof serviceStatusSchema>;

export class UnknownEvaluationError extends Error {
  constructor(message = "请求已断开，评估结果未知；查询原输入，不要重新发送") { super(message); this.name = "UnknownEvaluationError"; }
}

function sameIdentity(a: Identity, b: Identity): boolean {
  return a.session_id === b.session_id && a.input_turn_id === b.input_turn_id
    && a.input_order === b.input_order && a.stimulus_revision === b.stimulus_revision;
}

async function readJson(response: Response): Promise<unknown> {
  try { return await response.json(); } catch { throw new Error("预览服务返回了无效JSON；请查询原输入的评估记录"); }
}

function parseResult(body: unknown): EvaluationResult {
  const result = evaluationResultSchema.safeParse(body);
  if (!result.success) throw new UnknownEvaluationError("评估已提交，但响应契约无法确认；视觉资格保持原状，请查询原输入");
  return result.data;
}

export async function requestEmotion(identity: Identity, message: string, context_source: "fixture" | "db", signal: AbortSignal): Promise<EvaluationResult> {
  let response: Response;
  try {
    response = await fetch("/api/avatar-preview/evaluate", {
      method: "POST", headers: { "Content-Type": "application/json" }, signal,
      body: JSON.stringify({ identity, message, context_source }),
    });
  } catch { throw new UnknownEvaluationError(); }
  if (response.status >= 500) throw new UnknownEvaluationError("预览服务连接失败，服务端结果未知；查询原输入，不要重新发送");
  let body: unknown;
  try { body = await readJson(response); }
  catch { throw new UnknownEvaluationError("已提交请求但响应无法读取，服务端结果未知；查询原输入，不要重新发送"); }
  if (!response.ok) {
    const messageText = typeof body === "object" && body !== null && "message" in body && typeof body.message === "string"
      ? body.message : "评估请求未完成；可查询原输入的评估记录，勿重新发送";
    throw new Error(messageText);
  }
  const result = parseResult(body);
  if (result.status !== "unknown" && (!sameIdentity(result.identity, identity) || result.context_source !== context_source)) {
    throw new UnknownEvaluationError("服务端评估身份或上下文来源与本次请求不匹配，结果未知；请查询原输入，不要重新发送");
  }
  return result;
}

export async function queryEmotion(identity: Identity, context_source: "fixture" | "db"): Promise<EvaluationResult> {
  const path = `/api/avatar-preview/evaluations/${encodeURIComponent(identity.session_id)}/${encodeURIComponent(identity.input_turn_id)}/${identity.stimulus_revision}`;
  let response: Response;
  try { response = await fetch(path, { cache: "no-store" }); }
  catch { throw new UnknownEvaluationError("查询连接已断开；评估仍为未知，可稍后再次查询原输入") ; }
  if (response.status >= 500) throw new UnknownEvaluationError("查询服务暂不可用；评估仍未知，可稍后再次查询原输入");
  const body = await readJson(response);
  const result = parseResult(body);
  if (!(response.status === 200 || (response.status === 404 && result.status === "unknown"))) {
    throw new UnknownEvaluationError("查询未返回可确认的评估结果；稍后查询原输入");
  }
  if (result.status !== "unknown" && !sameIdentity(result.identity, identity)) {
    throw new UnknownEvaluationError("查询结果身份与当前输入不匹配，状态未知；稍后查询原输入");
  }
  if (result.status !== "unknown" && result.context_source !== context_source) {
    throw new UnknownEvaluationError("查询结果上下文来源与原请求不匹配，状态未知；稍后查询原输入");
  }
  return result;
}

export async function getPreviewServiceStatus(): Promise<ServiceStatus> {
  let response: Response;
  try { response = await fetch("/api/avatar-preview/status", { cache: "no-store" }); }
  catch { throw new Error("预览服务状态不可用；检查明确选择的服务地址后重试"); }
  if (response.status >= 500) throw new Error("预览服务状态暂不可用；离线预览仍可继续");
  let body: unknown;
  try { body = await readJson(response); } catch { throw new Error("预览服务返回无效状态；离线预览仍可继续"); }
  const parsed = serviceStatusSchema.safeParse(body);
  if (!response.ok || !parsed.success) throw new Error("预览服务状态契约无效；离线预览仍可继续");
  return parsed.data;
}
