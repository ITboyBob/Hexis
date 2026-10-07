import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { writeFile } from "node:fs/promises";
import { isAbsolute, join } from "node:path";
import { expect, test } from "@playwright/test";
import { installVoiceWavFeed, prewarmVoiceUI } from "./fixtures/voice";

type MediaRun = { database: string; env_file: string; api_url: string; ui_url: string; python: string; source_root: string; source_fingerprints: Record<string, string>; wav: { path: string; sha256: string }; };
type Frame = { type: string; kind?: string; session_id?: string; media_session_id?: string; capture_instance_id?: string; pcm_base64?: string; [key: string]: unknown };
type SelectionRow = { selection_id: string; command_id: string; state: string; actual_call_id: string };
type TaskRow = { id: string; product_state: string; current_work_execution_id: string; revision: number };
type Snapshot = { selections: SelectionRow[]; pending: { id: string; task_id: string }[]; tasks: TaskRow[]; voice: { status: string; closed_reason: string | null }[]; events: Record<string, unknown>[]; task_agent_turns: number };
type Fixture = { working: { task_id: string }; idle: { task_id: string }; scope: string };
type MediaWindow = Window & { __voiceWavFeed?: { play: (name: string) => Promise<void>; trackState: () => { id: string; enabled: boolean; readyState: string } | null } };
const selectedRun = process.env.M5_MEDIA_RUN;
if (!selectedRun || !isAbsolute(selectedRun)) throw new Error("M5_MEDIA_RUN must select an absolute isolated run manifest.");
const run = JSON.parse(readFileSync(selectedRun, "utf8")) as MediaRun;
if (!run.database.startsWith("tmp_m5_") && !run.database.startsWith("m5_acceptance_")) throw new Error("The media test requires an isolated M5 database.");
for (const value of [run.api_url, run.ui_url]) {
  const url = new URL(value);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1") throw new Error("Explicit local API and UI URLs are required.");
}
test.use({ channel: "chrome", headless: false, baseURL: run.ui_url });

const pythonProbe = String.raw`
import asyncio,json,sys,asyncpg
from uuid import UUID,uuid4
from dotenv import dotenv_values
from services.m5_task_service import M5TaskService
from services.m5_report_receiver import M5ReportReceiver
run=json.loads(sys.argv[1]); action=sys.argv[2]; origin=UUID(sys.argv[3]) if len(sys.argv)>3 else None
s=dotenv_values(run['env_file'])
assert run['database'] != s['POSTGRES_DB']
def obj(value): return json.loads(value) if isinstance(value,str) else value
async def main():
 pool=await asyncpg.create_pool(host=s['POSTGRES_HOST'],port=int(s['POSTGRES_PORT']),user=s['POSTGRES_USER'],password=s['POSTGRES_PASSWORD'],database=run['database'],min_size=1,max_size=2)
 try:
  async with pool.acquire() as conn:
   assert await conn.fetchval('SELECT current_database()')==run['database']
   assert obj(await conn.fetchval('SELECT get_agent_status()'))['configured'] is True
   latest=await conn.fetchval('SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1')
   assert latest=='0267_m5_selections'
  if action=='readiness': return {'database':run['database'],'configured':True,'latest_migration':latest}
  service=M5TaskService(pool)
  if action=='seed':
   async with pool.acquire() as conn: assert await conn.fetchval("SELECT status='active' FROM chat_sessions WHERE id=$1",origin)
   def auth(label): return {'origin_session_id':str(origin),'input_turn_id':str(uuid4()),'input_order':0,'stimulus_revision':0,'response_id':'fixture-'+label,'supplier_response_id':'fixture-'+label,'request_id':'fixture-'+label,'actual_call_id':'fixture-'+label+'-'+str(uuid4())}
   working=await service.submit_m5_task(origin,uuid4(),'订单核算：三件18元与两件25元（S10受控夹具）','受控夹具；本次不是C1真实submit案例。',auth('working'))
   idle=await service.submit_m5_task(origin,uuid4(),'核对访谈目标与问题（S10受控夹具）','受控夹具；用于验证Idle选项。',auth('idle'))
   assert await service.claim_work_round(idle['work_execution_id'])
   assert await service.finish_work_round(task_id=idle['task_id'],work_execution_id=idle['work_execution_id'],task_session_id=idle['task_session_id'],status='completed',result={'fixture':True},error=None,turn_id=None)
   async with pool.acquire() as conn: report=await conn.fetchval('SELECT report_id FROM m5_reports WHERE work_execution_id=$1',idle['work_execution_id'])
   await M5ReportReceiver(pool).deliver(report,origin,'受控S10夹具已就绪；非真实云报告。','fixture-idle-report')
   return {'working':working,'idle':idle,'scope':'controlled fixture, not real submit/report acceptance'}
  assert action=='snapshot'
  async with pool.acquire() as conn:
   selections=await conn.fetch('SELECT selection_id,command_id,actual_call_id,"authorization",message,state,chosen_task_id,version FROM m5_selections WHERE origin_session_id=$1 ORDER BY created_at',origin)
   pending=await conn.fetch('SELECT p.id,p.task_id,p.command_id,p.receive_order,p.message,p.claimed_work_execution_id FROM m5_pending_messages p JOIN m5_tasks t ON t.id=p.task_id WHERE t.origin_session_id=$1 ORDER BY p.receive_order',origin)
   tasks=await conn.fetch('SELECT id,product_state,current_work_execution_id,revision FROM m5_tasks WHERE origin_session_id=$1 ORDER BY id',origin)
   events=await conn.fetch('SELECT event_type,seq,input_turn_id,response_id,call_id,media_session_id,payload FROM behavior_events WHERE session_id=$1 ORDER BY ts,seq',origin)
   voice=await conn.fetch('SELECT id,status,closed_reason,model FROM voice_realtime_sessions WHERE session_id=$1 ORDER BY created_at',origin)
   turns=await conn.fetchval('SELECT count(*) FROM agent_turns WHERE session_id IN (SELECT task_session_id FROM m5_tasks WHERE origin_session_id=$1)',origin)
  return {k:[dict(row) for row in rows] for k,rows in [('selections',selections),('pending',pending),('tasks',tasks),('events',events),('voice',voice)]}|{'task_agent_turns':turns}
 finally: await pool.close()
print(json.dumps(asyncio.run(main()),default=str,ensure_ascii=False))
`;

