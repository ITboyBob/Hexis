import { readFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import { installVoiceWavFeed, startVoiceServices, type VoiceServiceHandle } from "./fixtures/voice";

const audioRoot = "/Users/itboybob/.cache/hexis/m4-test-audio-2026-10-05";
const audioNames = ["turn-01.wav", "turn-02.wav", "recall.wav", "interrupt.wav", "recovery.wav", "continuation.wav"];
let services: VoiceServiceHandle | null = null;

type AvatarServerEvent = {
  event_type: string;
  seq: number;
  producer_seq: number;
  session_id: string;
  media_session_id: string;
  payload: Record<string, unknown>;
};

test.beforeAll(async () => {
  services = await startVoiceServices();
});

test.afterAll(async () => {
  await services?.close();
  services = null;
});

test.afterEach(async ({ page }) => {
  const stop = page.getByRole("button", { name: "Stop Talk mode" });
  if (await stop.isVisible().catch(() => false)) {
    await stop.click({ timeout: 5_000 });
    await expect(page.getByRole("button", { name: "Start Talk mode" })).toBeVisible({ timeout: 10_000 });
  }
});

test("real Chat carries current Avatar events across recall, interruption, recovery, and reopen", async ({ page }, testInfo) => {
  test.setTimeout(360_000);
  const encodedWavs: Record<string, string> = {};
  for (const name of audioNames) {
    encodedWavs[name] = (await readFile(`${audioRoot}/${name}`)).toString("base64");
  }
  await installVoiceWavFeed(page, encodedWavs);

  const avatarEvents: AvatarServerEvent[] = [];
  const readyMessages: Array<{ session_id: string; media_session_id: string }> = [];
  const sentControls: string[] = [];
  const receivedMessages: Array<Record<string, unknown>> = [];
  const browserErrors: string[] = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("websocket", (socket) => {
    if (!socket.url().includes("/api/voice/realtime/")) return;
    socket.on("framesent", (frame) => {
      const raw = typeof frame.payload === "string" ? frame.payload : frame.payload.toString();
      try {
        const message = JSON.parse(raw) as Record<string, unknown>;
        if (message.type === "control" && typeof message.action === "string") sentControls.push(message.action);
      } catch {
        // The transport contract requires JSON; malformed frames remain visible through the missing assertions.
      }
    });
    socket.on("framereceived", (frame) => {
      const raw = typeof frame.payload === "string" ? frame.payload : frame.payload.toString();
      try {
        const message = JSON.parse(raw) as Record<string, unknown>;
        receivedMessages.push(message);
        if (message.type === "ready" && typeof message.session_id === "string" && typeof message.media_session_id === "string") {
          readyMessages.push({ session_id: message.session_id, media_session_id: message.media_session_id });
        }
        if (message.type !== "event" || !message.event || typeof message.event !== "object") return;
        const event = message.event as Record<string, unknown>;
        if (typeof event.event_type !== "string" || !Number.isInteger(event.seq) || !Number.isInteger(event.producer_seq)
          || typeof event.session_id !== "string" || typeof message.media_session_id !== "string"
          || !event.payload || typeof event.payload !== "object") return;
        avatarEvents.push({
          event_type: event.event_type,
          seq: event.seq as number,
          producer_seq: event.producer_seq as number,
          session_id: event.session_id,
          media_session_id: message.media_session_id,
          payload: event.payload as Record<string, unknown>,
        });
      } catch {
        // Captured protocol frames are summarized below without storing audio bytes.
      }
    });
  });

  await page.goto("/chat");
  await page.getByRole("button", { name: "Start Talk mode" }).click();
  await expect(page.getByRole("button", { name: "Stop Talk mode" })).toBeVisible();
  await expect(page.getByTestId("voice-avatar")).toBeVisible();
  await expect.poll(() => readyMessages.length, { timeout: 30_000 }).toBe(1);
  const initialMediaSession = readyMessages[0].media_session_id;
  let generationDoneCount = 0;

  const play = async (name: string) => page.evaluate(async (wavName) => {
    const feed = (window as Window & { __voiceWavFeed?: { play: (value: string) => Promise<void> } }).__voiceWavFeed;
    if (!feed) throw new Error("Voice WAV feed is not installed");
    await feed.play(wavName);
  }, name);
  const waitForInput = async (since: number) => {
    await expect.poll(
      () => avatarEvents.slice(since).some((event) => event.event_type === "voice_input_committed"),
      { timeout: 90_000 },
    ).toBe(true);
  };
  const waitForGenerationDone = async (target: number) => {
    await expect.poll(
      () => receivedMessages.filter((message) => message.type === "generation" && message.kind === "done").length,
      { timeout: 90_000 },
    ).toBeGreaterThanOrEqual(target);
    generationDoneCount = receivedMessages.filter((message) => message.type === "generation" && message.kind === "done").length;
  };
  const waitForRecall = async (eventType: "voice_recall_started" | "voice_recall_ended", since: number) => {
    const deadline = Date.now() + 45_000;
    while (Date.now() < deadline) {
      if (avatarEvents.slice(since).some((event) => event.event_type === eventType)) return true;
      await page.waitForTimeout(250);
    }
    return avatarEvents.slice(since).some((event) => event.event_type === eventType);
  };

  let cursor = avatarEvents.length;
  await play("turn-01.wav");
  await waitForInput(cursor);
  await waitForGenerationDone(1);
  const firstInput = avatarEvents.slice(cursor).find((event) => event.event_type === "voice_input_committed");
  const firstIdentity = firstInput?.payload.identity as Record<string, unknown> | undefined;
  expect(firstIdentity).toMatchObject({
    session_id: readyMessages[0].session_id,
    input_turn_id: expect.any(String),
    input_order: expect.any(Number),
    stimulus_revision: expect.any(Number),
  });
  expect(avatarEvents.slice(cursor).some((event) => event.event_type === "voice_visual_window")).toBe(true);
  expect(avatarEvents.slice(cursor).some((event) => event.event_type === "voice_model_call_started")).toBe(true);
  expect(avatarEvents.slice(cursor).every((event) => event.seq > 0 && event.producer_seq >= 0)).toBe(true);

  cursor = avatarEvents.length;
  await play("recall.wav");
  await waitForInput(cursor);
  const recallTriggered = await waitForRecall("voice_recall_started", cursor);
  const recallCompleted = recallTriggered
    ? await waitForRecall("voice_recall_ended", cursor)
    : false;
  await waitForGenerationDone(generationDoneCount + 1);

  cursor = avatarEvents.length;
  const audioChunksBefore = receivedMessages.filter((message) => message.type === "audio_chunk").length;
  await play("turn-02.wav");
  await waitForInput(cursor);
  await expect.poll(
    () => receivedMessages.filter((message) => message.type === "audio_chunk").length,
    { timeout: 45_000 },
  ).toBeGreaterThan(audioChunksBefore);
  await play("interrupt.wav");
  await expect.poll(() => sentControls.includes("barge_in"), { timeout: 45_000 }).toBe(true);

  const generationDoneBeforeRecovery = receivedMessages.filter(
    (message) => message.type === "generation" && message.kind === "done",
  ).length;
  cursor = avatarEvents.length;
  await play("recovery.wav");
  await waitForInput(cursor);
  await waitForGenerationDone(generationDoneBeforeRecovery + 1);

  await page.getByRole("button", { name: "Stop Talk mode" }).click();
  await expect(page.getByTestId("voice-avatar")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Start Talk mode" })).toBeVisible();
  await page.getByRole("button", { name: "Start Talk mode" }).click();
  await expect(page.getByTestId("voice-avatar")).toBeVisible();
  await expect.poll(() => readyMessages.length, { timeout: 30_000 }).toBe(2);
  expect(readyMessages[1].session_id).toBe(readyMessages[0].session_id);
  expect(readyMessages[1].media_session_id).not.toBe(initialMediaSession);

  cursor = avatarEvents.length;
  await play("continuation.wav");
  await waitForInput(cursor);
  const reopenedEvent = avatarEvents.slice(cursor).find((event) => event.event_type === "voice_input_committed");
  expect(reopenedEvent?.media_session_id).toBe(readyMessages[1].media_session_id);
  expect(avatarEvents.map((event) => event.seq)).toEqual(
    [...avatarEvents.map((event) => event.seq)].sort((left, right) => left - right),
  );
  expect(browserErrors).toEqual([]);
  await testInfo.attach("voice-round2-avatar-events", {
    body: JSON.stringify({
      mediaSessions: readyMessages.map((item) => item.media_session_id),
      events: avatarEvents.map(({ event_type, seq, producer_seq, media_session_id }) => ({ event_type, seq, producer_seq, media_session_id })),
      controls: sentControls,
      generationDoneCount,
      recallTriggered,
      recallCompleted,
    }),
    contentType: "application/json",
  });
  await page.getByRole("button", { name: "Stop Talk mode" }).click();
  await expect(page.getByRole("button", { name: "Start Talk mode" })).toBeVisible();
  expect(recallTriggered, "recall.wav did not produce a real voice_recall_started event; record as uncovered, do not substitute fixture events").toBe(true);
  expect(recallCompleted, "the real synchronous recall did not produce a terminal event").toBe(true);
});
