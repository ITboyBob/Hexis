import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { installVoiceWavFeed } from "./fixtures/voice";

type C3Record = {
  schema_version: 1;
  kind: "c3";
  env_id: string;
  database: string;
  api_url: string;
  api_start_id: string;
  acceptance_session_id: string;
  session_execution_policy: Record<string, string>;
  migration_verification: Record<string, unknown>;
  ui_origin: string;
  evidence_dir: string;
  configuration_fingerprints: Record<string, string>;
  records: Record<string, { path: string; sha256: string }>;
};

type D1Sample = {
  sample_id: string;
  split: "tuning" | "acceptance";
  path: string;
  file_sha256: string;
};

const c3Path = process.env.VOICE_BARGE_IN_RUN;
if (!c3Path || !path.isAbsolute(c3Path)) {
  throw new Error("VOICE_BARGE_IN_RUN must name an explicit absolute C3 JSON path; no resident service is selected by default.");
}

const c3 = JSON.parse(readFileSync(c3Path, "utf8")) as C3Record;
if (c3.schema_version !== 1 || c3.kind !== "c3" || !c3.env_id || !c3.api_url || !c3.records?.U1
  || !c3.acceptance_session_id || c3.session_execution_policy?.policy_id !== "m6-c5-realtime-only-v1") {
  throw new Error("Selected record is not a complete M6 C3 run manifest.");
}
if (c3.migration_verification?.status !== "verified_read_only" || c3.migration_verification.mode !== "committed_applied_only") {
  throw new Error("C3 requires read-only schema verification against committed migration sources.");
}

const sourceRoot = path.resolve(process.cwd(), "..");
const cacheRoot = path.resolve(path.dirname(c3Path), "..");
if (path.basename(path.dirname(c3Path)) !== "runs" || path.basename(cacheRoot) !== "voice-barge-in") {
  throw new Error("C3 must live under the selected Hexis voice-barge-in cache directory.");
}
const records = Object.fromEntries(Object.entries(c3.records).map(([name, record]) => {
  if (!path.isAbsolute(record.path)) throw new Error(`C3 ${name} path must be absolute.`);
  const relative = path.relative(cacheRoot, path.resolve(record.path));
  if (relative.startsWith("..") || path.isAbsolute(relative)) throw new Error(`C3 ${name} record is outside the selected cache root.`);
  const bytes = readFileSync(record.path);
  if (createHash("sha256").update(bytes).digest("hex") !== record.sha256) {
    throw new Error(`C3 ${name} record changed after preflight.`);
  }
  return [name, JSON.parse(bytes.toString("utf8")) as Record<string, unknown>];
}));
const uiRun = records.U1 as {
  kind?: string; mode?: string; status?: string; ui_url?: string; env_id?: string;
  runtime_identity?: { database?: Record<string, unknown>; api?: Record<string, unknown> };
};
const apiRun = records.API as { credential_env_names?: string[] };
const c6Path = path.resolve(sourceRoot, "config/voice-barge-in/run-profile.json");
if (createHash("sha256").update(readFileSync(c6Path)).digest("hex") !== c3.configuration_fingerprints.C6) {
  throw new Error("C6 changed after this C3 was created.");
}
const selectedPolicy = (JSON.parse(readFileSync(c6Path, "utf8")) as { session_execution_policy: Record<string, string> }).session_execution_policy;
for (const record of [c3, records.API, records.U1]) {
  const policy = record.session_execution_policy as Record<string, string> | undefined;
  if (!policy || Object.keys(policy).length !== Object.keys(selectedPolicy).length
    || Object.entries(selectedPolicy).some(([key, value]) => policy[key] !== value)
    || record.acceptance_session_id !== c3.acceptance_session_id
    || JSON.stringify(record.migration_verification) !== JSON.stringify(c3.migration_verification)) {
    throw new Error("C3/API/U1 acceptance session and execution policy must exactly match C6.");
  }
}
const d1 = JSON.parse(readFileSync(c3.records.D1.path, "utf8")) as { samples?: D1Sample[] };
if (uiRun.kind !== "talk_ui_run" || uiRun.mode !== "talk" || uiRun.status !== "ready" || uiRun.env_id !== c3.env_id) {
  throw new Error("C3 U1 record is not a ready Talk UI for the selected E1 environment.");
}
if (!uiRun.ui_url?.startsWith("http://127.0.0.1:")) throw new Error("C3 U1 must select an explicit loopback UI URL.");
if (c3.ui_origin !== new URL(uiRun.ui_url).origin || records.API.ui_origin !== c3.ui_origin || records.U1.ui_origin !== c3.ui_origin) {
  throw new Error("C3/API/U1 UI origin must exactly match the actual selected Talk page origin.");
}

