import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import { isAbsolute, join, dirname } from "node:path";
import { expect, test } from "@playwright/test";
import { installVoiceWavFeed, prewarmVoiceUI } from "./fixtures/voice";

type Run = { run_id: string; database: string; source_root: string; selected_config: { path: string }; source_fingerprints: Record<string, string>; api_url: string; ui_url: string; case_directory?: string; resume_case?: string; compatibility_only?: boolean; compatibility_focus?: string };
type Action = { command_id: string; actual_call_id: string; tool_name: string; state: string; result: { task_id?: string; data?: { memory_id?: string } } };
type Snapshot = { actions: Action[]; events: { event_type: string; payload: { playback_scope?: string } }[]; tasks: { id: string; product_state: string }[]; work: { id: string; task_id: string; status: string; message_id: string | null }[]; reports: { task_id: string; outcome: string }[]; pending: { task_id: string; claimed_work_execution_id: string | null }[]; [key: string]: unknown };
type Frame = { type: string; kind?: string; pcm_base64?: string; event?: { event_type: string; payload?: { playback_scope?: string } }; [key: string]: unknown };
type VoiceWindow = Window & { __voiceWavFeed?: { play: (name: string) => Promise<void>; trackState: () => { id: string; enabled: boolean; readyState: string } | null } };
const selectedRun = process.env.M5_ACCEPTANCE_RUN;
if (!selectedRun || !isAbsolute(selectedRun)) throw new Error("M5_ACCEPTANCE_RUN must select an absolute run manifest.");
const run = JSON.parse(readFileSync(selectedRun, "utf8")) as Run;
if (!/^m5_acceptance_[0-9a-f]{32}$/.test(run.database)) throw new Error("A generated isolated M5 database is required.");
const selected = JSON.parse(readFileSync(run.selected_config.path, "utf8")) as { assets_manifest: { path: string } };
const assets = JSON.parse(readFileSync(selected.assets_manifest.path, "utf8")) as { audio: { id: string; path: string; sha256: string }[] };
for (const value of [run.api_url, run.ui_url]) {
  const url = new URL(value);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1") throw new Error("Only the selected local runtime is allowed.");
}
test.use({ channel: "chrome", headless: false, baseURL: run.ui_url });

function probe<T = Snapshot>(command: string, origin?: string, caseId?: string): T {
  const args = ["-m", "apps.m5_acceptance_probe", command, "--run", selectedRun!];
  if (origin) args.push("--origin-session-id", origin);
  if (caseId) args.push("--case", caseId);
  return JSON.parse(execFileSync(join(run.source_root, ".venv/bin/python"), args, { cwd: run.source_root, encoding: "utf8", timeout: 45_000 }));
}

