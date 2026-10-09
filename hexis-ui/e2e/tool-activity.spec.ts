import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { readFileSync } from "node:fs";
import { dirname, isAbsolute, join } from "node:path";
import { expect, test, type Page, type Locator } from "@playwright/test";
import { installVoiceWavFeed, prewarmVoiceUI } from "./fixtures/voice";

type AudioAsset = { id: string; path: string; sha256: string; duration_seconds: number };
type RunManifest = {
  schema: string;
  run_id: string;
  runtime: { database: string; api_url: string; ui_url: string; realtime_config: { path: string } };
  source_fingerprints: Record<string, string>;
};
type SelectedRuntime = { materials_manifest_sha256: string };
type ActivityEvent = {
  event_id: string;
  session_id: string;
  stream_id: string;
  seq: number;
  event_type: string;
  response_id?: string | null;
  payload: Record<string, unknown>;
  activity?: { title?: string; detail?: string; [key: string]: unknown } | null;
};
type ActivityPage = { session_id: string; events: ActivityEvent[]; cursor: Record<string, number>; has_more: boolean };
type Snapshot = {
  run_id: string;
  database: string;
  origin_session_id: string;
  tasks: { id: string; task_session_id: string; product_state: string }[];
  work: { id: string; task_id: string; execution_id: string; status: string; started_at?: string; completed_at?: string }[];
  actions: { command_id: string; actual_call_id: string; tool_name: string; state: string; arguments: unknown; result: unknown; error: unknown }[];
  events: ActivityEvent[];
  chat_messages: { id: string; session_id: string; ordinal: number; role: string; content: string; content_blocks: unknown[]; source_message_id: string | null; metadata: Record<string, unknown> }[];
  memories: { id: string; type: string; status: string; content: string }[];
  belief_revision_audit: { memory_id: string; evidence: Record<string, unknown> }[];
};
type VoiceWindow = Window & {
  __voiceWavFeed?: { play: (name: string) => Promise<void>; trackState: () => { id: string; enabled: boolean; readyState: string } | null };
};
type RealtimeFrame = Record<string, unknown>;
type ActiveCase = {
  name: string; sessionId: string; screenshotName: string; beganAt: string;
  expectedTools: string[]; details: Record<string, unknown>; toolActivity?: ActivityPage;
};
type CaseOutcome = { name: string; status: "PASS" | "FAIL" | "NOT_RUN"; reason?: string };
type CompletionWatch = { inputIds: Set<string>; expectedTools: string[]; frameStart: number };

const manifestPath = process.env.BOB108_RUN_MANIFEST;
if (!manifestPath || !isAbsolute(manifestPath)) throw new Error("BOB108_RUN_MANIFEST must select an absolute run-01 manifest.");
const run = JSON.parse(readFileSync(manifestPath, "utf8")) as RunManifest;
if (run.schema !== "bob108-tool-activity-run-v1" || !/^[0-9a-f]{32}$/.test(run.run_id)) {
  throw new Error("The selected run manifest is not a BOB-108 isolated runtime.");
}
for (const value of [run.runtime.api_url, run.runtime.ui_url]) {
  const url = new URL(value);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1") throw new Error("Only this run's loopback API and UI are allowed.");
}
const repoRoot = process.cwd().endsWith("/hexis-ui") ? dirname(process.cwd()) : process.cwd();
const python = join(repoRoot, ".venv/bin/python");
const selectedPath = join(dirname(manifestPath), "selected-runtime.json");
const selected = JSON.parse(readFileSync(selectedPath, "utf8")) as SelectedRuntime;
const materialsPath = JSON.parse(readFileSync(manifestPath, "utf8")).runtime.materials_manifest_path as string;
const materialsBytes = readFileSync(materialsPath);
if (digest(materialsBytes) !== selected.materials_manifest_sha256) throw new Error("The selected materials manifest changed after run preparation.");
const materials = JSON.parse(materialsBytes.toString("utf8")) as { audio: AudioAsset[] };
const audioById = new Map(materials.audio.map((asset) => [asset.id, asset]));
const evidenceDir = join(dirname(manifestPath), "evidence", "cases");
const realtimeTimeout = Number(JSON.parse(readFileSync(run.runtime.realtime_config.path, "utf8")).timeout ?? 30);
test.use({ channel: "chrome", headless: false, baseURL: run.runtime.ui_url });

function digest(bytes: Buffer): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function snapshot(sessionId: string): Snapshot {
  const output = execFileSync(python, [
    "-m", "apps.bob108_tool_activity_probe", "snapshot", "--run-dir", dirname(manifestPath!), "--session-id", sessionId,
  ], { cwd: repoRoot, encoding: "utf8", timeout: 20_000, env: { ...process.env } });
  return JSON.parse(output) as Snapshot;
}

function checkReadiness(): Record<string, unknown> {
  const output = execFileSync(python, ["-m", "apps.bob108_tool_activity_probe", "check-readiness", "--run-dir", dirname(manifestPath!)], {
    cwd: repoRoot, encoding: "utf8", timeout: 45_000, env: { ...process.env },
  });
  return JSON.parse(output) as Record<string, unknown>;
}

async function activity(page: Page, sessionId: string, cursor?: Record<string, number>): Promise<ActivityPage> {
  const url = new URL("/api/chat/activity", run.runtime.api_url);
  url.searchParams.set("session_id", sessionId);
  url.searchParams.set("limit", "500");
  if (cursor && Object.keys(cursor).length) url.searchParams.set("cursor", JSON.stringify(cursor));
  const response = await page.request.get(url.toString());
  expect(response.ok(), `Activity API returned ${response.status()}`).toBeTruthy();
  return await response.json() as ActivityPage;
}

function realtimeFrames(page: Page, sent: RealtimeFrame[], received: RealtimeFrame[]): void {
  page.on("websocket", (socket) => {
    if (!socket.url().includes("/api/voice/realtime/")) return;
    socket.on("framesent", ({ payload }) => {
      try {
        const frame = JSON.parse(payload.toString()) as RealtimeFrame;
        delete frame.pcm_base64;
        sent.push(frame);
      } catch { /* binary audio is not JSON evidence */ }
    });
    socket.on("framereceived", ({ payload }) => {
      try {
        const frame = JSON.parse(payload.toString()) as RealtimeFrame;
        delete frame.pcm_base64;
        received.push(frame);
      } catch { /* binary audio is not JSON evidence */ }
    });
  });
}