const c1Path = path.resolve(sourceRoot, "hexis-ui/lib/voice-realtime/speech-detection-config.json");
if (createHash("sha256").update(readFileSync(c1Path)).digest("hex") !== c3.configuration_fingerprints.C1) {
  throw new Error("C1 changed after this C3 was created.");
}
const c5Path = path.resolve(sourceRoot, "config/qwen-realtime.json");
if (createHash("sha256").update(readFileSync(c5Path)).digest("hex") !== c3.configuration_fingerprints.C5) {
  throw new Error("C5 changed after this C3 was created.");
}
const providerKeyEnv = (JSON.parse(readFileSync(c5Path, "utf8")) as { api_key_env?: string }).api_key_env;
const cloudTestsEnabled = process.env.VOICE_BARGE_IN_CLOUD_TESTS === "1";
if (cloudTestsEnabled && (!providerKeyEnv || !apiRun.credential_env_names?.includes(providerKeyEnv))) {
  throw new Error("Cloud Talk tests require a new C3/API start record that names the C5 provider key environment variable.");
}

function selectedSample(sampleId: string, expectedSplit: D1Sample["split"]): { name: string; base64: string; sha256: string } {
  const sample = d1.samples?.find((item) => item.sample_id === sampleId && item.split === expectedSplit);
  if (!sample || !path.isAbsolute(sample.path)) throw new Error(`C3 D1 is missing selected ${expectedSplit} sample ${sampleId}.`);
  const bytes = readFileSync(sample.path);
  if (createHash("sha256").update(bytes).digest("hex") !== sample.file_sha256) {
    throw new Error(`D1 WAV changed after C3 creation: ${sampleId}`);
  }
  return { name: sampleId, base64: bytes.toString("base64"), sha256: sample.file_sha256 };
}

test.use({ channel: "chrome", headless: false, baseURL: uiRun.ui_url });

