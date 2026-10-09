// @vitest-environment node
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import ts from "typescript";
import { describe, expect, it } from "vitest";

// 执行实际 E2E 启动段，模拟 addInitScript 只在下一次导航生效；不访问 DB、浏览器或 Provider。
const source = readFileSync(new URL("../e2e/tool-activity.spec.ts", import.meta.url), "utf8");
const parsed = ts.createSourceFile("tool-activity.spec.ts", source, ts.ScriptTarget.Latest, true);
const registration = parsed.statements.find((node): node is ts.ExpressionStatement =>
  ts.isExpressionStatement(node) && ts.isCallExpression(node.expression) && node.expression.expression.getText(parsed) === "test");
if (!registration || !ts.isCallExpression(registration.expression)) throw new Error("E2E registration is missing");
const callbackCode = ts.transpileModule(`const callback = ${registration.expression.arguments[1].getText(parsed)}; callback`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText;

async function startup(injectionWorks = true, inspectorVisible = true, evidenceMode?: string) {
  const events: string[] = [];
  let registered = false;
  let injected = false;
  const page = {
    on() {},
    async goto() { events.push("navigate"); injected = registered && injectionWorks; },
    async waitForResponse() { return { async json() { return { session_id: "f08f18a3-4e84-4fed-a0d6-e9184eb137fd" }; } }; },
    getByRole(_role: string, options: { name: string; exact?: boolean }) {
      return { async isVisible() { return options.name === "Show activity" && !inspectorVisible; }, async click(clickOptions?: { timeout?: number }) {
        if (options.name === "Show activity" || options.name === "Start Talk mode") {
          if (clickOptions?.timeout !== 30_000) throw new Error("UI action requires a bounded timeout");
        }
        if (options.name === "Show activity") { events.push("show-activity"); inspectorVisible = true; }
        if (options.name === "Start Talk mode") { events.push("talk"); throw new Error("STOP_TALK"); }
        if (options.name === "Activity" && options.exact) { events.push("invalid-activity-locator"); throw new Error("No exact Activity button"); }
      } };
    },
    async evaluate() { events.push("check-feed"); return injected; },
  };
  const assertion = (value: unknown) => ({
    async toBeVisible() {}, toMatchObject() {}, toMatch() {},
    toBe(expected: unknown) { if (value !== expected) throw new Error("WAV feed missing"); },
    toBeTruthy() { if (!value) throw new Error("WAV feed missing"); },
  });
  const callback = runInNewContext(callbackCode, {
    test: { setTimeout() {} }, expect: assertion, run: { acceptance_modes: { "add-evidence": evidenceMode }, source_fingerprints: {}, runtime: { ui_url: "http://127.0.0.1:3577", database: "isolated" }, run_id: "run" },
    checkReadiness: () => ({}), mkdir: async () => {}, evidenceDir: "unused", realtimeFrames() {},
    prewarmVoiceUI: async () => {}, audioById: new Map(["voice-recall", "remember", ...(evidenceMode === "text_chat" ? [] : ["add-evidence"]), "background-tools"].map((id) => [id, { path: id, sha256: "hash" }])),
    readFile: async (path: string) => { events.push(`load:${path}`); return Buffer.from("wav"); }, digest: () => "hash", Buffer,
    installVoiceWavFeed: async () => { events.push("register-feed"); registered = true; },
    ensureActivityExpanded: async () => { events.push("activity-expanded"); },
  });
  let failure = "";
  try { await callback({ page, browser: {} }, {}); } catch (error) { failure = (error as Error).message; }
  return { events, failure };
}

describe("真实 WAV E2E 启动边界", () => {
  it("text_chat只加载三个WAV，不依赖或读取旧add-evidence素材", async () => {
    const result = await startup(true, true, "text_chat");
    expect(result.failure).toBe("STOP_TALK");
    expect(result.events.filter((event) => event.startsWith("load:"))).toEqual(["load:voice-recall", "load:remember", "load:background-tools"]);
  });
  it("在首次导航前注册 WAV，在 Talk 前检查当前 document 已注入", async () => {
    const result = await startup();
    expect(result.failure).toBe("STOP_TALK");
    expect(result.events.indexOf("register-feed")).toBeLessThan(result.events.indexOf("navigate"));
    expect(result.events.indexOf("check-feed")).toBeGreaterThan(result.events.indexOf("navigate"));
    expect(result.events.indexOf("check-feed")).toBeLessThan(result.events.indexOf("talk"));
  });

  it("不等待不存在的 exact Activity 按钮", async () => {
    expect((await startup()).events).not.toContain("invalid-activity-locator");
  });

  it("Inspector 关闭时通过 Show activity 有界打开，已打开时无需切换", async () => {
    const closed = await startup(true, false);
    expect(closed.failure).toBe("STOP_TALK");
    expect(closed.events.indexOf("show-activity")).toBeLessThan(closed.events.indexOf("activity-expanded"));
    expect((await startup()).events).not.toContain("show-activity");
  });

  it("注入失败时在 Talk 点击之前失败，防止调用真实麦克风", async () => {
    const result = await startup(false);
    expect(result.failure).toBe("WAV feed missing");
    expect(result.events).not.toContain("talk");
  });
});

function turnHelpers(extra: Record<string, unknown> = {}) {
  const names = ["assertSpeechUsable", "voiceTurnForTool", "completedVoiceTurn", "waitForVoiceTurnComplete", "pollActivity", "accumulateActivityPage", "assertExpectedToolAfterCompletion", "runRecordedCase", "memoryIdFromReceipt", "voiceActionForInput", "submittedTaskFromAction", "recoverInputAfterFailure"];
  const helpers = parsed.statements.filter((node) => ts.isFunctionDeclaration(node) && names.includes(node.name?.text ?? ""));
  const code = ts.transpileModule(helpers.map((node) => node.getText(parsed)).join("\n") + `\n({${names.join(",")}})`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText;
  return runInNewContext(code, { Error, Date, expect, ...extra });
}

const identity = { session_id: "origin", input_turn_id: "input-2", stimulus_revision: 0 };
const toolPage = { session_id: "origin", cursor: {}, has_more: false, events: [
  { event_type: "voice_model_call_started", payload: { response_id: "response-2", identity } },
  { event_type: "TOOL_RESULT", payload: { tool_name: "remember", response_id: "response-2", call_id: "call-2", state: "done" } },
] };
const completion = (turn = "input-2", scope = "complete", response = "response-2") => ({
  event_id: `completed-${turn}`, session_id: "origin", event_type: "voice_turn_completed", payload: { source_identity: `voice:origin:${turn}:0`, playback_scope: scope, response_ids: [response] },
});

describe("真实 provider 逐案例结果", () => {
  it("权威 done receipt 的 memory UUID 在 data 内，平坦/accepted/result失败不能代替", () => {
    const helpers = turnHelpers();
    const action = { state: "done", result: { ok: true, state: "done", error: null, data: { memory_id: "40956fa1-ce78-4fc8-9c92-734c7162c23d" } } };
    expect(helpers.memoryIdFromReceipt(action)).toBe(action.result.data.memory_id);
    expect(() => helpers.memoryIdFromReceipt({ state: "done", result: { memory_id: action.result.data.memory_id } })).toThrow("receipt");
    expect(() => helpers.memoryIdFromReceipt({ ...action, state: "accepted" })).toThrow("receipt");
  });

  it("submit 通过当前input的持久toolCall和权威action识别，不需要TOOL Activity", () => {
    const helpers = turnHelpers();
    const action = { tool_name: "submit", actual_call_id: "call-submit", state: "accepted", result: { task_id: "task-a", task_session_id: "session-a", work_execution_id: "work-a" } };
    const snapshot = { origin_session_id: "origin", actions: [action], events: [{ event_type: "voice_model_call_started", payload: { identity, turn_id: "agent-turn-2" } }], chat_messages: [{ session_id: "origin", metadata: { agent_turn_id: "agent-turn-2" }, content_blocks: [{ type: "toolCall", name: "submit", id: "call-submit" }] }] };
    expect(helpers.voiceActionForInput(snapshot, "submit", "input-2")).toEqual(action);
    expect(helpers.voiceActionForInput(snapshot, "submit", "input-1")).toBeUndefined();
    expect(helpers.submittedTaskFromAction(action)).toEqual({ taskId: "task-a", taskSessionId: "session-a", workExecutionId: "work-a" });
    expect(helpers.voiceActionForInput({ ...snapshot, actions: [{ ...action, actual_call_id: "another-call" }] }, "submit", "input-2")).toBeUndefined();
    expect(() => helpers.submittedTaskFromAction({ ...action, state: "failed" })).toThrow("accepted");
  });

  it.each(["complete", "partial", "missing-input"])("失败恢复须绑定本案例input并完整结束后才能继续：%s", async (mode) => {
    const events = mode === "missing-input" ? [] : [
      { event_type: "voice_input_committed", payload: { identity } },
      { event_type: "voice_model_call_started", payload: { identity, response_id: "response-2" } },
      completion("input-2", mode),
    ];
    const order: string[] = [];
    const helpers = turnHelpers({ snapshot: () => ({ events }), expect: () => ({ toBeVisible: async () => { order.push("listening"); } }) });
    const recovering = helpers.recoverInputAfterFailure({ getByText() {} }, "origin", { inputIds: new Set(["input-1"]) }, 10_000);
    if (mode === "complete") { await recovering; expect(order).toEqual(["listening"]); }
    else { await expect(recovering).rejects.toThrow(mode === "partial" ? "partial" : "authorized input turn"); expect(order).toEqual([]); }
  });
  it("remember 失败后跳过依赖案例，仍运行独立后台案例并保留三种状态", async () => {
    const helpers = turnHelpers();
    const records: string[] = [];
    const record = async (status: string) => { records.push(status); };
    const failed = await helpers.runRecordedCase("remember", async () => { throw new Error("no tool call"); }, record);
    const skipped = await helpers.runRecordedCase("add-evidence", async () => { throw new Error("must not run"); }, record, "缺少本轮真实 remember memory UUID");
    const passed = await helpers.runRecordedCase("background-tools", async () => { records.push("background-ran"); }, record);
    expect([failed.status, skipped.status, passed.status]).toEqual(["FAIL", "NOT_RUN", "PASS"]);
    expect(records).toEqual(["FAIL", "NOT_RUN", "background-ran"]);
  });

  it("当前输入完整结束且无目标工具时即失败；旧turn和已调用的异步accepted不误判", () => {
    const helpers = turnHelpers();
    const input = { event_type: "voice_input_committed", payload: { identity } };
    const events = [input, completion()];
    expect(() => helpers.assertExpectedToolAfterCompletion(events, new Set(["input-1"]), ["remember"])).toThrow("completed without expected tool");
    expect(() => helpers.assertExpectedToolAfterCompletion(events, new Set(["input-2"]), ["remember"])).not.toThrow();
    expect(() => helpers.assertExpectedToolAfterCompletion([...events, toolPage.events[1]], new Set(), ["remember"])).not.toThrow();
  });
});

describe("PASS 证据的实际 Activity DOM 屏障", () => {
  function domHelper(assertion: unknown) {
    const helper = parsed.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === "waitForToolActivityDom");
    if (!helper) throw new Error("waitForToolActivityDom is missing");
    return runInNewContext(ts.transpileModule(helper.getText(parsed) + "\nwaitForToolActivityDom", { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText,
      { Error, expect: assertion, ensureActivityExpanded: async () => {} });
  }
  const events = [
    { event_type: "TOOL_START", payload: { call_id: "actual-recall-call", tool_name: "voice_recall" }, activity: { detail: '{"query": "项目名称 Hexis"}' } },
    { event_type: "TOOL_RESULT", payload: { call_id: "actual-recall-call", tool_name: "voice_recall", success: true }, activity: { detail: "未召回记忆" } },
  ];
  it("API 两条事件已到而原DOM无history时，先等待同call的request/result exact正文，再允许截图", async () => {
    const order: string[] = [];
    let dom = "No tool history yet.";
    const pane = { locator: () => ({ getByText: (text: string, options: { exact: boolean }) => {
      expect(options.exact).toBe(true);
      return { text };
    } }) };
    const barrier = domHelper((locator: { text: string }) => ({ toBeVisible: async (options: { timeout: number }) => {
      expect(options.timeout).toBe(30_000);
      order.push(`wait:${locator.text}`);
      await Promise.resolve();
      dom = events.map((event) => event.activity.detail).join("\n");
      expect(dom).toContain(locator.text);
    } }));
    await barrier({ getByRole: () => pane }, { events }, ["voice_recall"]);
    order.push("screenshot");
    expect(order).toEqual([`wait:${events[0].activity.detail}`, `wait:${events[1].activity.detail}`, "screenshot"]);
  });
  it("结果来自其他call时不能以工具名相同放行", async () => {
    const barrier = domHelper(() => { throw new Error("must not inspect DOM before pairing"); });
    await expect(barrier({}, { events: [events[0], { ...events[1], payload: { ...events[1].payload, call_id: "other-call" } }] }, ["voice_recall"]))
      .rejects.toThrow("terminal result");
  });
});

describe("add_evidence canonical 验收入口", () => {
  function evidenceBranch() {
    const registration = parsed.statements.find((node): node is ts.ExpressionStatement => ts.isExpressionStatement(node)
      && ts.isCallExpression(node.expression) && node.expression.expression.getText(parsed) === "test");
    const callback = (registration!.expression as ts.CallExpression).arguments[1] as ts.ArrowFunction;
    const outerTry = (callback.body as ts.Block).statements.find(ts.isTryStatement)!;
    return outerTry.tryBlock.statements.find((node) => node.getText(parsed).includes('executeCase("add-evidence"'))!;
  }
  async function runEvidenceBranch(mode?: string) {
    const registered: string[] = [], played: string[] = [];
    const code = ts.transpileModule(`(async () => { ${evidenceBranch().getText(parsed)} })()`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
    try {
      await runInNewContext(code, {
        run: { acceptance_modes: { "add-evidence": mode } },
        executeCase: async (name: string, _tools: string[], operation: () => Promise<void>) => { registered.push(name); await operation(); },
        execFileSync: () => JSON.stringify({ memory_id: "actual-memory" }), python: "unused", dirname: () => "unused", manifestPath: "unused", repoRoot: "unused", process: { env: {} },
        expect: () => ({ toBe() {} }), rememberMemoryId: "actual-memory", audioById: new Map([["add-evidence", {}]]), origin: "origin", page: {},
        play: async (_page: unknown, name: string) => { played.push(name); throw new Error("STOP_AFTER_PLAY"); },
      });
    } catch (error) { if ((error as Error).message !== "STOP_AFTER_PLAY") throw error; }
    return { registered, played };
  }
  it("text_chat模式不注册或播放voice add-evidence，canonical artifact留给独立text test", async () => {
    expect(await runEvidenceBranch("text_chat")).toEqual({ registered: [], played: [] });
  });
  it("未选择text_chat时保留原voice入口", async () => {
    expect(await runEvidenceBranch()).toEqual({ registered: ["add-evidence"], played: ["add-evidence"] });
  });
});

describe("WAV 间的真实 turn 完成屏障", () => {
  it("工具结果绑定具体 input turn，旧完成事件及不同 response 不能放行", () => {
    const helpers = turnHelpers();
    const turn = helpers.voiceTurnForTool(toolPage, "remember");
    expect(turn).toMatchObject({ sourceIdentity: "voice:origin:input-2:0", responseId: "response-2" });
    expect(helpers.completedVoiceTurn([completion("input-1")], turn)).toBeUndefined();
    expect(helpers.completedVoiceTurn([completion("input-2", "complete", "other")], turn)).toBeUndefined();
    expect(helpers.completedVoiceTurn([completion()], turn)).toBeDefined();
    expect(() => helpers.completedVoiceTurn([completion("input-2", "partial")], turn)).toThrow("partial");
  });

  it("工具结果缺少对应输入身份时失败，不能以任意最近 turn 代替", () => {
    expect(() => turnHelpers().voiceTurnForTool({ ...toolPage, events: toolPage.events.slice(1) }, "remember")).toThrow("input identity");
  });

  it("异步 remember done 通过同一 call 的 accepted response 绑定原输入", () => {
    const done = { event_type: "TOOL_RESULT", payload: { tool_name: "remember", call_id: "call-2", state: "done" } };
    const page = { ...toolPage, events: [...toolPage.events, done] };
    expect(turnHelpers().voiceTurnForTool(page, "remember")).toMatchObject({ sourceIdentity: "voice:origin:input-2:0", responseId: "response-2" });
  });

  it.each(["event", "websocket", "websocket-event"])("转写失败即结束 Activity polling 并保留具体信息：%s", async (source) => {
    let waits = 0;
    const events = source === "event" ? [{ event_type: "speech_without_usable_transcript", payload: { item_id: "item-empty", capture_instance_id: "capture-2" } }] : [];
    const frames = source === "websocket" ? [{ type: "error", session_id: "origin", code: "speech_not_recognized", stage: "input", message: "empty transcript" }]
      : source === "websocket-event" ? [{ type: "event", session_id: "origin", event: { event_type: "speech_without_usable_transcript", payload: { item_id: "item-empty" } } }] : [];
    const helpers = turnHelpers({ activity: async () => ({ session_id: "origin", events, cursor: {}, has_more: false }) });
    await expect(helpers.pollActivity({ waitForTimeout: async () => { waits++; } }, "origin", () => false, 155_000, undefined, frames))
      .rejects.toThrow(source !== "websocket" ? "item-empty" : "speech_not_recognized");
    expect(waits).toBe(0);
  });

  it("同一 turn 完成后再确认 Listening，屏障完成才能放行下一条 WAV", async () => {
    const order: string[] = [];
    const helpers = turnHelpers({ snapshot: () => { if (order.length === 0) order.push("completion"); return { events: [...toolPage.events, completion()] }; },
      expect: (locator: { wait: () => void }) => ({ toBeVisible: async () => locator.wait() }) });
    await helpers.waitForVoiceTurnComplete({ getByText: () => ({ wait: () => order.push("listening") }) }, "origin", toolPage, "remember", 10_000, []);
    expect(order).toEqual(["completion", "listening"]);
  });
});