async function play(page: Page, assetId: string): Promise<void> {
  const asset = audioById.get(assetId);
  if (!asset) throw new Error(`Missing selected WAV ${assetId}`);
  const bytes = await readFile(asset.path);
  if (digest(bytes) !== asset.sha256) throw new Error(`Selected WAV hash changed for ${assetId}`);
  await page.evaluate(async (name) => {
    const feed = (window as VoiceWindow).__voiceWavFeed;
    if (!feed) throw new Error("Talk WAV input stream was not installed.");
    await feed.play(name);
  }, assetId);
}

async function pollActivity(
  page: Page,
  sessionId: string,
  predicate: (events: ActivityEvent[]) => boolean,
  deadlineMs: number,
  onPage?: (result: ActivityPage) => Promise<void>,
  frames: RealtimeFrame[] = [],
  watch?: CompletionWatch,
): Promise<ActivityPage> {
  const stopAt = Date.now() + deadlineMs;
  let cursor: Record<string, number> = {};
  let latest: ActivityPage = { session_id: sessionId, events: [], cursor, has_more: false };
  while (Date.now() < stopAt) {
    assertSpeechUsable(latest.events, frames.slice(watch?.frameStart ?? 0), sessionId);
    const nextPage = await activity(page, sessionId, cursor);
    assertSpeechUsable(nextPage.events, frames.slice(watch?.frameStart ?? 0), sessionId);
    await onPage?.(nextPage);
    latest = accumulateActivityPage(latest, nextPage);
    cursor = latest.cursor;
    if (predicate(latest.events)) return latest;
    if (watch) assertExpectedToolAfterCompletion(snapshot(sessionId).events, watch.inputIds, watch.expectedTools);
    await page.waitForTimeout(250);
  }
  throw new Error(`Timed out waiting for actual Activity events in session ${sessionId}`);
}

function assertExpectedToolAfterCompletion(events: ActivityEvent[], previousInputIds: Set<string>, expectedTools: string[]): void {
  const input = events.filter((event) => event.event_type === "voice_input_committed")
    .map((event) => event.payload.identity as { session_id: string; input_turn_id: string; stimulus_revision: number })
    .filter((identity) => identity && !previousInputIds.has(identity.input_turn_id)).at(-1);
  if (!input) return;
  const source = `voice:${input.session_id}:${input.input_turn_id}:${input.stimulus_revision}`;
  const completed = events.find((event) => event.event_type === "voice_turn_completed" && event.payload.source_identity === source);
  if (!completed || !Array.isArray(completed.payload.response_ids)) return;
  const invoked = events.some((event) => ["TOOL_START", "TOOL_RESULT"].includes(event.event_type)
    && expectedTools.includes(String(event.payload.tool_name))
    && (completed.payload.response_ids as unknown[]).includes(event.payload.response_id ?? event.response_id));
  if (!invoked) throw new Error(`Model turn ${source} completed without expected tool: ${expectedTools.join(", ")}`);
}

function memoryIdFromReceipt(action: Snapshot["actions"][number]): string {
  const receipt = action.result as { ok?: boolean; state?: string; error?: unknown; data?: { memory_id?: unknown } } | null;
  const id = receipt?.data?.memory_id;
  if (action.state !== "done" || receipt?.state !== "done" || receipt.ok !== true || receipt.error !== null
    || typeof id !== "string" || !/^[0-9a-f-]{36}$/i.test(id)) throw new Error("Authoritative done receipt lacks data.memory_id");
  return id;
}

function voiceActionForInput(state: Snapshot, toolName: string, inputTurnId: string): Snapshot["actions"][number] | undefined {
  const turnIds = new Set(state.events.filter((event) => event.event_type === "voice_model_call_started"
    && (event.payload.identity as { input_turn_id?: string } | undefined)?.input_turn_id === inputTurnId).map((event) => event.payload.turn_id));
  const calls = new Set(state.chat_messages.filter((message) => message.session_id === state.origin_session_id
    && turnIds.has(message.metadata.agent_turn_id)).flatMap((message) => message.content_blocks)
    .filter((block) => typeof block === "object" && block !== null && (block as Record<string, unknown>).type === "toolCall"
      && (block as Record<string, unknown>).name === toolName).map((block) => (block as Record<string, unknown>).id));
  return state.actions.find((action) => action.tool_name === toolName && calls.has(action.actual_call_id));
}

function submittedTaskFromAction(action: Snapshot["actions"][number]): { taskId: string; taskSessionId: string; workExecutionId: string } {
  const result = action.result as { task_id?: string; task_session_id?: string; work_execution_id?: string } | null;
  if (action.tool_name !== "submit" || action.state !== "accepted" || !result?.task_id || !result.task_session_id || !result.work_execution_id) {
    throw new Error("Authoritative submit action lacks accepted task/work identity");
  }
  return { taskId: result.task_id, taskSessionId: result.task_session_id, workExecutionId: result.work_execution_id };
}

async function waitForSubmittedAction(page: Page, sessionId: string, watch: CompletionWatch, deadlineMs: number, frames: RealtimeFrame[]): Promise<Snapshot["actions"][number]> {
  const stopAt = Date.now() + deadlineMs;
  while (Date.now() < stopAt) {
    assertSpeechUsable([], frames.slice(watch.frameStart), sessionId);
    const state = snapshot(sessionId);
    const input = state.events.filter((event) => event.event_type === "voice_input_committed")
      .map((event) => event.payload.identity as { input_turn_id: string }).filter((identity) => identity && !watch.inputIds.has(identity.input_turn_id)).at(-1);
    if (input) {
      const action = voiceActionForInput(state, "submit", input.input_turn_id);
      if (action) { submittedTaskFromAction(action); return action; }
    }
    assertExpectedToolAfterCompletion(state.events, watch.inputIds, ["submit"]);
    await page.waitForTimeout(250);
  }
  throw new Error("Timed out waiting for the current input's authoritative submit action");
}

async function recoverInputAfterFailure(page: Page, sessionId: string, watch: CompletionWatch, deadlineMs: number): Promise<void> {
  const state = snapshot(sessionId);
  const input = state.events.filter((event) => event.event_type === "voice_input_committed")
    .map((event) => event.payload.identity as { input_turn_id: string; stimulus_revision: number }).filter((identity) => identity && !watch.inputIds.has(identity.input_turn_id)).at(-1);
  if (!input) throw new Error("Cannot establish this failed case's authorized input turn; further WAV input is blocked");
  const start = state.events.find((event) => event.event_type === "voice_model_call_started"
    && (event.payload.identity as { input_turn_id?: string } | undefined)?.input_turn_id === input.input_turn_id);
  if (typeof start?.payload.response_id !== "string") throw new Error("Failed input has no bound response; further WAV input is blocked");
  const turn = { sourceIdentity: `voice:${sessionId}:${input.input_turn_id}:${input.stimulus_revision}`, responseId: start.payload.response_id };
  const stopAt = Date.now() + deadlineMs;
  while (Date.now() < stopAt) {
    if (completedVoiceTurn(snapshot(sessionId).events, turn)) {
      await expect(page.getByText("Listening", { exact: true })).toBeVisible({ timeout: 30_000 });
      return;
    }
    await page.waitForTimeout(250);
  }
  throw new Error(`Failed input ${turn.sourceIdentity} did not complete; further WAV input is blocked`);
}