test.describe("M6 Talk runtime identity", () => {
  test("renders the selected Talk UI and re-reads the exact E1 database/API identity without starting a provider session", async ({ page, request }) => {
    const runtimeResponse = await request.get("/api/voice-barge-in/runtime-identity");
    expect(runtimeResponse.status()).toBe(200);
    const runtimeIdentity = await runtimeResponse.json();
    expect(runtimeIdentity.env_id).toBe(c3.env_id);
    expect(runtimeIdentity.database).toMatchObject(uiRun.runtime_identity?.database ?? {});
    expect(runtimeIdentity.api).toMatchObject(uiRun.runtime_identity?.api ?? {});
    expect(runtimeIdentity.database.database).toBe(c3.database);

    const sessionRequests: string[] = [];
    page.on("request", (requestEvent) => {
      if (new URL(requestEvent.url()).pathname === "/api/voice/realtime/sessions") {
        sessionRequests.push(requestEvent.method());
      }
    });
    await page.goto("/chat");
    await expect(page.getByRole("button", { name: "Start Talk mode" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Stop Talk mode" })).toHaveCount(0);
    expect(sessionRequests).toEqual([]);
  });

  test("does not open capture or WebSocket when the selected realtime session is not ready", async ({ page }) => {
    await installVoiceWavFeed(page, {});
    await page.route("**/api/chat/session", (route) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ session_id: "c3-no-ready-smoke" }),
    }));
    let createSessionCalls = 0;
    await page.route("**/api/voice/realtime/sessions", async (route) => {
      createSessionCalls += 1;
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: "synthetic no-ready fault" }),
      });
    });
    const websocketUrls: string[] = [];
    page.on("websocket", (socket) => {
      if (new URL(socket.url()).pathname.startsWith("/api/voice/realtime/")) {
        websocketUrls.push(socket.url());
      }
    });

    await page.goto("/chat");
    await page.getByRole("button", { name: "Start Talk mode" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "synthetic no-ready fault" })).toBeVisible();
    await expect(page.getByTestId("voice-status-card")).toContainText("Talk needs attention");
    await expect(page.getByText("Listening", { exact: true })).toHaveCount(0);
    expect(createSessionCalls).toBe(1);
    expect(websocketUrls).toEqual([]);
    const track = await page.evaluate(() => (window as Window & {
      __voiceWavFeed?: { trackState: () => { id: string; enabled: boolean; readyState: string } | null };
    }).__voiceWavFeed?.trackState());
    expect(track?.readyState).toBe("ended");
  });
});

