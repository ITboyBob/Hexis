import { afterEach, describe, expect, it, vi } from "vitest";
import { evaluationResultSchema, queryEmotion, requestEmotion, UnknownEvaluationError } from "./client";

const identity = { session_id: "00000000-0000-4000-8000-000000000001", input_turn_id: "00000000-0000-4000-8000-000000000002", input_order: 1, stimulus_revision: 1 };

describe("预览评估客户端", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("一次POST携带身份、消息和来源并校验严格成功契约", async () => {
    const fetcher = vi.fn().mockResolvedValue(Response.json({ identity, status: "valid", label: "curious", raw_business_value: "curious", wordset_version: "v1", emotion_source: "real", context_source: "fixture", logical_call_count: 1, transport_attempts: null, visual_window: { status: "accepted", owner: "service", decision_seq: 1 } }));
    vi.stubGlobal("fetch", fetcher);
    const result = await requestEmotion(identity, "hello", "fixture", new AbortController().signal);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][0]).toBe("/api/avatar-preview/evaluate");
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ identity, message: "hello", context_source: "fixture" });
    expect(result).toMatchObject({ status: "valid", visual_window: { status: "accepted" } });
  });

  it("只按原完整身份查询，404 unknown不重放POST", async () => {
    const fetcher = vi.fn().mockResolvedValue(Response.json({ status: "unknown", message: "不会自动重新调用模型" }, { status: 404 }));
    vi.stubGlobal("fetch", fetcher);
    expect(await queryEmotion(identity, "fixture")).toMatchObject({ status: "unknown", message: "不会自动重新调用模型" });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][0]).toBe(`/api/avatar-preview/evaluations/${identity.session_id}/${identity.input_turn_id}/1`);
  });

  it("保留服务端超时关闭事实，不把普通error合成为超时", async () => {
    const timeout = { identity, status: "error", wordset_version: "v1", emotion_source: "real", context_source: "fixture", error_code: "ModelEvaluationTimeout", stage: "model", logical_call_count: 1, transport_attempts: null, transport_status: "unknown", message: "超时", visual_window: { status: "closed", owner: "service", decision_seq: 1, reason: "model_timeout" }, late_diagnostics: [] };
    const fetcher = vi.fn().mockResolvedValueOnce(Response.json(timeout)).mockResolvedValueOnce(Response.json({ identity: { ...identity, input_order: 2 }, status: "error", wordset_version: "v1", emotion_source: "real", context_source: "fixture", error_code: "ContextSourceUnavailable", stage: "context", logical_call_count: 0, transport_attempts: null, message: "来源不可用" }));
    vi.stubGlobal("fetch", fetcher);
    expect(await requestEmotion(identity, "hello", "fixture", new AbortController().signal)).toMatchObject({ status: "error", visual_window: { status: "closed" } });
    expect(await requestEmotion({ ...identity, input_order: 2 }, "hello", "fixture", new AbortController().signal)).toMatchObject({ status: "error", logical_call_count: 0 });
  });

  it("拒绝身份/上下文漂移、错误owner、缺标签与异常计数", () => {
    const valid = { identity, status: "valid", label: "curious", raw_business_value: "curious", wordset_version: "v1", emotion_source: "real", context_source: "fixture", logical_call_count: 1, transport_attempts: null, visual_window: { status: "accepted", owner: "service", decision_seq: 1 } };
    expect(evaluationResultSchema.safeParse(valid).success).toBe(true);
    expect(evaluationResultSchema.safeParse({ ...valid, identity: { ...identity, input_order: 2 } }).success).toBe(true); // 与请求身份的比较发生在客户端边界
    expect(evaluationResultSchema.safeParse({ ...valid, context_source: "db" }).success).toBe(true); // 与请求来源的比较发生在客户端边界
    expect(evaluationResultSchema.safeParse({ ...valid, wordset_version: "v0" }).success).toBe(false);
    expect(evaluationResultSchema.safeParse({ ...valid, label: undefined }).success).toBe(false);
    expect(evaluationResultSchema.safeParse({ ...valid, visual_window: { status: "accepted", owner: "local", decision_seq: 1 } }).success).toBe(false);
    expect(evaluationResultSchema.safeParse({ ...valid, logical_call_count: 0 }).success).toBe(false);
    expect(evaluationResultSchema.safeParse({ ...valid, transport_attempts: 0 }).success).toBe(false);
    expect(evaluationResultSchema.safeParse({ ...valid, identity: { ...identity, stimulus_revision: -1 } }).success).toBe(false);
  });

  it("pending只允许服务端open；超时error必须携带同原因closed事实", () => {
    const pending = { identity, status: "pending", wordset_version: "v1", emotion_source: "real", context_source: "fixture", visual_owner: "service", visual_window: { status: "open", owner: "service", decision_seq: 0 } };
    expect(evaluationResultSchema.safeParse(pending).success).toBe(true);
    expect(evaluationResultSchema.safeParse({ ...pending, visual_window: { ...pending.visual_window, status: "closed", decision_seq: 1, reason: "model_timeout" } }).success).toBe(false);
    const timeout = { identity, status: "error", wordset_version: "v1", emotion_source: "real", context_source: "fixture", error_code: "TotalDeadlineExceeded", stage: "context_read", logical_call_count: 0, transport_attempts: null, message: "超时", visual_window: { status: "closed", owner: "service", decision_seq: 1, reason: "total_deadline" } };
    expect(evaluationResultSchema.safeParse(timeout).success).toBe(true);
    expect(evaluationResultSchema.safeParse({ ...timeout, visual_window: { ...timeout.visual_window, reason: "model_timeout" } }).success).toBe(false);
  });

  it("查询只接受200结果或404 unknown，500中伪装的valid仍为unknown", async () => {
    const fetcher = vi.fn().mockResolvedValue(Response.json({ identity, status: "valid", label: "curious", raw_business_value: "curious", wordset_version: "v1", emotion_source: "real", context_source: "fixture", logical_call_count: 1, transport_attempts: null, visual_window: { status: "accepted", owner: "service", decision_seq: 1 } }, { status: 500 }));
    vi.stubGlobal("fetch", fetcher);
    await expect(queryEmotion(identity, "fixture")).rejects.toThrow(/未知/);
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it("请求比较完整身份与原上下文来源", async () => {
    const mismatch = { identity: { ...identity, stimulus_revision: 2 }, status: "valid", label: "curious", raw_business_value: "curious", wordset_version: "v1", emotion_source: "real", context_source: "fixture", logical_call_count: 1, transport_attempts: null, visual_window: { status: "accepted", owner: "service", decision_seq: 1 } };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(mismatch)));
    await expect(requestEmotion(identity, "hello", "fixture", new AbortController().signal)).rejects.toBeInstanceOf(UnknownEvaluationError);
    const changedSource = { ...mismatch, identity, context_source: "db" };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(changedSource)));
    await expect(requestEmotion(identity, "hello", "fixture", new AbortController().signal)).rejects.toBeInstanceOf(UnknownEvaluationError);
  });
});