async function runRecordedCase(
  name: string, operation: () => Promise<void>, recordFailure: (status: "FAIL" | "NOT_RUN", reason: string) => Promise<void>, dependencyReason?: string,
): Promise<CaseOutcome> {
  if (dependencyReason) {
    await recordFailure("NOT_RUN", dependencyReason);
    return { name, status: "NOT_RUN", reason: dependencyReason };
  }
  try { await operation(); return { name, status: "PASS" }; }
  catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    await recordFailure("FAIL", reason);
    return { name, status: "FAIL", reason };
  }
}

function assertSpeechUsable(events: ActivityEvent[], frames: RealtimeFrame[], sessionId: string): void {
  const receivedEvents = frames.filter((value) => value.session_id === sessionId && value.type === "event")
    .map((value) => value.event as ActivityEvent).filter(Boolean);
  const event = [...events, ...receivedEvents].find((value) => value.event_type === "speech_without_usable_transcript");
  if (event) throw new Error(`speech_without_usable_transcript: ${JSON.stringify(event.payload)}`);
  const error = frames.find((value) => value.session_id === sessionId && value.type === "error" && value.code === "speech_not_recognized");
  if (error) throw new Error(`speech_not_recognized: ${JSON.stringify(error)}`);
}

type VoiceTurnBinding = { sourceIdentity: string; responseId: string };

function voiceTurnForTool(page: ActivityPage, toolName: string, sourceEvents: ActivityEvent[] = page.events): VoiceTurnBinding {
  const result = page.events.filter((event) => event.event_type === "TOOL_RESULT" && event.payload.tool_name === toolName).at(-1);
  const boundCall = result?.payload.call_id;
  const sameCall = typeof boundCall === "string" && boundCall ? page.events.find((event) =>
    ["TOOL_START", "TOOL_RESULT"].includes(event.event_type) && event.payload.tool_name === toolName
    && event.payload.call_id === boundCall && typeof event.payload.response_id === "string") : undefined;
  const responseId = result?.payload.response_id ?? result?.response_id ?? sameCall?.payload.response_id;
  const start = sourceEvents.find((event) => event.event_type === "voice_model_call_started" && event.payload.response_id === responseId);
  const identity = start?.payload.identity as { session_id?: string; input_turn_id?: string; stimulus_revision?: number } | undefined;
  if (typeof responseId !== "string" || !responseId || identity?.session_id !== page.session_id
    || typeof identity.input_turn_id !== "string" || !identity.input_turn_id || !Number.isInteger(identity.stimulus_revision)) {
    throw new Error(`Tool ${toolName} result lacks a matching response and input identity`);
  }
  return { sourceIdentity: `voice:${page.session_id}:${identity.input_turn_id}:${identity.stimulus_revision}`, responseId };
}

function completedVoiceTurn(events: ActivityEvent[], turn: VoiceTurnBinding): ActivityEvent | undefined {
  const completed = events.find((event) => event.event_type === "voice_turn_completed"
    && event.payload.source_identity === turn.sourceIdentity && Array.isArray(event.payload.response_ids)
    && event.payload.response_ids.includes(turn.responseId));
  if (completed && completed.payload.playback_scope !== "complete") {
    throw new Error(`Voice turn ${turn.sourceIdentity} completed with playback_scope=${completed.payload.playback_scope}`);
  }
  return completed;
}

async function waitForVoiceTurnComplete(
  page: Page, sessionId: string, toolPage: ActivityPage, toolName: string, deadlineMs: number, frames: RealtimeFrame[],
): Promise<void> {
  const turn = voiceTurnForTool(toolPage, toolName, snapshot(sessionId).events);
  const stopAt = Date.now() + deadlineMs;
  let complete = false;
  while (Date.now() < stopAt) {
    const events = snapshot(sessionId).events;
    assertSpeechUsable([], frames, sessionId);
    if (completedVoiceTurn(events, turn)) { complete = true; break; }
    await page.waitForTimeout(250);
  }
  if (!complete) throw new Error(`Timed out waiting for complete voice turn ${turn.sourceIdentity}`);
  await expect(page.getByText("Listening", { exact: true })).toBeVisible({ timeout: 30_000 });
  assertSpeechUsable([], frames, sessionId);
}

function accumulateActivityPage(previous: ActivityPage, next: ActivityPage): ActivityPage {
  if (previous.session_id !== next.session_id || next.events.some((event) => event.session_id !== next.session_id)) {
    throw new Error("Activity pagination returned events for another session.");
  }
  const events = new Map(previous.events.map((event) => [event.event_id, event]));
  for (const event of next.events) {
    if (!events.has(event.event_id)) events.set(event.event_id, event);
  }
  return { ...next, events: [...events.values()] };
}

function rawToolEvents(snapshotValue: Snapshot, sessionId: string, toolNames: string[]): ActivityEvent[] {
  return snapshotValue.events.filter((event) => event.session_id === sessionId
    && ["TOOL_START", "TOOL_RESULT"].includes(event.event_type)
    && toolNames.includes(String(event.payload.tool_name ?? "")));
}

function caseToolEvidence(snapshotValue: Snapshot, sessionId: string, toolNames: string[], toolActivity?: ActivityPage) {
  const activitySessionId = toolActivity?.session_id ?? sessionId;
  if (toolActivity && activitySessionId !== sessionId && !snapshotValue.tasks.some((task) => task.task_session_id === activitySessionId)) {
    throw new Error("The selected task Activity does not belong to this origin snapshot.");
  }
  const events = rawToolEvents({ ...snapshotValue, events: toolActivity?.events ?? snapshotValue.events }, activitySessionId, toolNames);
  return {
    activitySessionId, events,
    callIds: [...new Set(events.map((event) => String(event.payload.call_id ?? "")).filter(Boolean))],
    eventIds: events.map((event) => event.event_id),
  };
}