function dbProbe<T = Snapshot>(action: string, origin?: string): T {
  return JSON.parse(execFileSync(run.python, ["-c", pythonProbe, JSON.stringify(run), action, ...(origin ? [origin] : [])], { cwd: run.source_root, encoding: "utf8", timeout: 30_000 }));
}

function resumeTone(): string {
  const count = 3_520;
  const wav = Buffer.alloc(44 + count * 2);
  wav.write("RIFF", 0); wav.writeUInt32LE(wav.length - 8, 4); wav.write("WAVEfmt ", 8);
  wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(16_000, 24); wav.writeUInt32LE(32_000, 28); wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34);
  wav.write("data", 36); wav.writeUInt32LE(count * 2, 40);
  for (let i = 0; i < count; i += 1) wav.writeInt16LE(Math.round(8_000 * Math.sin(2 * Math.PI * 440 * i / 16_000)), 44 + i * 2);
  return wav.toString("base64");
}

test("真实Qwen WAV pend选择取消与确认保留同一Chrome媒体身份", async ({ page, browser }, testInfo) => {
  test.setTimeout(240_000);
  for (const [path, expectedHash] of Object.entries(run.source_fingerprints)) {
    expect(createHash("sha256").update(readFileSync(join(run.source_root, path))).digest("hex"), `source fingerprint: ${path}`).toBe(expectedHash);
  }
  const readiness = dbProbe<Record<string, unknown>>("readiness");
  const wav = readFileSync(run.wav.path);
  expect(createHash("sha256").update(wav).digest("hex")).toBe(run.wav.sha256);
  await installVoiceWavFeed(page, { pend: wav.toString("base64"), resume: resumeTone() });
  const sent: Frame[] = [], received: Frame[] = [], pageErrors: string[] = [];
  const selectionHttp: { url: string; body: unknown; plannedAction: string | null }[] = [], unscheduledSelectionRequests: string[] = [];
  const playbackHttpErrors: { status: number; request: unknown }[] = [];
  let plannedSelectionAction: "confirm" | "cancel" | null = null;
  const snapshots: Partial<Record<"before" | "cancelled" | "confirmed" | "closed" | "final", Snapshot>> = {};
  let origin = "", fixture: Fixture | null = null, failure: string | null = null;
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("response", (response) => {
    if (response.url().includes("/api/voice/playback") && response.status() >= 400) {
      playbackHttpErrors.push({ status: response.status(), request: response.request().postDataJSON() });
    }
  });
  page.on("request", (request) => {
    if (request.method() !== "POST" || !request.url().includes("/api/m5/selections/")) return;
    selectionHttp.push({ url: request.url(), body: request.postDataJSON(), plannedAction: plannedSelectionAction });
    if (!plannedSelectionAction || !request.url().endsWith(`/${plannedSelectionAction}`)) unscheduledSelectionRequests.push(request.url());
  });
  page.on("websocket", (socket) => {
    if (!socket.url().includes("/api/voice/realtime/")) return;
    socket.on("framesent", ({ payload }) => {
      const frame = JSON.parse(typeof payload === "string" ? payload : payload.toString()) as Frame;
      delete frame.pcm_base64;
      sent.push(frame);
    });
    socket.on("framereceived", ({ payload }) => {
      const frame = JSON.parse(typeof payload === "string" ? payload : payload.toString()) as Frame;
      delete frame.pcm_base64;
      received.push(frame);
    });
  });
  const messages = (type: string) => received.filter((frame) => frame.type === type);
  const play = (name: string) => page.evaluate(async (value) => {
    const feed = (window as MediaWindow).__voiceWavFeed;
    if (!feed) throw new Error("WAV feed is missing");
    await feed.play(value);
  }, name);
  const inputTrack = () => page.evaluate(() => {
    return (window as MediaWindow).__voiceWavFeed?.trackState() ?? null;
  });
  try {
    await prewarmVoiceUI(run.ui_url);
    await page.goto("/chat");
    const created = page.waitForResponse((response) => response.url().endsWith("/api/chat/session") && response.request().method() === "POST");
    await page.getByRole("button", { name: "Start new conversation" }).click();
    origin = (await (await created).json()).session_id;
    fixture = dbProbe<Fixture>("seed", origin);
    snapshots.before = dbProbe<Snapshot>("snapshot", origin);
    await page.getByRole("button", { name: "Start Talk mode" }).click();
    await expect(page.getByText("Listening", { exact: true })).toBeVisible({ timeout: 30_000 });
    await expect.poll(() => messages("ready").length).toBe(1);
    const track = await inputTrack();
    if (!track) throw new Error("The injected media track is unavailable.");
    const dialog = page.getByRole("dialog", { name: "选择要继续的任务" });

    await play("pend");
    await expect(dialog).toBeVisible({ timeout: 90_000 });
    await expect.poll(() => messages("capture_suspended").length).toBe(1);
    expect(await inputTrack()).toEqual({ ...track, enabled: false });
    await expect(dialog.getByRole("radio")).toHaveCount(2);
    await expect(dialog.getByText("Working", { exact: true })).toBeVisible();
    await expect(dialog.getByText("Idle", { exact: true })).toBeVisible();
    const pausedAt = sent.length;
    await play("pend");
    expect(unscheduledSelectionRequests, "selection requests must come from the scheduled Playwright action").toHaveLength(0);
    expect(sent.slice(pausedAt).filter((frame) => ["audio_chunk", "utterance_end"].includes(frame.type))).toHaveLength(0);
    await dialog.locator(`input[value="${fixture.working.task_id}"]`).check();
    await page.screenshot({ path: testInfo.outputPath("选择取消前.png") });
    const doneBeforeCancel = messages("generation").filter((frame) => frame.kind === "done").length;
    plannedSelectionAction = "cancel";
    await dialog.getByRole("button", { name: "取消，不追加" }).click();
    await expect(dialog).not.toBeVisible();
    plannedSelectionAction = null;
    await expect.poll(() => messages("capture_resumed").length).toBe(1);
    expect(await inputTrack()).toEqual(track);
    snapshots.cancelled = dbProbe<Snapshot>("snapshot", origin);
    expect(snapshots.cancelled.pending).toHaveLength(0);
    expect(snapshots.cancelled.selections).toHaveLength(1);
    expect(snapshots.cancelled.selections[0].state).toBe("cancelled");
    await expect.poll(() => messages("generation").filter((frame) => frame.kind === "done").length, { timeout: 45_000 }).toBeGreaterThan(doneBeforeCancel);

    await play("pend");
    await expect(dialog).toBeVisible({ timeout: 90_000 });
    await expect.poll(() => messages("capture_suspended").length).toBe(2);
    await dialog.locator(`input[value="${fixture.working.task_id}"]`).check();
    await page.screenshot({ path: testInfo.outputPath("选择确认前.png") });
    const doneBeforeConfirm = messages("generation").filter((frame) => frame.kind === "done").length;
    plannedSelectionAction = "confirm";
    await dialog.getByRole("button", { name: "确认追加" }).click();
    await expect(dialog).not.toBeVisible();
    plannedSelectionAction = null;
    await expect.poll(() => messages("capture_resumed").length).toBe(2);
    expect(await inputTrack()).toEqual(track);
    snapshots.confirmed = dbProbe<Snapshot>("snapshot", origin);
    expect(snapshots.confirmed.pending).toHaveLength(1);
    expect(snapshots.confirmed.pending[0].task_id).toBe(fixture.working.task_id);
    expect(snapshots.confirmed.selections.map((item) => item.state)).toEqual(["cancelled", "confirmed"]);
    expect(new Set(snapshots.confirmed.selections.map((item) => item.command_id)).size).toBe(2);
    expect(snapshots.confirmed.selections.every((item) => !item.actual_call_id.startsWith("fixture-"))).toBe(true);
    expect(snapshots.confirmed.task_agent_turns).toBe(0);
    await expect.poll(() => messages("generation").filter((frame) => frame.kind === "done").length, { timeout: 45_000 }).toBeGreaterThan(doneBeforeConfirm);
    const chunksBeforeResume = sent.filter((frame) => frame.type === "audio_chunk").length;
    await play("resume");
    await expect.poll(() => sent.filter((frame) => frame.type === "audio_chunk").length, { intervals: [50], timeout: 500 }).toBeGreaterThan(chunksBeforeResume);
    await page.getByRole("button", { name: "Stop Talk mode" }).click();
    await expect(page.getByRole("button", { name: "Start Talk mode" })).toBeVisible();
    expect(messages("ready")).toHaveLength(1);
    expect(messages("error")).toHaveLength(0);
    expect(playbackHttpErrors).toHaveLength(0);
    expect(pageErrors).toHaveLength(0);
    const capture = sent.find((frame) => frame.type === "capture_open");
    if (!capture) throw new Error("The original capture identity is missing.");
    expect(sent.filter((frame) => frame.capture_instance_id).every((frame) => frame.capture_instance_id === capture.capture_instance_id)).toBe(true);
    expect(sent.every((frame) => frame.session_id === origin && frame.media_session_id === messages("ready")[0].media_session_id)).toBe(true);
    await expect.poll(() => dbProbe<Snapshot>("snapshot", origin).voice.at(-1)?.status, { timeout: 5_000 }).toBe("closed");
    snapshots.closed = dbProbe<Snapshot>("snapshot", origin);
    expect(snapshots.closed.voice.at(-1)).toMatchObject({ status: "closed", closed_reason: "manual" });
    expect(snapshots.closed.tasks).toEqual(snapshots.confirmed.tasks);
  } catch (error) {
    failure = error instanceof Error ? error.message : String(error);
    throw error;
  } finally {
    if (origin) snapshots.final = dbProbe<Snapshot>("snapshot", origin);
    await writeFile(testInfo.outputPath("真实WAV选择证据.json"), JSON.stringify({ scope: "S10真实供应商pend与选择媒体链；任务夹具非C1真实submit，非真人声学", selectedRun, readiness, browser: browser.version(), fixture, origin, failure, pageErrors, playbackHttpErrors, selectionHttp, unscheduledSelectionRequests, sent, received, snapshots }, null, 2));
    const cancel = page.getByRole("button", { name: "取消，不追加" });
    if (await cancel.isVisible().catch(() => false)) { plannedSelectionAction = "cancel"; await cancel.click(); }
    const stop = page.getByRole("button", { name: "Stop Talk mode" });
    if (await stop.isVisible().catch(() => false)) await stop.click();
  }
});
