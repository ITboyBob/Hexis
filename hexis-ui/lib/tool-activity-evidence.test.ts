// @vitest-environment node
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import ts from "typescript";
import { describe, expect, it, vi } from "vitest";

// 只执行 spec 的纯证据/导航 helper，避免加载 Playwright 注册、manifest、DB 或 Provider。
const source = readFileSync(new URL("../e2e/tool-activity.spec.ts", import.meta.url), "utf8");
const names = ["digest", "rawToolEvents", "caseToolEvidence", "ensureActivityExpanded", "navigateToTask", "rememberPhaseEvents", "rememberLifecycleEvidence", "accumulateActivityPage", "failureCaseRecord", "captureCaseArtifact", "originalChatMessagesEvidence"];
const parsed = ts.createSourceFile("tool-activity.spec.ts", source, ts.ScriptTarget.Latest, true);
const helpers = parsed.statements.filter((node) => ts.isFunctionDeclaration(node) && names.includes(node.name?.text ?? ""));
const code = ts.transpileModule(helpers.map((node) => node.getText(parsed)).join("\n")
  + `\n({${names.join(",")}})`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;

function loadHelpers() {
  return runInNewContext(code, { URL, Error, createHash, Buffer }) as Record<string, (...args: any[]) => any>;
}

const row = (id: string, sessionId: string, tool: string, callId: string, eventType = "TOOL_START") => ({
  event_id: id, session_id: sessionId, event_type: eventType,
  payload: { tool_name: tool, call_id: callId }, stream_id: sessionId, seq: 1,
});

describe("真实工具 case 证据归属与页面适配", () => {
  it("Snapshot 原始消息必须声明所属 session_id 字符串，供 task session 隔离过滤", () => {
    const snapshot = parsed.statements.find((node): node is ts.TypeAliasDeclaration =>
      ts.isTypeAliasDeclaration(node) && node.name.text === "Snapshot");
    if (!snapshot || !ts.isTypeLiteralNode(snapshot.type)) throw new Error("Snapshot type declaration is missing");
    const messages = snapshot.type.members.find((node): node is ts.PropertySignature =>
      ts.isPropertySignature(node) && node.name.getText(parsed) === "chat_messages");
    if (!messages?.type || !ts.isArrayTypeNode(messages.type) || !ts.isTypeLiteralNode(messages.type.elementType)) throw new Error("Snapshot.chat_messages must declare its row fields");
    const session = messages.type.elementType.members.find((node): node is ts.PropertySignature =>
      ts.isPropertySignature(node) && node.name.getText(parsed) === "session_id");
    expect(session, "chat_messages requires session_id for task ownership filtering").toBeDefined();
    expect(session?.questionToken).toBeUndefined();
    expect(session?.type?.kind).toBe(ts.SyntaxKind.StringKeyword);
  });

  it("失败记录的 active spread 和状态字段只有一份，无重复关键字", () => {
    const helper = parsed.statements.find((node): node is ts.FunctionDeclaration =>
      ts.isFunctionDeclaration(node) && node.name?.text === "failureCaseRecord");
    const returned = helper?.body?.statements.find(ts.isReturnStatement)?.expression;
    if (!returned || !ts.isObjectLiteralExpression(returned)) throw new Error("failureCaseRecord must return an object literal");
    const spreads = returned.properties.filter(ts.isSpreadAssignment).map((node) => node.expression.getText(parsed));
    const assignments = returned.properties.filter(ts.isPropertyAssignment);
    const keys = assignments.map((node) => node.name.getText(parsed));
    expect(spreads).toEqual(["active"]);
    expect(keys).toEqual(["status", "reason", "details"]);
    expect(new Set(keys).size).toBe(keys.length);
    const state = assignments.find((node) => node.name.getText(parsed) === "status")!.initializer;
    expect(ts.isAsExpression(state) ? state.expression.getText(parsed) : state.getText(parsed)).toBe('"FAIL"');
  });

  it("已开始但未触发工具的 case 保留 FAIL 原因、WAV、task owner 与实际 frames", () => {
    const active = { name: "background-tools", sessionId: "origin", beganAt: "now", expectedTools: ["recall", "read_file"], details: { wav: { sha256: "asset-hash" }, task: { task_id: "task-a" } } };
    const frames = { received: [{ type: "provider.error" }], sent: [] };
    const failed = loadHelpers().failureCaseRecord(active, new Error("Provider did not trigger recall"), frames);
    expect(failed.status).toBe("FAIL");
    expect(failed.reason).toBe("Provider did not trigger recall");
    expect(failed.details.task.task_id).toBe("task-a");
    expect(failed.details.wav.sha256).toBe("asset-hash");
    expect(failed.details.browser_realtime_protocol).toEqual(frames);
    expect(active).not.toHaveProperty("status");
  });

  it("失败取证容许缺失 screenshot/DB，但记录读取错误；PASS 取证失败即抛错", async () => {
    const capture = loadHelpers().captureCaseArtifact;
    const errors: string[] = [];
    expect(await capture(async () => ({ raw: "actual evidence" }), "API", "FAIL", errors)).toEqual({ raw: "actual evidence" });
    expect(await capture(async () => { throw new Error("page closed"); }, "screenshot", "FAIL", errors)).toBeUndefined();
    expect(errors).toEqual(["screenshot: page closed"]);
    await expect(capture(async () => { throw new Error("DB unavailable"); }, "DB snapshot", "PASS", errors)).rejects.toThrow("DB unavailable");
  });

  it("跨页累计 request/result、重放按 event_id 去重，同时使用最新 cursor", () => {
    const start = row("start", "origin", "recall", "call-1");
    const result = { ...row("result", "origin", "recall", "call-1", "TOOL_RESULT"), seq: 2 };
    const accumulate = loadHelpers().accumulateActivityPage;
    const first = accumulate({ session_id: "origin", events: [], cursor: {}, has_more: false }, { session_id: "origin", events: [start], cursor: { origin: 1 }, has_more: true });
    const second = accumulate(first, { session_id: "origin", events: [start, result], cursor: { origin: 2 }, has_more: false });
    const empty = accumulate(second, { session_id: "origin", events: [], cursor: { origin: 3 }, has_more: false });
    expect(empty.events.map((event: { event_id: string }) => event.event_id)).toEqual(["start", "result"]);
    expect(empty.cursor).toEqual({ origin: 3 });
    expect(empty.has_more).toBe(false);
  });

  it.each(["wrong-page", "wrong-row"])("拒绝跨会话分页响应（%s）", (fault) => {
    expect(() => loadHelpers().accumulateActivityPage({ session_id: "origin", events: [] }, {
      session_id: fault === "wrong-page" ? "foreign" : "origin",
      events: [row("foreign", "foreign", "recall", "call-1")], cursor: {},
    })).toThrow("another session");
  });

  it("后台 IDs 来自 task Activity，保留 origin snapshot 和任务归属", () => {
    const snapshot = { origin_session_id: "origin", tasks: [{ id: "task-a", task_session_id: "task-a-session" }], events: [row("submit", "origin", "submit", "submit-call")] };
    const taskActivity = { session_id: "task-a-session", events: [
      row("recall-start", "task-a-session", "recall", "recall-call"),
      row("recall-result", "task-a-session", "recall", "recall-call", "TOOL_RESULT"),
      row("read-start", "task-a-session", "read_file", "read-call"),
      row("foreign", "task-b-session", "recall", "foreign-call"),
      row("other-tool", "task-a-session", "submit", "other-call"),
    ] };
    const evidence = loadHelpers().caseToolEvidence(snapshot, "origin", ["recall", "read_file"], taskActivity);
    expect(evidence.activitySessionId).toBe("task-a-session");
    expect(evidence.callIds).toEqual(["recall-call", "read-call"]);
    expect(evidence.eventIds).toEqual(["recall-start", "recall-result", "read-start"]);
    expect(snapshot.origin_session_id).toBe("origin");
    expect(snapshot.tasks[0].id).toBe("task-a");
  });

  it("前台继续从 origin snapshot 提取，不串入 task 事件", () => {
    const evidence = loadHelpers().caseToolEvidence({ events: [row("voice", "origin", "voice_recall", "voice-call"), row("task", "task-a", "voice_recall", "task-call")] }, "origin", ["voice_recall"]);
    expect(evidence.activitySessionId).toBe("origin");
    expect(evidence.callIds).toEqual(["voice-call"]);
    expect(evidence.eventIds).toEqual(["voice"]);
  });

  it("对比真实 origin chat_messages 原始字段，键顺序不影响摘要、content_blocks 变化会被发现", () => {
    const before = [{
      id: "message-1", session_id: "task-a", ordinal: 1, role: "assistant", content: "tool history",
      content_blocks: [{ type: "toolCall", id: "call-1", name: "read_file", arguments: { path: "README.md" } }],
      metadata: { source: "voice", version: 1 }, source_message_id: "source-1",
    }];
    const keyOrderChanged = [{
      source_message_id: "source-1", metadata: { version: 1, source: "voice" },
      content_blocks: [{ arguments: { path: "README.md" }, name: "read_file", id: "call-1", type: "toolCall" }],
      content: "tool history", role: "assistant", ordinal: 1, session_id: "task-a", id: "message-1",
    }];
    const compare = loadHelpers().originalChatMessagesEvidence;
    const unchanged = compare(before, keyOrderChanged);
    expect(unchanged.unchanged).toBe(true);
    expect(unchanged.before.sha256).toBe(unchanged.after.sha256);
    expect(unchanged.before.message_count).toBe(1);

    const mutated = compare(before, [{ ...keyOrderChanged[0], content_blocks: [{ type: "toolResult", content: "modified" }] }]);
    expect(mutated.unchanged).toBe(false);
    expect(mutated.before.sha256).not.toBe(mutated.after.sha256);
    const movedSession = compare(before, [{ ...keyOrderChanged[0], session_id: "task-b" }]);
    expect(movedSession.unchanged).toBe(false);
  });

  it("拒绝与 origin snapshot 无关的 task Activity", () => {
    expect(() => loadHelpers().caseToolEvidence({ tasks: [], events: [] }, "origin", ["recall"], { session_id: "foreign", events: [] })).toThrow("does not belong");
  });

  it.each(["false", "true"])("显式打开重载后的 Activity，已经展开时保持展开（%s）", async (initial) => {
    let state = initial;
    const pane = { waitFor: vi.fn().mockResolvedValue(undefined), getAttribute: vi.fn(async () => state), click: vi.fn(async () => { state = "true"; }) };
    const page = { getByRole: vi.fn(() => pane) };
    await loadHelpers().ensureActivityExpanded(page);
    expect(pane.waitFor).toHaveBeenCalledWith({ state: "visible", timeout: 30_000 });
    expect(pane.click).toHaveBeenCalledTimes(initial === "false" ? 1 : 0);
    expect(state).toBe("true");
  });

  it("任务导航等待正确 task session 的 Activity 响应，在点击前安装等待", async () => {
    const order: string[] = [];
    let predicate: ((response: any) => boolean) | undefined;
    const page = { waitForResponse: vi.fn(async (fn) => { order.push("wait"); predicate = fn; }) };
    const taskRow = { click: vi.fn(async () => { order.push("click"); }) };
    await loadHelpers().navigateToTask(page, taskRow, "task-a");
    const response = (session: string, status = true, pathname = "/api/chat/activity") => ({
      url: () => `http://127.0.0.1:3577${pathname}?session_id=${session}`,
      ok: () => status, request: () => ({ method: () => "GET" }),
    });
    expect(order).toEqual(["wait", "click"]);
    expect(predicate!(response("task-a"))).toBe(true);
    expect(predicate!(response("origin"))).toBe(false);
    expect(predicate!(response("task-a", false))).toBe(false);
    expect(predicate!(response("task-a", true, "/api/other"))).toBe(false);
  });

  it("remember 先保存 accepted 隐藏/cursor，再接受同 call 的可见 done 终态", () => {
    const accepted = { ...row("accepted", "origin", "remember", "call-1", "TOOL_RESULT"), payload: { tool_name: "remember", call_id: "call-1", state: "accepted" }, seq: 2, activity: null };
    const done = { ...accepted, event_id: "done", seq: 3, payload: { ...accepted.payload, state: "done" }, activity: { kind: "tool_result", detail: "记忆已保存" } };
    const lifecycle = loadHelpers().rememberLifecycleEvidence({ events: [accepted], cursor: { origin: 2 } }, { events: [accepted, done], cursor: { origin: 3 } }, "origin");
    expect(lifecycle.accepted.event_id).toBe("accepted");
    expect(lifecycle.done.event_id).toBe("done");
    expect(lifecycle.callId).toBe("call-1");
    expect(lifecycle.accepted_cursor.origin).toBe(2);
    expect(lifecycle.done_cursor.origin).toBe(3);
  });

  it.each(["visible-accepted", "missing-cursor", "foreign-done-call", "hidden-done"])("拒绝无效记忆时序证据（%s）", (fault) => {
    const accepted = { ...row("accepted", "origin", "remember", "call-1", "TOOL_RESULT"), payload: { tool_name: "remember", call_id: "call-1", state: "accepted" }, activity: fault === "visible-accepted" ? { kind: "tool_result" } : null };
    const done = { ...row("done", "origin", "remember", "call-1", "TOOL_RESULT"), payload: { tool_name: "remember", call_id: fault === "foreign-done-call" ? "call-2" : "call-1", state: "done" }, activity: fault === "hidden-done" ? null : { kind: "tool_result" } };
    expect(() => loadHelpers().rememberLifecycleEvidence({ events: [accepted], cursor: fault === "missing-cursor" ? {} : { origin: 1 } }, { events: [done], cursor: { origin: 2 } }, "origin")).toThrow();
  });

  it("spec 将 helper 接入 saveCase、任务导航与重载流程，并绑定源码指纹", () => {
    expect(source).toContain("caseToolEvidence(snapshotValue, sessionId, expectedTools, toolActivity)");
    expect(source).toContain('}, "PASS", undefined, taskActivity);');
    expect(source).toContain("source_fingerprints: run.source_fingerprints");
    expect(source).toContain("() => activity(page, activitySessionId)");
    expect(source).toContain("const apiCursor = toolActivity?.cursor ?? apiPage?.cursor");
    expect(source).toContain("api_cursor: apiCursor");
    expect(source).toContain("latest = accumulateActivityPage(latest, nextPage)");
    expect(source).toContain("await navigateToTask(page, taskRow, taskSessionId);");
    const replaySource = source.slice(source.indexOf("await page.reload();"));
    expect(replaySource.indexOf("await ensureActivityExpanded(page);")).toBeLessThan(replaySource.indexOf("page.getByText(/read_file/).count()"));
    expect(source).not.toContain("expect(visibleRememberResults).toHaveLength(0)");
    expect(source).toContain("failureCaseRecord(activeCase, error, caseFrames())");
    expect(source).toContain('failed.expectedTools, failed.details, "FAIL", failed.reason, failed.toolActivity');
  });
});