test("WAV经生产Talk入口完成五类独立真实调用与DB取证", async ({ page, browser }, testInfo) => {
  test.skip(Boolean(run.resume_case) || Boolean(run.compatibility_only), "逐案例续验保留已有真实操作，不重复业务写入");
  test.setTimeout(600_000);
  probe("check-readiness");
  for (const [path, digest] of Object.entries(run.source_fingerprints)) {
    expect(createHash("sha256").update(readFileSync(join(run.source_root, path))).digest("hex"), path).toBe(digest);
  }
  const wavs: Record<string, string> = {};
  for (const id of ["submit", "pend-message", "remember", "add-evidence"]) {
    const asset = assets.audio.find((item) => item.id === id);
    if (!asset) throw new Error(`Missing selected WAV: ${id}`);
    const bytes = readFileSync(asset.path);
    expect(createHash("sha256").update(bytes).digest("hex")).toBe(asset.sha256);
    wavs[id] = bytes.toString("base64");
  }
  await installVoiceWavFeed(page, wavs);
  const sent: Frame[] = [], received: Frame[] = [], pageErrors: string[] = [], playbackErrors: number[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("response", (response) => {
    if (response.url().includes("/api/voice/playback") && response.status() >= 400) playbackErrors.push(response.status());
  });
  page.on("websocket", (socket) => {
    if (!socket.url().includes("/api/voice/realtime/")) return;
    socket.on("framesent", ({ payload }) => { const frame = JSON.parse(payload.toString()) as Frame; delete frame.pcm_base64; sent.push(frame); });
    socket.on("framereceived", ({ payload }) => { const frame = JSON.parse(payload.toString()) as Frame; delete frame.pcm_base64; received.push(frame); });
  });
  const completed = () => probe("snapshot", origin).events.filter((event) => event.event_type === "voice_turn_completed");
  const play = (id: string) => page.evaluate(async (name) => {
    const feed = (window as VoiceWindow).__voiceWavFeed;
    if (!feed) throw new Error("WAV feed is not ready");
    await feed.play(name);
  }, id);
  const caseDirectory = run.case_directory ?? join(dirname(selectedRun!), "cases");
  await mkdir(caseDirectory, { recursive: true });
  let origin = "", taskId = "", failure: string | null = null;
  const completedCases: string[] = [];
  let firstBefore: Snapshot;
  async function executeCase(caseId: string, operation: (before: Snapshot) => Promise<void>) {
    const before = probe("snapshot", origin), sentAt = sent.length, receivedAt = received.length;
    let caseFailure: string | null = null;
    try {
      await operation(before);
    } catch (error) {
      caseFailure = error instanceof Error ? error.message : String(error);
      throw error;
    } finally {
      await writeFile(join(caseDirectory, `${caseId}.json`), JSON.stringify({ case: caseId, run_id: run.run_id,
        database: run.database, origin_session_id: origin, source: "real", before, after: probe("snapshot", origin),
        failure: caseFailure, wav: assets.audio.find((item) => item.id === ({ "C1-submit": "submit", "C2-pend_message": "pend-message", "C3-remember": "remember", "C4-add_evidence": "add-evidence" } as Record<string, string>)[caseId]),
        sent: sent.slice(sentAt), received: received.slice(receivedAt), browser: browser.version() }, null, 2));
    }
    expect(probe<{ passed: boolean }>("verify-case", undefined, caseId).passed).toBe(true);
    completedCases.push(caseId);
  }
  async function waitComplete(count: number) {
    await expect.poll(() => completed().length, { timeout: 90_000 }).toBeGreaterThan(count);
    expect(completed().slice(count).every((event) => event.payload.playback_scope === "complete")).toBe(true);
    await expect(page.getByText("Listening", { exact: true })).toBeVisible();
    expect(received.filter((frame) => frame.type === "error")).toHaveLength(0);
  }
  try {
    await prewarmVoiceUI(run.ui_url);
    await page.goto("/chat");
    const created = page.waitForResponse((response) => response.url().endsWith("/api/chat/session") && response.request().method() === "POST");
    await page.getByRole("button", { name: "Start new conversation" }).click();
    origin = (await (await created).json()).session_id;
    firstBefore = probe("snapshot", origin);
    await page.getByRole("button", { name: "Start Talk mode" }).click();
    await expect(page.getByText("Listening", { exact: true })).toBeVisible({ timeout: 30_000 });
    const track = await page.evaluate(() => (window as VoiceWindow).__voiceWavFeed?.trackState());
    expect(track?.readyState).toBe("live");
    await executeCase("C1-submit", async () => {
      const count = completed().length;
      await play("submit");
      await expect.poll(() => probe("snapshot", origin).actions.filter((a) => a.tool_name === "submit").length, { timeout: 90_000 }).toBe(1);
      const action = probe("snapshot", origin).actions.find((a) => a.tool_name === "submit")!;
      taskId = action.result.task_id ?? "";
      if (!taskId) throw new Error("Actual submit did not return a task id");
      await expect.poll(() => probe("snapshot", origin).reports.filter((r) => r.task_id === taskId && r.outcome === "delivered").length, { timeout: 120_000 }).toBe(1);
      await waitComplete(count);
      await page.screenshot({ path: testInfo.outputPath("C1-真实受理与工作.png") });
    });
    await executeCase("C2-pend_message", async (before) => {
      const count = completed().length;
      await play("pend-message");
      const dialog = page.getByRole("dialog", { name: "选择要继续的任务" });
      await expect(dialog).toBeVisible({ timeout: 90_000 });
      await expect.poll(() => received.filter((f) => f.type === "capture_suspended").length).toBe(1);
      expect(await page.evaluate(() => (window as VoiceWindow).__voiceWavFeed?.trackState())).toEqual({ ...track, enabled: false });
      await dialog.locator(`input[value="${taskId}"]`).check();
      await dialog.getByRole("button", { name: "确认追加" }).click();
      await expect(dialog).not.toBeVisible();
      await expect.poll(() => received.filter((f) => f.type === "capture_resumed").length).toBe(1);
      expect(await page.evaluate(() => (window as VoiceWindow).__voiceWavFeed?.trackState())).toEqual(track);
      await expect.poll(() => probe("snapshot", origin).reports.filter((r) => r.task_id === taskId && r.outcome === "delivered").length, { timeout: 120_000 }).toBe(before.reports.length + 1);
      await waitComplete(count);
    });
    await executeCase("C3-remember", async () => {
      const count = completed().length;
      await play("remember");
      await expect.poll(() => probe("snapshot", origin).actions.filter((a) => a.tool_name === "remember" && a.state === "done").length, { timeout: 90_000 }).toBe(1);
      await waitComplete(count);
    });
    probe("bind-evidence-context", origin);
    await executeCase("C4-add_evidence", async () => {
      const count = completed().length;
      await play("add-evidence");
      await expect.poll(() => probe("snapshot", origin).actions.filter((a) => a.tool_name === "add_evidence" && a.state === "done").length, { timeout: 90_000 }).toBe(1);
      await waitComplete(count);
    });
    await writeFile(join(caseDirectory, "C5-report_task_progress.json"), JSON.stringify({ case: "C5-report_task_progress", run_id: run.run_id,
      database: run.database, origin_session_id: origin, before: firstBefore, source: "real", failure: null,
      scope: "C1或C2真实工作结束后的独立报告，不新增语音案例" }, null, 2));
    expect(probe<{ passed: boolean }>("verify-case", undefined, "C5-report_task_progress").passed).toBe(true);
    completedCases.push("C5-report_task_progress");
    expect(received.filter((f) => f.type === "ready")).toHaveLength(1);
    expect(pageErrors).toHaveLength(0);
    expect(playbackErrors).toHaveLength(0);
    await page.getByRole("button", { name: "Stop Talk mode" }).click();
    await expect(page.getByRole("button", { name: "Start Talk mode" })).toBeVisible();
  } catch (error) {
    failure = error instanceof Error ? error.message : String(error);
    throw error;
  } finally {
    await writeFile(testInfo.outputPath("五案例旅程.json"), JSON.stringify({ run_id: run.run_id, origin_session_id: origin,
      failure, completedCases, sent, received, pageErrors, playbackErrors }, null, 2));
    const cancel = page.getByRole("button", { name: "取消，不追加" });
    if (await cancel.isVisible()) await cancel.click().catch(() => {});
    const stop = page.getByRole("button", { name: "Stop Talk mode" });
    if (await stop.isVisible()) await stop.click().catch(() => {});
  }
});

test("必要M4多轮Recall与Avatar关闭兼容使用原WAV和同一媒体", async ({ page }, testInfo) => {
  test.skip(!run.compatibility_only, "仅显式选择的M4必要兼容验证，不计第六M5案例");
  test.setTimeout(240_000);
  probe("check-readiness");
  const wavs: Record<string, string> = {};
  for (const id of ["turn-01", "turn-02", "recall"]) {
    const asset = assets.audio.find((item) => item.id === id);
    if (!asset) throw new Error(`Missing selected M4 WAV: ${id}`);
    const bytes = readFileSync(asset.path);
    expect(createHash("sha256").update(bytes).digest("hex")).toBe(asset.sha256);
    wavs[id] = bytes.toString("base64");
  }
  await installVoiceWavFeed(page, wavs);
  const received: Frame[] = [], errors: string[] = [], playbackErrors: number[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", (response) => {
    if (response.url().includes("/api/voice/playback") && response.status() >= 400) playbackErrors.push(response.status());
  });
  page.on("websocket", (socket) => {
    if (!socket.url().includes("/api/voice/realtime/")) return;
    socket.on("framereceived", ({ payload }) => { const frame = JSON.parse(payload.toString()) as Frame; delete frame.pcm_base64; received.push(frame); });
  });
  // 选择既有M4兼容分支；原WAV仍走生产采集与正式授权，不替代模型或工具回执。
  await page.route("**/api/voice/realtime/sessions", async (route) => {
    const request = route.request();
    if (request.method() === "POST") await route.continue({ postData: JSON.stringify({ ...request.postDataJSON(), tool_surface: "m4" }) });
    else await route.continue();
  });
  let origin = "", failure: string | null = null;
  try {
    await prewarmVoiceUI(run.ui_url);
    await page.goto("/chat");
    const created = page.waitForResponse((response) => response.url().endsWith("/api/chat/session") && response.request().method() === "POST");
    await page.getByRole("button", { name: "Start new conversation" }).click();
    origin = (await (await created).json()).session_id;
    await page.getByRole("button", { name: "Start Talk mode" }).click();
    await expect(page.getByText("Listening", { exact: true })).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId("voice-avatar")).toBeVisible();
    const sequence = run.compatibility_focus === "recall" ? ["recall"] : ["turn-01", "turn-02", "recall"];
    for (const [index, id] of sequence.entries()) {
      await page.evaluate(async (name) => {
        const feed = (window as VoiceWindow).__voiceWavFeed;
        if (!feed) throw new Error("WAV feed is not ready");
        await feed.play(name);
      }, id);
      await expect.poll(() => probe("snapshot", origin).events.filter((e) => e.event_type === "voice_turn_completed" && e.payload.playback_scope === "complete").length, { timeout: 90_000 }).toBe(index + 1);
      await expect(page.getByTestId("voice-avatar")).toBeVisible();
    }
    expect(received.filter((frame) => frame.type === "ready")).toHaveLength(1);
    const eventTypes = received.filter((frame) => frame.type === "event").map((frame) => frame.event?.event_type);
    expect(eventTypes).toContain("voice_recall_started");
    expect(eventTypes).toContain("voice_recall_ended");
    expect(eventTypes).toContain("voice_visual_window");
    expect(eventTypes).toContain("voice_model_call_started");
    expect(received.filter((frame) => frame.type === "error")).toHaveLength(0);
    expect(errors).toHaveLength(0);
    expect(playbackErrors).toHaveLength(0);
    expect(probe("snapshot", origin).actions).toHaveLength(0);
    await page.screenshot({ path: testInfo.outputPath("M4-多轮Recall与Avatar.png") });
    await page.getByRole("button", { name: "Stop Talk mode" }).click();
    await expect(page.getByTestId("voice-avatar")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Start Talk mode" })).toBeVisible();
  } catch (error) {
    failure = error instanceof Error ? error.message : String(error);
    throw error;
  } finally {
    await writeFile(testInfo.outputPath("M4-必要兼容证据.json"), JSON.stringify({ run_id: run.run_id, origin_session_id: origin,
      scope: "既有M4 surface参数选择的必要兼容；不是M5第六独立案例", failure, received, errors, playbackErrors,
      focus: run.compatibility_focus ?? "multi-turn",
      snapshot: origin ? probe("snapshot", origin) : null }, null, 2));
    const stop = page.getByRole("button", { name: "Stop Talk mode" });
    if (await stop.isVisible()) await stop.click().catch(() => {});
  }
});