function originalChatMessagesEvidence(before: Snapshot["chat_messages"], after: Snapshot["chat_messages"]) {
  const canonical = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(canonical);
    if (value !== null && typeof value === "object") {
      return Object.fromEntries(Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
        .map(([key, nested]) => [key, canonical(nested)]));
    }
    return value;
  };
  const summarize = (messages: Snapshot["chat_messages"]) => {
    const normalized = canonical([...messages].sort((left, right) => left.ordinal - right.ordinal));
    const bytes = Buffer.from(JSON.stringify(normalized));
    return { message_count: messages.length, sha256: digest(bytes) };
  };
  const beforeSummary = summarize(before);
  const afterSummary = summarize(after);
  return {
    before: beforeSummary,
    after: afterSummary,
    before_messages: before,
    after_messages: after,
    unchanged: beforeSummary.sha256 === afterSummary.sha256,
  };
}

async function ensureActivityExpanded(page: Page): Promise<void> {
  const pane = page.getByRole("button", { name: /^Activity\s/ });
  await pane.waitFor({ state: "visible", timeout: 30_000 });
  if (await pane.getAttribute("aria-expanded") !== "true") await pane.click({ timeout: 30_000 });
}

async function waitForToolActivityDom(page: Page, activityPage: ActivityPage, expectedTools: string[]): Promise<void> {
  const starts = activityPage.events.filter((event) => event.event_type === "TOOL_START" && expectedTools.includes(String(event.payload.tool_name)));
  const details: string[] = [];
  for (const tool of expectedTools) if (!starts.some((event) => event.payload.tool_name === tool)) throw new Error(`Activity DOM evidence lacks ${tool} request`);
  for (const start of starts) {
    const result = activityPage.events.find((event) => event.event_type === "TOOL_RESULT" && event.payload.state !== "accepted"
      && event.payload.tool_name === start.payload.tool_name && event.payload.call_id === start.payload.call_id);
    if (!result || typeof start.activity?.detail !== "string" || !start.activity.detail
      || typeof result.activity?.detail !== "string" || !result.activity.detail) {
      throw new Error(`Activity DOM evidence lacks a presented request/terminal result for call ${start.payload.call_id}`);
    }
    details.push(start.activity.detail, result.activity.detail);
  }
  await ensureActivityExpanded(page);
  const pane = page.getByRole("button", { name: /^Activity\s/ }).locator("..");
  for (const detail of details) await expect(pane.getByText(detail, { exact: true })).toBeVisible({ timeout: 30_000 });
}

async function navigateToTask(page: Page, taskRow: Locator, taskSessionId: string): Promise<void> {
  await Promise.all([
    page.waitForResponse((response) => {
      const url = new URL(response.url());
      return response.ok() && response.request().method() === "GET"
        && url.pathname === "/api/chat/activity" && url.searchParams.get("session_id") === taskSessionId;
    }, { timeout: 30_000 }),
    taskRow.click(),
  ]);
}

function rememberPhaseEvents(page: ActivityPage, sessionId: string, state: string, callId?: string): ActivityEvent[] {
  return page.events.filter((event) => event.session_id === sessionId && event.event_type === "TOOL_RESULT"
    && event.payload.tool_name === "remember" && event.payload.state === state
    && (!callId || event.payload.call_id === callId));
}

function rememberLifecycleEvidence(acceptedPage: ActivityPage, donePage: ActivityPage, sessionId: string) {
  const accepted = rememberPhaseEvents(acceptedPage, sessionId, "accepted")[0];
  if (!accepted || accepted.activity !== null || acceptedPage.cursor[accepted.stream_id] < accepted.seq
    || !Number.isInteger(acceptedPage.cursor[accepted.stream_id])) {
    throw new Error("The real accepted receipt must be hidden while its stream cursor advances.");
  }
  const callId = String(accepted.payload.call_id ?? "");
  const done = rememberPhaseEvents(donePage, sessionId, "done", callId)[0];
  if (!callId || !done || !done.activity || done.activity.kind !== "tool_result") {
    throw new Error("The same remember call must have a real visible done event.");
  }
  return { accepted, done, callId, accepted_cursor: acceptedPage.cursor, done_cursor: donePage.cursor };
}

function failureCaseRecord(active: ActiveCase, error: unknown, frames: Record<string, unknown>) {
  return {
    ...active, status: "FAIL" as const,
    reason: error instanceof Error ? error.message : String(error),
    details: { ...active.details, browser_realtime_protocol: frames },
  };
}

async function captureCaseArtifact<T>(read: () => Promise<T>, label: string, status: "PASS" | "FAIL" | "NOT_RUN", errors: string[]): Promise<T | undefined> {
  try { return await read(); } catch (error) {
    if (status === "PASS") throw error;
    errors.push(`${label}: ${error instanceof Error ? error.message : String(error)}`);
    return undefined;
  }
}

async function saveCase(
  name: string,
  page: Page,
  sessionId: string,
  screenshotName: string,
  beganAt: string,
  expectedTools: string[],
  details: Record<string, unknown>,
  status: "PASS" | "FAIL" | "NOT_RUN",
  reason?: string,
  toolActivity?: ActivityPage,
): Promise<void> {
  const file = join(evidenceDir, `${name}.json`);
  const screenshot = join(evidenceDir, screenshotName);
  await mkdir(evidenceDir, { recursive: true });
  const captureErrors: string[] = [];
  const activitySessionId = toolActivity?.session_id ?? sessionId;
  const apiPage = await captureCaseArtifact(() => activity(page, activitySessionId), "Activity API", status, captureErrors);
  if (status === "PASS") await waitForToolActivityDom(page, toolActivity ?? apiPage!, expectedTools);
  const screenshotBytes = await captureCaseArtifact(async () => {
    await page.screenshot({ path: screenshot, fullPage: true });
    return await readFile(screenshot);
  }, "screenshot", status, captureErrors);
  const snapshotValue = await captureCaseArtifact(async () => snapshot(sessionId), "DB snapshot", status, captureErrors);
  const actualActivity = toolActivity ?? apiPage;
  const events = snapshotValue
    ? caseToolEvidence(snapshotValue, sessionId, expectedTools, toolActivity).events
    : (actualActivity?.events ?? []).filter((event) => event.session_id === activitySessionId
      && ["TOOL_START", "TOOL_RESULT"].includes(event.event_type) && expectedTools.includes(String(event.payload.tool_name ?? "")));
  const callIds = [...new Set(events.map((event) => String(event.payload.call_id ?? "")).filter(Boolean))];
  const eventIds = events.map((event) => event.event_id);
  const apiCursor = toolActivity?.cursor ?? apiPage?.cursor;
  const sidePanel = await captureCaseArtifact(() => page.locator("aside").filter({ has: page.getByRole("heading", { name: "Activity", exact: true }) }).innerText(), "Activity DOM", status, captureErrors);
  await writeFile(file, JSON.stringify({
    schema: "bob108-tool-activity-case-v1",
    case: name,
    run_id: run.run_id,
    status,
    ...(reason ? { reason } : {}),
    began_at: beganAt,
    ended_at: new Date().toISOString(),
    run: { database: run.runtime.database, api_url: run.runtime.api_url, ui_url: run.runtime.ui_url },
    origin_session_id: sessionId,
    expected_tools: expectedTools,
    evidence: {
      source_fingerprints: run.source_fingerprints,
      call_ids: callIds,
      event_ids: eventIds,
      screenshot: screenshotBytes ? screenshot : null,
      screenshot_sha256: screenshotBytes ? digest(screenshotBytes) : null,
      activity_session_id: activitySessionId,
      api_cursor: apiCursor,
      activity_events: events,
      api_activity: apiPage ?? null,
      activity_dom: sidePanel,
      snapshot: snapshotValue ?? null,
      capture_errors: captureErrors,
      ...details,
    },
  }, null, 2), "utf8");
}