test.describe("M6 confirmed interruption with the selected C3", () => {
  test("keeps audible target through pure noise, then interrupts that same target once and commits speech", async ({ page }, testInfo) => {
    test.skip(!cloudTestsEnabled, "requires the separately authorized C5 provider key to be selected in the API start record");
    if (!providerKeyEnv || !apiRun.credential_env_names?.includes(providerKeyEnv)) {
      throw new Error("C3/API record does not name the selected C5 provider environment key.");
    }
    const prompt = selectedSample("ordinary-tuning-01", "tuning");
    const noise = selectedSample("pure-noise-tuning-01", "tuning");
    const interruption = selectedSample("short-stop-acceptance-01", "acceptance");
    await installVoiceWavFeed(page, {
      prompt: prompt.base64,
      noise: noise.base64,
      interruption: interruption.base64,
    });
    const registrationResponse = await page.request.post("/api/chat/session");
    expect(registrationResponse.ok()).toBe(true);
    const registration = await registrationResponse.json() as Record<string, unknown>;
    expect(registration).toMatchObject({
      session_id: c3.acceptance_session_id, api_start_id: c3.api_start_id,
      session_execution_policy: c3.session_execution_policy,
    });
    await page.route("**/api/voice/realtime/sessions", async (route) => {
      expect(route.request().postDataJSON()).toMatchObject({ session_id: c3.acceptance_session_id, tool_surface: "m5" });
      await route.continue();
    });

    const clientMessages: Record<string, unknown>[] = [];
    const serverMessages: Record<string, unknown>[] = [];
    const playbackReceiptResponses: Array<{ receipt: Record<string, unknown>; status: number }> = [];
    const sessionResponseMessages: Record<string, unknown>[] = [];
    page.on("websocket", (socket) => {
      if (!new URL(socket.url()).pathname.startsWith("/api/voice/realtime/")) return;
      socket.on("framesent", ({ payload }) => {
        const text = typeof payload === "string" ? payload : payload.toString("utf8");
        try { clientMessages.push(JSON.parse(text) as Record<string, unknown>); } catch { /* ignore non-JSON provider frames */ }
      });
      socket.on("framereceived", ({ payload }) => {
        const text = typeof payload === "string" ? payload : payload.toString("utf8");
        try { serverMessages.push(JSON.parse(text) as Record<string, unknown>); } catch { /* ignore non-JSON provider frames */ }
      });
    });
    page.on("response", async (response) => {
      const pathname = new URL(response.url()).pathname;
      if (pathname === "/api/voice/realtime/sessions") {
        try { sessionResponseMessages.push(await response.json() as Record<string, unknown>); } catch { /* non-JSON response is not ready evidence */ }
        return;
      }
      if (pathname !== "/api/voice/playback") return;
      try {
        const receipt = response.request().postDataJSON() as Record<string, unknown>;
        playbackReceiptResponses.push({ receipt, status: response.status() });
      } catch { /* non-JSON playback requests are not evidence */ }
    });

    await page.goto("/chat");
    await page.getByRole("button", { name: "Start Talk mode" }).click();
    const status = page.getByTestId("voice-status-card");
    await expect(status.getByText("Listening", { exact: true })).toBeVisible({ timeout: 60_000 });
    await page.evaluate(async () => {
      const feed = (window as Window & { __voiceWavFeed?: { play: (name: string) => Promise<void> } }).__voiceWavFeed;
      if (!feed) throw new Error("C3 WAV feed was not installed.");
      await feed.play("prompt");
    });
    await expect.poll(() => serverMessages.filter((message) => {
      const event = message.type === "event" ? message.event as Record<string, unknown> | undefined : undefined;
      return event?.event_type === "voice_input_committed";
    }).length, { timeout: 30_000 }).toBeGreaterThan(0);
    const promptInputEvent = serverMessages.find((message) => {
      const event = message.type === "event" ? message.event as Record<string, unknown> | undefined : undefined;
      return event?.event_type === "voice_input_committed";
    })!;
    const promptEvent = promptInputEvent.event as Record<string, unknown>;
    const promptIdentity = (promptEvent.payload as Record<string, unknown>).identity as Record<string, unknown>;
    const promptInputTurnId = promptIdentity.input_turn_id;
    const promptStimulusRevision = promptIdentity.stimulus_revision;
    await expect(status.getByText("Speaking", { exact: true })).toBeVisible({ timeout: 60_000 });
    const targetStarted = [...serverMessages].reverse().find((message) => {
      const event = message.type === "event" ? message.event as Record<string, unknown> | undefined : undefined;
      const payload = event?.payload as Record<string, unknown> | undefined;
      const identity = payload?.identity as Record<string, unknown> | undefined;
      return event?.event_type === "voice_model_call_started" && identity?.input_turn_id === promptInputTurnId;
    });
    expect(targetStarted).toBeTruthy();
    const targetPayload = ((targetStarted?.event as Record<string, unknown>).payload ?? {}) as Record<string, unknown>;
    const targetIdentity = (targetPayload.identity ?? {}) as Record<string, unknown>;
    expect(targetIdentity.input_turn_id).toBe(promptInputTurnId);
    expect(targetPayload.response_id).toBeTruthy();
    expect(targetPayload.playback_id).toBeTruthy();
    const priorCommands = clientMessages.filter((message) => message.type === "barge_in_confirmed").length;

    await page.evaluate(async () => {
      const feed = (window as Window & { __voiceWavFeed?: { play: (name: string) => Promise<void> } }).__voiceWavFeed;
      await feed?.play("noise");
    });
    await expect(status.getByText("Speaking", { exact: true })).toBeVisible();
    expect(clientMessages.filter((message) => message.type === "barge_in_confirmed")).toHaveLength(priorCommands);

    const priorCommittedInputs = serverMessages.filter((message) => {
      const event = message.type === "event" ? message.event as Record<string, unknown> | undefined : undefined;
      return event?.event_type === "voice_input_committed";
    }).length;
    await page.evaluate(async () => {
      const feed = (window as Window & { __voiceWavFeed?: { play: (name: string) => Promise<void> } }).__voiceWavFeed;
      await feed?.play("interruption");
    });
    await expect.poll(() => clientMessages.filter((message) => message.type === "barge_in_confirmed").length, { timeout: 30_000 })
      .toBe(priorCommands + 1);
    const command = clientMessages.filter((message) => message.type === "barge_in_confirmed").at(-1)!;
    expect(command.response_id).toBe(targetPayload.response_id);
    expect(command.playback_id).toBe(targetPayload.playback_id);
    expect(command.output_epoch).toBe(targetPayload.output_epoch);
    expect(command.local_playback_state).toBe("started");
    await expect.poll(() => serverMessages.find((message) =>
      message.type === "barge_in_result" && message.command_id === command.command_id,
    ), { timeout: 30_000 }).toBeTruthy();
    const matchingResult = serverMessages.find((message) =>
      message.type === "barge_in_result" && message.command_id === command.command_id,
    )!;
    expect(matchingResult.response_id).toBe(targetPayload.response_id);
    expect(["accepted", "playback_only"]).toContain(matchingResult.status);
    // playback_only 的生成先结束顺序由 E1 只读 DB 回读验证，不能作为云 cancel 证据。
    // 确认打断回执早于短词结束、静音尾帧、水位收齐和 provider final 转录。
    await expect.poll(() => serverMessages.filter((message) => {
      const event = message.type === "event" ? message.event as Record<string, unknown> | undefined : undefined;
      return event?.event_type === "voice_input_committed";
    }).length, { timeout: 30_000 }).toBeGreaterThan(priorCommittedInputs);
    const committedInputs = serverMessages.filter((message) => {
      const event = message.type === "event" ? message.event as Record<string, unknown> | undefined : undefined;
      return event?.event_type === "voice_input_committed";
    });
    expect(committedInputs.length).toBeGreaterThan(priorCommittedInputs);

    const interruptionInputEvent = committedInputs.slice(priorCommittedInputs).at(-1)!;
    const interruptionEvent = interruptionInputEvent.event as Record<string, unknown>;
    const interruptionIdentity = (interruptionEvent.payload as Record<string, unknown>).identity as Record<string, unknown>;
    const interruptionInputTurnId = interruptionIdentity.input_turn_id;
    const interruptionStimulusRevision = interruptionIdentity.stimulus_revision;
    await expect.poll(() => [...serverMessages].reverse().find((message) => {
      const event = message.type === "event" ? message.event as Record<string, unknown> | undefined : undefined;
      const payload = event?.payload as Record<string, unknown> | undefined;
      const identity = payload?.identity as Record<string, unknown> | undefined;
      return event?.event_type === "voice_model_call_started" && identity?.input_turn_id === interruptionInputTurnId;
    }), { timeout: 60_000 }).toBeTruthy();
    const nextResponseStarted = [...serverMessages].reverse().find((message) => {
      const event = message.type === "event" ? message.event as Record<string, unknown> | undefined : undefined;
      const payload = event?.payload as Record<string, unknown> | undefined;
      const identity = payload?.identity as Record<string, unknown> | undefined;
      return event?.event_type === "voice_model_call_started" && identity?.input_turn_id === interruptionInputTurnId;
    })!;
    const nextResponsePayload = (nextResponseStarted.event as Record<string, unknown>).payload as Record<string, unknown>;
    const nextResponseId = nextResponsePayload.response_id;
    const nextPlaybackId = nextResponsePayload.playback_id;
    const nextOutputEpoch = nextResponsePayload.output_epoch;
    expect(nextResponseId).toBeTruthy();
    await expect(status.getByText("Speaking", { exact: true })).toBeVisible({ timeout: 60_000 });
    await expect.poll(() => playbackReceiptResponses.some(({ receipt, status: responseStatus }) =>
      receipt.action === "end" && receipt.response_id === nextResponseId && responseStatus === 200,
    ), { timeout: 120_000 }).toBe(true);
    const nextTerminalReceipt = playbackReceiptResponses.find(({ receipt, status: responseStatus }) =>
      receipt.action === "end" && receipt.response_id === nextResponseId && responseStatus === 200,
    )!;
    const stopRequestedAt = Date.now();
    const stopWaitTimeoutMs = 20_000;
    await page.getByRole("button", { name: "Stop Talk mode" }).click();
    await expect(page.getByRole("button", { name: "Start Talk mode" })).toBeVisible({ timeout: stopWaitTimeoutMs });
    const stopCompletedAt = Date.now();

    const ready = serverMessages.find((message) => message.type === "ready");
    const captureOpen = clientMessages.find((message) => message.type === "capture_open");
    const sessionResponseMessage = sessionResponseMessages[0];
    expect(sessionResponseMessage).toBeTruthy();
    const priorReadyCount = serverMessages.filter((message) => message.type === "ready").length;
    const priorSessionResponseCount = sessionResponseMessages.length;
    await page.getByRole("button", { name: "Start Talk mode" }).click();
    await expect(status.getByText("Listening", { exact: true })).toBeVisible({ timeout: 60_000 });
    await expect.poll(() => sessionResponseMessages.length, { timeout: 10_000 })
      .toBe(priorSessionResponseCount + 1);
    await expect.poll(() => serverMessages.filter((message) => message.type === "ready").length, { timeout: 30_000 })
      .toBe(priorReadyCount + 1);
    const reopenedSessionResponse = sessionResponseMessages.at(-1)!;
    const reopenedReady = [...serverMessages].filter((message) => message.type === "ready").at(-1)!;
    const reopenedCaptureOpen = [...clientMessages].filter((message) => message.type === "capture_open").at(-1)!;
    expect(reopenedSessionResponse.session_id).toBe(c3.acceptance_session_id);
    expect(reopenedReady.session_id).toBe(c3.acceptance_session_id);
    expect(reopenedSessionResponse.media_session_id).not.toBe(sessionResponseMessage!.media_session_id);
    expect(reopenedReady.media_session_id).toBe(reopenedSessionResponse.media_session_id);
    expect(reopenedReady.cloud_session_id).not.toBe(ready?.cloud_session_id);
    expect(reopenedCaptureOpen.media_session_id).toBe(reopenedSessionResponse.media_session_id);
    await page.getByRole("button", { name: "Stop Talk mode" }).click();
    await expect(page.getByRole("button", { name: "Start Talk mode" })).toBeVisible({ timeout: stopWaitTimeoutMs });
    const reopenStoppedAt = new Date().toISOString();
    const promptCommittedMessage = committedInputs.find((message) => {
      const event = message.type === "event" ? message.event as Record<string, unknown> | undefined : undefined;
      return event?.event_type === "voice_input_committed";
    });
    expect(sessionResponseMessage).toBeTruthy();
    expect(sessionResponseMessage?.session_id).toBe(c3.acceptance_session_id);
    expect(ready).toBeTruthy();
    expect(captureOpen).toBeTruthy();
    expect(promptCommittedMessage).toBeTruthy();
    const mediaSessionId = sessionResponseMessage!.media_session_id;
    const cloudSessionId = ready!.cloud_session_id;
    expect(ready).toMatchObject({ session_id: sessionResponseMessage!.session_id, media_session_id: mediaSessionId });
    expect(captureOpen).toMatchObject({ session_id: sessionResponseMessage!.session_id, media_session_id: mediaSessionId });
    expect(command.capture_instance_id).toBe(captureOpen!.capture_instance_id);
    const observation = {
      schema_version: 3,
      kind: "m6_talk_barge_in_observation",
      c3_path: c3Path,
      c3_sha256: createHash("sha256").update(readFileSync(c3Path)).digest("hex"),
      env_id: c3.env_id,
      api_start_id: c3.api_start_id,
      session_execution_policy: c3.session_execution_policy,
      acceptance_session_id: c3.acceptance_session_id,
      migration_verification: c3.migration_verification,
      stop_requested_at_utc: new Date(stopRequestedAt).toISOString(),
      stop_completed_at_utc: new Date(stopCompletedAt).toISOString(),
      stop_elapsed_ms: stopCompletedAt - stopRequestedAt,
      stop_wait_timeout_ms: stopWaitTimeoutMs,
      session_id: sessionResponseMessage!.session_id,
      media_session_id: mediaSessionId,
      cloud_session_id: cloudSessionId,
      capture_instance_id: captureOpen!.capture_instance_id,
      session_response: sessionResponseMessage,
      ready,
      capture_open: captureOpen,
      media_reopen_sessions: [{
        session_response: reopenedSessionResponse,
        ready: reopenedReady,
        capture_open: reopenedCaptureOpen,
        stop_completed: true,
        stopped_at_utc: reopenStoppedAt,
      }],
      prompt_input_order: promptIdentity.input_order,
      prompt_input_turn_id: promptInputTurnId,
      prompt_stimulus_revision: promptStimulusRevision,
      prompt_input_committed: promptCommittedMessage,
      target_model_call_started: targetStarted,
      interruption_input_turn_id: interruptionInputTurnId,
      interruption_input_order: interruptionIdentity.input_order,
      interruption_stimulus_revision: interruptionStimulusRevision,
      interruption_input_committed: interruptionInputEvent,
      next_model_call_started: nextResponseStarted,
      target_input_turn_id: targetIdentity.input_turn_id,
      sample_ids: [prompt.name, noise.name, interruption.name],
      prompt_sha256: prompt.sha256,
      noise_sha256: noise.sha256,
      interruption_sha256: interruption.sha256,
      target_response_id: targetPayload.response_id,
      target_playback_id: targetPayload.playback_id,
      target_output_epoch: targetPayload.output_epoch,
      target_identity: {
        session_id: sessionResponseMessage!.session_id,
        media_session_id: mediaSessionId,
        response_id: targetPayload.response_id,
        playback_id: targetPayload.playback_id,
        output_epoch: targetPayload.output_epoch,
        input_turn_id: targetIdentity.input_turn_id,
        stimulus_revision: targetIdentity.stimulus_revision,
      },
      target_was_audible: true,
      no_barge_commands_during_noise: true,
      command,
      command_result: matchingResult,
      barge_in_evidence_scope: matchingResult.status === "accepted"
        ? "active_generation_cancel_command" : "local_playback_interrupt_after_generation",
      provider_cancel_terminal_verified: false,
      next_response_id: nextResponseId,
      next_playback_id: nextPlaybackId,
      next_output_epoch: nextOutputEpoch,
      next_identity: {
        session_id: sessionResponseMessage!.session_id,
        media_session_id: mediaSessionId,
        response_id: nextResponseId,
        playback_id: nextPlaybackId,
        output_epoch: nextOutputEpoch,
        input_turn_id: interruptionInputTurnId,
        stimulus_revision: interruptionStimulusRevision,
      },
      next_response_end_receipt_persisted: true,
      next_response_terminal_receipt: nextTerminalReceipt.receipt,
      next_response_terminal_http_status: nextTerminalReceipt.status,
      playback_receipts_observed: playbackReceiptResponses.map(({ receipt, status: receiptStatus }) => ({ receipt, status: receiptStatus })),
      committed_input_count: committedInputs.length,
      observed_at_utc: new Date().toISOString(),
    };
    const evidencePath = path.join(c3.evidence_dir, `talk-barge-in-${Date.now()}-${process.pid}.json`);
    const relativeEvidence = path.relative(cacheRoot, path.resolve(evidencePath));
    if (relativeEvidence.startsWith("..") || path.isAbsolute(relativeEvidence)) {
      throw new Error("C3 evidence directory is outside the selected Hexis cache.");
    }
    writeFileSync(evidencePath, `${JSON.stringify(observation, null, 2)}\n`, { flag: "wx", mode: 0o600 });
    await testInfo.attach("m6-talk-barge-in-observation", {
      contentType: "application/json",
      body: Buffer.from(JSON.stringify(observation, null, 2)),
    });
  });
});