test("独立新来源Talk为本运行C3真实semantic记忆追加正式日程证据", async ({ page, browser }, testInfo) => {
  test.skip(run.resume_case !== "C4-add_evidence" || Boolean(run.compatibility_only), "仅显式选择的C4续验运行");
  test.setTimeout(180_000);
  probe("check-readiness");
  const asset = assets.audio.find((item) => item.id === "add-evidence");
  if (!asset) throw new Error("Missing selected add-evidence WAV");
  const wav = readFileSync(asset.path);
  expect(createHash("sha256").update(wav).digest("hex")).toBe(asset.sha256);
  await installVoiceWavFeed(page, { "add-evidence": wav.toString("base64") });
  const sent: Frame[] = [], received: Frame[] = [], errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("websocket", (socket) => {
    if (!socket.url().includes("/api/voice/realtime/")) return;
    socket.on("framesent", ({ payload }) => { const value = JSON.parse(payload.toString()) as Frame; delete value.pcm_base64; sent.push(value); });
    socket.on("framereceived", ({ payload }) => { const value = JSON.parse(payload.toString()) as Frame; delete value.pcm_base64; received.push(value); });
  });
  let origin = "", failure: string | null = null, before: Snapshot | null = null;
  try {
    await prewarmVoiceUI(run.ui_url);
    await page.goto("/chat");
    const created = page.waitForResponse((response) => response.url().endsWith("/api/chat/session") && response.request().method() === "POST");
    await page.getByRole("button", { name: "Start new conversation" }).click();
    origin = (await (await created).json()).session_id;
    probe("bind-evidence-context", origin);
    before = probe("snapshot", origin);
    await page.getByRole("button", { name: "Start Talk mode" }).click();
    await expect(page.getByText("Listening", { exact: true })).toBeVisible({ timeout: 30_000 });
    await page.evaluate(async () => {
      const feed = (window as VoiceWindow).__voiceWavFeed;
      if (!feed) throw new Error("WAV feed is not ready");
      await feed.play("add-evidence");
    });
    await expect.poll(() => probe("snapshot", origin).actions.filter((a) => a.tool_name === "add_evidence" && a.state === "done").length, { timeout: 90_000 }).toBe(1);
    await expect.poll(() => probe("snapshot", origin).events.filter((e) => e.event_type === "voice_turn_completed" && e.payload.playback_scope === "complete").length, { timeout: 90_000 }).toBe(1);
    expect(received.filter((frame) => frame.type === "error")).toHaveLength(0);
    expect(received.filter((frame) => frame.type === "ready")).toHaveLength(1);
    expect(sent.filter((frame) => frame.type === "audio_chunk").length).toBeGreaterThan(0);
    expect(errors).toHaveLength(0);
    await page.screenshot({ path: testInfo.outputPath("C4-真实证据追加.png") });
  } catch (error) {
    failure = error instanceof Error ? error.message : String(error);
    throw error;
  } finally {
    if (origin && before) {
      const directory = run.case_directory ?? join(dirname(selectedRun!), "cases");
      await mkdir(directory, { recursive: true });
      await writeFile(join(directory, "C4-add_evidence.json"), JSON.stringify({ case: "C4-add_evidence", run_id: run.run_id,
        database: run.database, origin_session_id: origin, before, after: probe("snapshot", origin), source: "real",
        failure, sent, received, wav: asset, browser: browser.version(), scope: "C4新正式来源，复用同一隔离agent已验证C3全局semantic记忆；不重放C1/C2/C3" }, null, 2));
    }
    const stop = page.getByRole("button", { name: "Stop Talk mode" });
    if (await stop.isVisible()) await stop.click().catch(() => {});
  }
  expect(probe<{ passed: boolean }>("verify-case", undefined, "C4-add_evidence").passed).toBe(true);
});