test("BOB-108 四条固定 WAV 真实调用与 Tool Activity 生命周期", async ({ page, browser }, testInfo) => {
  test.setTimeout(900_000);
  for (const [relative, expected] of Object.entries(run.source_fingerprints)) {
    const sourcePath = join(repoRoot, relative);
    if (!isAbsolute(sourcePath) || digest(readFileSync(sourcePath)) !== expected) {
      throw new Error(`Run source fingerprint changed or is missing: ${relative}`);
    }
  }
  const readiness = checkReadiness();
  expect(readiness).toMatchObject({ run_id: run.run_id, state: "ready", database: run.runtime.database });
  await mkdir(evidenceDir, { recursive: true });
  const sent: RealtimeFrame[] = [];
  const received: RealtimeFrame[] = [];
  const pageErrors: string[] = [];
  const requestFailures: string[] = [];
  const evidenceFailures: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("requestfailed", (request) => requestFailures.push(`${request.method()} ${request.url()} ${request.failure()?.errorText ?? "failed"}`));
  realtimeFrames(page, sent, received);
  const wavs: Record<string, string> = {};
  for (const id of ["voice-recall", "remember", "add-evidence", "background-tools"]) {
    const asset = audioById.get(id);
    if (!asset) throw new Error(`Required WAV ${id} is missing from the selected material manifest.`);
    const bytes = await readFile(asset.path);
    expect(digest(bytes), `WAV ${id} digest`).toBe(asset.sha256);
    wavs[id] = bytes.toString("base64");
  }
  await installVoiceWavFeed(page, wavs);
  await prewarmVoiceUI(run.runtime.ui_url);
  await page.goto("/chat");
  await expect(page.getByRole("button", { name: "Start new conversation" })).toBeVisible();
  const sessionResponse = page.waitForResponse((response) => response.url().endsWith("/api/chat/session") && response.request().method() === "POST");
  await page.getByRole("button", { name: "Start new conversation" }).click({ timeout: 30_000 });
  const origin = String((await (await sessionResponse).json()).session_id);
  expect(origin).toMatch(/^[0-9a-f-]{36}$/i);
  const showActivity = page.getByRole("button", { name: "Show activity", exact: true });
  if (await showActivity.isVisible()) await showActivity.click({ timeout: 30_000 });
  await ensureActivityExpanded(page);
  expect(await page.evaluate(() => typeof (window as VoiceWindow).__voiceWavFeed?.play === "function"),
    "WAV microphone feed must be injected before Start Talk").toBe(true);
  await page.getByRole("button", { name: "Start Talk mode" }).click({ timeout: 30_000 });
  await expect(page.getByText("Listening", { exact: true })).toBeVisible({ timeout: 60_000 });
  expect(await page.evaluate(() => (window as VoiceWindow).__voiceWavFeed?.trackState()?.readyState)).toBe("live");

  const realtimeConfig = JSON.parse(readFileSync(run.runtime.realtime_config.path, "utf8")) as { model?: string; timeout?: number };
  const overallDeadline = (duration: number) => Math.ceil((duration + Number(realtimeConfig.timeout ?? realtimeTimeout) + 120) * 1000);
  const caseFrames = () => ({ browser_version: browser.version(), sent: [...sent], received: [...received], page_errors: [...pageErrors], request_failures: [...requestFailures] });

  let startedAt = new Date().toISOString();
  let activeCase: ActiveCase | null = null;
  let rememberMemoryId: string | undefined;
  let watch: CompletionWatch;
  let stimulusBlockReason: string | undefined;
  const outcomes: CaseOutcome[] = [];
  async function executeCase(name: string, tools: string[], operation: () => Promise<void>, dependencyReason?: string): Promise<void> {
    startedAt = new Date().toISOString();
    activeCase = { name, sessionId: origin, screenshotName: `${name}-failed.png`, beganAt: startedAt, expectedTools: tools, details: { wav: audioById.get(name) } };
    const before = snapshot(origin);
    watch = { inputIds: new Set(before.events.filter((event) => event.event_type === "voice_transcript_final").map((event) => String(event.payload.input_turn_id))), expectedTools: name === "background-tools" ? ["submit"] : tools, frameStart: received.length };
    const result = await runRecordedCase(name, async () => {
      await expect(page.getByText("Listening", { exact: true })).toBeVisible({ timeout: 30_000 });
      await operation();
    }, async (status, reason) => {
      const failed = activeCase!;
      try {
        await saveCase(failed.name, page, origin, failed.screenshotName, failed.beganAt,
          failed.expectedTools, { ...failed.details, browser_realtime_protocol: caseFrames() }, status, reason, failed.toolActivity);
      } catch (captureError) { evidenceFailures.push(`${name}: ${captureError instanceof Error ? captureError.message : String(captureError)}`); }
    }, stimulusBlockReason ?? dependencyReason);
    outcomes.push(result);
    if (result.status === "FAIL") {
      try { await recoverInputAfterFailure(page, origin, watch, overallDeadline(audioById.get(name)!.duration_seconds)); }
      catch (error) { stimulusBlockReason = error instanceof Error ? error.message : String(error); evidenceFailures.push(`输入恢复：${stimulusBlockReason}`); }
    }
    activeCase = null;
  }
  try {
    await executeCase("voice-recall", ["voice_recall"], async () => {
    startedAt = new Date().toISOString();
    const recallAsset = audioById.get("voice-recall")!;
    activeCase = { name: "voice-recall", sessionId: origin, screenshotName: "voice-recall-failed.png", beganAt: startedAt, expectedTools: ["voice_recall"], details: { wav: recallAsset } };
    await play(page, "voice-recall");
    const recallPage = await pollActivity(page, origin, (events) => events.some((event) => event.event_type === "TOOL_RESULT" && event.payload.tool_name === "voice_recall"), overallDeadline(recallAsset.duration_seconds), undefined, received, watch);
    const recallEvents = recallPage.events.filter((event) => event.payload.tool_name === "voice_recall");
    const recallCalls = [...new Set(recallEvents.map((event) => String(event.payload.call_id ?? "")))];
    expect(recallCalls.length).toBeGreaterThan(0);
    expect(recallEvents.some((event) => event.event_type === "TOOL_START")).toBeTruthy();
    expect(recallEvents.some((event) => event.event_type === "TOOL_RESULT")).toBeTruthy();
    const recallOutput = recallEvents.find((event) => event.event_type === "TOOL_RESULT")?.payload.output as { memories?: { content?: string }[] } | undefined;
    const actualRecall = recallOutput?.memories ?? [];
    expect(recallOutput).toBeTruthy();
    expect(actualRecall.every((memory) => typeof memory.content === "string" && memory.content.length <= 2000)).toBeTruthy();
    const recallFrame = caseFrames();
    await saveCase("voice-recall", page, origin, "voice-recall.png", startedAt, ["voice_recall"], {
      wav: { ...recallAsset, duration_seconds: recallAsset.duration_seconds },
      provider: { model: realtimeConfig.model, tool_calls: recallEvents.map((event) => ({ event_type: event.event_type, call_id: event.payload.call_id, arguments: event.payload.arguments, response_id: event.payload.response_id, output: event.payload.output })) },
      browser_realtime_protocol: recallFrame,
      returned_memories: actualRecall,
      check: "Only compares actual returned text length/empty shape; semantic match quality is excluded.",
    }, "PASS");
    await waitForVoiceTurnComplete(page, origin, recallPage, "voice_recall", overallDeadline(recallAsset.duration_seconds), received.slice(watch.frameStart));
    activeCase = null;
    });

    await executeCase("remember", ["remember"], async () => {
    startedAt = new Date().toISOString();
    const rememberAsset = audioById.get("remember")!;
    activeCase = { name: "remember", sessionId: origin, screenshotName: "remember-failed.png", beganAt: startedAt, expectedTools: ["remember"], details: { wav: rememberAsset } };
    await play(page, "remember");
    const acceptedPage = await pollActivity(page, origin, (events) => events.some((event) => event.event_type === "TOOL_RESULT" && event.payload.tool_name === "remember" && event.payload.state === "accepted"), overallDeadline(rememberAsset.duration_seconds), undefined, received, watch);
    const acceptedRemember = rememberPhaseEvents(acceptedPage, origin, "accepted")[0];
    expect(acceptedRemember.activity, "accepted must not produce a visible Activity item").toBeNull();
    expect(acceptedPage.cursor[acceptedRemember.stream_id]).toBeGreaterThanOrEqual(acceptedRemember.seq);
    const acceptedObservedAt = new Date().toISOString();
    const acceptedScreenshot = join(evidenceDir, "remember-accepted.png");
    await page.screenshot({ path: acceptedScreenshot, fullPage: true });
    activeCase.details.accepted_phase = { event: acceptedRemember, cursor: acceptedPage.cursor, observed_at: acceptedObservedAt, screenshot: acceptedScreenshot };
    await expect.poll(() => snapshot(origin).actions.filter((action) => action.tool_name === "remember" && action.state === "done").length, { timeout: overallDeadline(rememberAsset.duration_seconds) }).toBe(1);
    const donePage = await pollActivity(page, origin, (events) => events.some((event) => event.event_type === "TOOL_RESULT" && event.payload.tool_name === "remember" && event.payload.state === "done" && event.payload.call_id === acceptedRemember.payload.call_id), overallDeadline(rememberAsset.duration_seconds), undefined, received, watch);
    const lifecycle = rememberLifecycleEvidence(acceptedPage, donePage, origin);
    const rememberSnapshot = snapshot(origin);
    const remember = rememberSnapshot.actions.find((action) => action.tool_name === "remember" && action.state === "done")!;
    const memoryId = memoryIdFromReceipt(remember);
    rememberMemoryId = memoryId;
    const rememberedMemory = rememberSnapshot.memories.find((memory) => memory.id === memoryId);
    expect(rememberedMemory, "The authoritative remember UUID must resolve to a real DB memory row").toBeTruthy();
    expect(remember.actual_call_id).toBe(lifecycle.callId);
    await ensureActivityExpanded(page);
    expect(lifecycle.done.activity?.detail).toBeTruthy();
    await expect(page.getByText(String(lifecycle.done.activity!.detail), { exact: true })).toBeVisible({ timeout: 30_000 });
    const rememberFrame = caseFrames();
    await saveCase("remember", page, origin, "remember.png", startedAt, ["remember"], {
      wav: rememberAsset,
      provider: { model: realtimeConfig.model, call_id: remember.actual_call_id, arguments: remember.arguments },
      receipt: remember,
      memory: rememberedMemory,
      cursor: donePage.cursor,
      accepted_phase: { event: lifecycle.accepted, cursor: acceptedPage.cursor, observed_at: acceptedObservedAt, screenshot: acceptedScreenshot, screenshot_sha256: digest(await readFile(acceptedScreenshot)) },
      done_phase: { event: lifecycle.done, cursor: donePage.cursor, observed_at: new Date().toISOString() },
      browser_realtime_protocol: rememberFrame,
      check: "accepted is not presented as a terminal done result; task receipt and cursor are read back from the target DB/API.",
    }, "PASS");
    await waitForVoiceTurnComplete(page, origin, donePage, "remember", overallDeadline(rememberAsset.duration_seconds), received.slice(watch.frameStart));
    activeCase = null;
    });

    await executeCase("add-evidence", ["add_evidence"], async () => {
    const bindOutput = execFileSync(python, ["-m", "apps.bob108_tool_activity_probe", "bind-evidence-context", "--run-dir", dirname(manifestPath!), "--origin-session-id", origin], {
      cwd: repoRoot, encoding: "utf8", timeout: 20_000, env: { ...process.env },
    });
    const evidenceContext = JSON.parse(bindOutput) as { memory_id: string; remember_call_id: string; source_sha256: string };
    expect(evidenceContext.memory_id).toBe(rememberMemoryId);

    startedAt = new Date().toISOString();
    const evidenceAsset = audioById.get("add-evidence")!;
    activeCase = { name: "add-evidence", sessionId: origin, screenshotName: "add-evidence-failed.png", beganAt: startedAt, expectedTools: ["add_evidence"], details: { wav: evidenceAsset, bound_context: evidenceContext } };
    await play(page, "add-evidence");
    const evidencePage = await pollActivity(page, origin, (events) => events.some((event) => event.event_type === "TOOL_RESULT" && event.payload.tool_name === "add_evidence"), overallDeadline(evidenceAsset.duration_seconds), undefined, received, watch);
    const evidenceEvents = evidencePage.events.filter((event) => event.payload.tool_name === "add_evidence");
    const evidenceSnapshot = snapshot(origin);
    const evidenceAction = evidenceSnapshot.actions.find((action) => action.tool_name === "add_evidence" && action.state === "done");
    expect(evidenceAction, "A real add_evidence done receipt must be persisted.").toBeTruthy();
    expect(evidenceEvents.some((event) => event.event_type === "TOOL_START")).toBeTruthy();
    expect(evidenceEvents.some((event) => event.event_type === "TOOL_RESULT")).toBeTruthy();
    expect(memoryIdFromReceipt(evidenceAction!)).toBe(evidenceContext.memory_id);
    const audit = evidenceSnapshot.belief_revision_audit.find((item) => item.memory_id === evidenceContext.memory_id);
    expect(audit, "The target memory must have a persisted evidence revision.").toBeTruthy();
    const evidenceFrame = caseFrames();
    await saveCase("add-evidence", page, origin, "add-evidence.png", startedAt, ["add_evidence"], {
      wav: evidenceAsset,
      provider: { model: realtimeConfig.model, call_id: evidenceAction!.actual_call_id, arguments: evidenceAction!.arguments },
      receipt: evidenceAction,
      bound_context: evidenceContext,
      revision_audit: audit,
      browser_realtime_protocol: evidenceFrame,
    }, "PASS");
    await waitForVoiceTurnComplete(page, origin, evidencePage, "add_evidence", overallDeadline(evidenceAsset.duration_seconds), received.slice(watch.frameStart));
    activeCase = null;
    }, rememberMemoryId ? undefined : "本轮 remember 没有真实 done memory UUID，add_evidence 依赖未满足。");

    await executeCase("background-tools", ["recall", "read_file"], async () => {
    startedAt = new Date().toISOString();
    const backgroundAsset = audioById.get("background-tools")!;
    activeCase = { name: "background-tools", sessionId: origin, screenshotName: "background-tools-failed.png", beganAt: startedAt, expectedTools: ["recall", "read_file"], details: { wav: backgroundAsset } };
    await play(page, "background-tools");
    const submitAction = await waitForSubmittedAction(page, origin, watch, overallDeadline(backgroundAsset.duration_seconds), received);
    const submitted = submittedTaskFromAction(submitAction);
    const taskSnapshot = snapshot(origin);
    const task = taskSnapshot.tasks.find((item) => item.id === submitted.taskId)!;
    expect(task?.task_session_id).toBe(submitted.taskSessionId);
    expect(taskSnapshot.work.some((item) => item.execution_id === submitted.workExecutionId && item.task_id === submitted.taskId)).toBeTruthy();
    const taskSessionId = task.task_session_id;
    activeCase.details.task = { task_id: task.id, task_session_id: taskSessionId };
    activeCase.toolActivity = { session_id: taskSessionId, events: [], cursor: {}, has_more: false };
    await page.getByRole("button", { name: "任务列表" }).click();
    const taskRow = page.getByRole("button", { name: /进入任务：/ }).last();
    await expect(taskRow).toBeVisible({ timeout: 30_000 });
    await navigateToTask(page, taskRow, taskSessionId);
    await ensureActivityExpanded(page);

    const eventTimes: { event: ActivityEvent; snapshot: Snapshot; observed_at: string }[] = [];
    activeCase.details.observed_event_state_snapshots = eventTimes;
    const expectedTools = ["recall", "read_file"];
    const needed = (events: ActivityEvent[]) => {
      const calls = new Map<string, Set<string>>();
      for (const event of events) {
        const callId = String(event.payload.call_id ?? "");
        const name = String(event.payload.tool_name ?? "");
        if (!callId) continue;
        const set = calls.get(callId) ?? new Set<string>();
        set.add(event.event_type);
        if (name) set.add(name);
        calls.set(callId, set);
      }
      return [...calls.values()].some((value) => value.has("recall") && value.has("TOOL_START") && value.has("TOOL_RESULT"))
        && [...calls.values()].filter((value) => value.has("read_file") && value.has("TOOL_START") && value.has("TOOL_RESULT")).length >= 2;
    };
    const stopAt = Date.now() + overallDeadline(backgroundAsset.duration_seconds) + 120_000;
    let taskActivity: ActivityPage = { session_id: taskSessionId, events: [], cursor: {}, has_more: false };
    const observedIds = new Set<string>();
    const collectedEvents = new Map<string, ActivityEvent>();
    while (Date.now() < stopAt) {
      const nextPage = await activity(page, taskSessionId, taskActivity.cursor);
      taskActivity = { ...nextPage, events: [...collectedEvents.values()] };
      activeCase.toolActivity = taskActivity;
      for (const event of nextPage.events) {
        if (observedIds.has(event.event_id)) continue;
        observedIds.add(event.event_id);
        collectedEvents.set(event.event_id, event);
        taskActivity.events = [...collectedEvents.values()];
        const state = snapshot(origin);
        if (["TOOL_START", "TOOL_RESULT"].includes(event.event_type) && expectedTools.includes(String(event.payload.tool_name))) {
          eventTimes.push({ event, snapshot: state, observed_at: new Date().toISOString() });
        }
      }
      if (needed(taskActivity.events)) break;
      await page.waitForTimeout(300);
    }
    if (!needed(taskActivity.events)) throw new Error("Real background provider did not produce recall plus README success and missing-file read_file call pairs.");
    expect(taskActivity.events.every((event) => event.session_id === taskSessionId)).toBeTruthy();
    const taskCalls = new Map<string, ActivityEvent[]>();
    for (const event of taskActivity.events) {
      const callId = String(event.payload.call_id ?? "");
      if (!callId) continue;
      taskCalls.set(callId, [...(taskCalls.get(callId) ?? []), event]);
    }
    const expectedPairs = [...taskCalls.values()].filter((events) => {
      const tool = String(events.find((event) => event.payload.tool_name)?.payload.tool_name ?? "");
      return expectedTools.includes(tool) && events.some((event) => event.event_type === "TOOL_START") && events.some((event) => event.event_type === "TOOL_RESULT");
    });
    expect(expectedPairs.filter((events) => events.some((event) => event.payload.tool_name === "recall"))).toHaveLength(1);
    expect(expectedPairs.filter((events) => events.some((event) => event.payload.tool_name === "read_file"))).toHaveLength(2);
    const requestEvents = taskActivity.events.filter((event) => event.event_type === "TOOL_START" && expectedTools.includes(String(event.payload.tool_name)));
    expect(requestEvents.length).toBeGreaterThanOrEqual(3);
    for (const observed of eventTimes.filter((item) => item.event.event_type === "TOOL_START")) {
      expect(observed.snapshot.tasks.find((item) => item.task_session_id === taskSessionId)?.product_state).toBe("working");
      expect(observed.snapshot.work.some((item) => item.status === "running" && item.task_id === task.id)).toBeTruthy();
    }
    const readFileResults = expectedPairs.filter((events) => events.some((event) => event.payload.tool_name === "read_file"))
      .map((events) => events.find((event) => event.event_type === "TOOL_RESULT")!);
    expect(readFileResults.some((event) => event.payload.success === true)).toBeTruthy();
    expect(readFileResults.some((event) => event.payload.success === false || Boolean(event.payload.error))).toBeTruthy();
    await expect.poll(() => snapshot(origin).tasks.find((item) => item.task_session_id === taskSessionId)?.product_state, { timeout: overallDeadline(backgroundAsset.duration_seconds) + 120_000 }).toBe("idle");
    await expect.poll(() => snapshot(origin).work.find((item) => item.task_id === task.id)?.status, { timeout: 30_000 }).toBe("completed");
    const completedTaskSnapshot = snapshot(origin);
    const originalToolMessagesBefore = completedTaskSnapshot.chat_messages.filter((message) => message.session_id === origin);
    expect(originalToolMessagesBefore.some((message) => message.role === "toolResult" || message.content_blocks.some((block) => typeof block === "object" && block !== null && "type" in block && ["toolCall", "toolResult"].includes(String((block as { type?: unknown }).type))))).toBe(true);
    const taskFrame = caseFrames();
    await expect.poll(async () => (await page.locator("aside").filter({ has: page.getByRole("heading", { name: "Activity", exact: true }) }).innerText()).includes("read_file"), { timeout: 60_000 }).toBeTruthy();
    await saveCase("background-tools", page, origin, "background-tools.png", startedAt, expectedTools, {
      wav: backgroundAsset,
      provider: { model: "qwen-plus", submit_call: taskSnapshot.actions.find((action) => action.tool_name === "submit")?.actual_call_id, submit_arguments: taskSnapshot.actions.find((action) => action.tool_name === "submit")?.arguments },
      task: { task_id: task.id, task_session_id: taskSessionId, task_state: completedTaskSnapshot.tasks.find((item) => item.task_session_id === taskSessionId)?.product_state, work: completedTaskSnapshot.work.filter((item) => item.task_id === task.id) },
      api_cursor: taskActivity.cursor,
      observed_event_state_snapshots: eventTimes,
      browser_realtime_protocol: taskFrame,
      task_activity: taskActivity.events,
    }, "PASS", undefined, taskActivity);

    const firstCount = await page.getByText(/read_file/).count();
    const replay = await activity(page, taskSessionId, {});
    expect(replay.events.map((event) => event.event_id)).toEqual(taskActivity.events.map((event) => event.event_id));
    await page.reload();
    await expect(page.getByRole("button", { name: "任务列表" })).toBeVisible({ timeout: 60_000 });
    await ensureActivityExpanded(page);
    const restored = await activity(page, taskSessionId, taskActivity.cursor);
    expect(restored.events).toHaveLength(0);
    await expect.poll(() => page.getByText(/read_file/).count(), { timeout: 30_000 }).toBe(firstCount);
    await page.getByRole("button", { name: "任务列表" }).click();
    await page.getByRole("button", { name: "返回来源会话" }).click();
    await expect(page.getByRole("button", { name: "Start Talk mode" })).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ path: join(evidenceDir, "origin-returned.png"), fullPage: true });
    const originAfterSnapshot = snapshot(origin);
    const originalToolMessagesAfter = originAfterSnapshot.chat_messages.filter((message) => message.session_id === origin);
    const originalToolMessages = originalChatMessagesEvidence(originalToolMessagesBefore, originalToolMessagesAfter);
    expect(originalToolMessages.unchanged, "Task/session Activity navigation must preserve the original origin chat message rows.").toBe(true);
    const backgroundCasePath = join(evidenceDir, "background-tools.json");
    const backgroundCase = JSON.parse(await readFile(backgroundCasePath, "utf8")) as { evidence: Record<string, unknown> };
    backgroundCase.evidence.snapshot = originAfterSnapshot;
    backgroundCase.evidence.original_tool_messages = originalToolMessages;
    await writeFile(backgroundCasePath, JSON.stringify(backgroundCase, null, 2), "utf8");
    expect(pageErrors).toEqual([]);
    activeCase = null;
    });
    const failedCases = outcomes.filter((outcome) => outcome.status !== "PASS");
    if (failedCases.length) throw new Error(`真实工具案例未全部通过：${JSON.stringify(failedCases)}`);
  } catch (error) {
    if (activeCase) {
      const failed = failureCaseRecord(activeCase, error, caseFrames());
      try {
        await saveCase(failed.name, page, failed.sessionId, failed.screenshotName, failed.beganAt,
          failed.expectedTools, failed.details, "FAIL", failed.reason, failed.toolActivity);
      } catch (captureError) {
        evidenceFailures.push(`${failed.name}: ${captureError instanceof Error ? captureError.message : String(captureError)}`);
      }
    }
    throw error;
  } finally {
    const stop = page.getByRole("button", { name: "Stop Talk mode" });
    if (await stop.isVisible().catch(() => false)) await stop.click().catch(() => {});
    await writeFile(join(evidenceDir, "browser-run.json"), JSON.stringify({
      run_id: run.run_id,
      origin_session_id: origin,
      browser_version: browser.version(),
      test_title: testInfo.title,
      failures: testInfo.errors.map((error) => error.message),
      page_errors: pageErrors,
      request_failures: requestFailures,
      evidence_failures: evidenceFailures,
      case_outcomes: outcomes,
      stimulus_block_reason: stimulusBlockReason,
      sent,
      received,
    }, null, 2), "utf8");
  }
});
