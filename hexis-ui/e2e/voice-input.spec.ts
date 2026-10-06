import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import { startVoiceServices, type VoiceServiceHandle } from "./fixtures/voice";

const inputWav =
  "/Users/itboybob/.cache/hexis/m4-test-audio-2026-10-05/turn-01.wav";
let services: VoiceServiceHandle | null = null;

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
    await stop.click({ timeoutMs: 5_000 });
    await expect(page.getByRole("button", { name: "Start Talk mode" })).toBeVisible({ timeout: 10_000 });
  }
});

test("test-injected WAV becomes contiguous PCM and one utterance range", async ({ page }) => {
  test.setTimeout(120_000);
  const encodedWav = (await readFile(inputWav)).toString("base64");
  const sentFrames: Array<{ payload: string; atMs: number }> = [];
  const receivedTypes: string[] = [];
  const avatarEvents: Array<{ type: string; seq: number; producerSeq: number; payload: Record<string, unknown> }> = [];
  const socketUrls: string[] = [];
  const browserErrors: string[] = [];
  const sessionSetupFailures: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("response", async (response) => {
    if (!response.url().endsWith("/api/voice/realtime/sessions") || response.status() < 400) return;
    try {
      const body = await response.json() as { detail?: unknown; error?: unknown };
      const detail = body.detail ?? body.error;
      sessionSetupFailures.push(typeof detail === "string" ? detail : `HTTP ${response.status()}`);
    } catch {
      sessionSetupFailures.push(`HTTP ${response.status()}`);
    }
  });
  await page.context().grantPermissions(["microphone"]);
  await page.addInitScript((wavBase64: string) => {
    (window as Window & { __voiceWavProbe?: Record<string, unknown> }).__voiceWavProbe = {};
    const mediaDevices = navigator.mediaDevices;
    mediaDevices.getUserMedia = async () => {
      const context = new AudioContext();
      const probe = (window as Window & { __voiceWavProbe?: Record<string, unknown> }).__voiceWavProbe!;
      probe.requestedAtMs = performance.now();
      await context.resume();
      probe.injectContextState = context.state;
      const NativeAudioWorkletNode = window.AudioWorkletNode;
      window.AudioWorkletNode = class extends NativeAudioWorkletNode {
        constructor(audioContext: BaseAudioContext, name: string, options?: AudioWorkletNodeOptions) {
          super(audioContext, name, options);
          probe.workletConstructed = Number(probe.workletConstructed ?? 0) + 1;
          probe.contextStateAtConstruction = audioContext.state;
          this.addEventListener("processorerror", () => { probe.appProcessorError = true; });
          const prior = this.port.onmessage;
          this.port.onmessage = (event) => {
            probe.appPortMessages = Number(probe.appPortMessages ?? 0) + 1;
            prior?.call(this.port, event);
          };
        }
      };
      const bytes = Uint8Array.from(atob(wavBase64), (character) => character.charCodeAt(0));
      const decoded = await context.decodeAudioData(bytes.buffer);
      await context.audioWorklet.addModule("/voice-realtime/capture-worklet.js");
      const destination = context.createMediaStreamDestination();
      const source = context.createBufferSource();
      const probeWorklet = new AudioWorkletNode(context, "hexis-voice-capture", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        channelCount: 1,
        channelCountMode: "explicit",
        channelInterpretation: "speakers",
      });
      probeWorklet.addEventListener("processorerror", () => { probe.probeProcessorError = true; });
      probeWorklet.port.onmessageerror = () => { probe.probeMessageError = true; };
      probeWorklet.port.onmessage = (event) => {
        const data = event.data as { samples?: Float32Array };
        probe.workletMessages = Number(probe.workletMessages ?? 0) + 1;
        if (data.samples instanceof Float32Array) {
          probe.workletMaxRms = Math.max(
            Number(probe.workletMaxRms ?? 0),
            Math.sqrt(data.samples.reduce((sum, sample) => sum + sample * sample, 0) / data.samples.length),
          );
        }
      };
      const probeWorkletMute = context.createGain();
      probeWorkletMute.gain.value = 0;
      probeWorklet.connect(probeWorkletMute);
      probeWorkletMute.connect(context.destination);
      const analyser = context.createAnalyser();
      const mute = context.createGain();
      mute.gain.value = 0;
      const createMediaStreamSource = AudioContext.prototype.createMediaStreamSource;
      AudioContext.prototype.createMediaStreamSource = function (stream: MediaStream) {
        const node = createMediaStreamSource.call(this, stream);
        probe.captureContextState = this.state;
        probe.captureSampleRate = this.sampleRate;
        const captureAnalyser = this.createAnalyser();
        const captureMute = this.createGain();
        captureMute.gain.value = 0;
        node.connect(captureAnalyser);
        captureAnalyser.connect(captureMute);
        captureMute.connect(this.destination);
        probe.captureTrackSettings = stream.getAudioTracks()[0]?.getSettings();
        const captureSignal = new Float32Array(captureAnalyser.fftSize);
        window.setInterval(() => {
          captureAnalyser.getFloatTimeDomainData(captureSignal);
          const rms = Math.sqrt(captureSignal.reduce((sum, sample) => sum + sample * sample, 0) / captureSignal.length);
          probe.captureInputMaxRms = Math.max(Number(probe.captureInputMaxRms ?? 0), rms);
        }, 50);
        return node;
      };
      source.buffer = decoded;
      probe.durationSeconds = decoded.duration;
      source.onended = () => { probe.sourceEndedAtMs = performance.now(); };
      source.connect(destination);
      source.connect(probeWorklet);
      source.connect(analyser);
      analyser.connect(mute);
      mute.connect(context.destination);
      const signal = new Float32Array(analyser.fftSize);
      const sampleTimer = window.setInterval(() => {
        analyser.getFloatTimeDomainData(signal);
        const rms = Math.sqrt(signal.reduce((sum, sample) => sum + sample * sample, 0) / signal.length);
        probe.injectedMaxRms = Math.max(Number(probe.injectedMaxRms ?? 0), rms);
      }, 50);
      source.addEventListener("ended", () => window.clearInterval(sampleTimer), { once: true });
      const originalSend = WebSocket.prototype.send;
      WebSocket.prototype.send = function (data: string | ArrayBufferLike | Blob | ArrayBufferView) {
        if (typeof data === "string" && data.includes('"type":"capture_open"')) {
          probe.captureOpenAtMs = performance.now();
          queueMicrotask(() => {
            probe.sourceStartedAtMs = performance.now();
            source.start();
          });
        }
        return originalSend.call(this, data);
      };
      return destination.stream;
    };
  }, encodedWav);
  page.on("websocket", (socket) => {
    socketUrls.push(socket.url());
    if (!socket.url().includes("/api/voice/realtime/")) return;
    socket.on("framesent", (frame) => {
      const payload = typeof frame.payload === "string" ? frame.payload : frame.payload.toString();
      sentFrames.push({ payload, atMs: Date.now() });
    });
    socket.on("framereceived", (frame) => {
      const payload = typeof frame.payload === "string" ? frame.payload : frame.payload.toString();
      try {
        const value = JSON.parse(payload) as {
          type?: unknown;
          kind?: unknown;
          event?: {
            event_type?: unknown;
            seq?: unknown;
            producer_seq?: unknown;
            payload?: unknown;
          };
        };
        if (typeof value.type === "string") {
          receivedTypes.push(value.type === "generation" && typeof value.kind === "string"
            ? `generation:${value.kind}`
            : value.type);
        }
        if (value.type === "event" && typeof value.event?.event_type === "string"
          && Number.isInteger(value.event.seq) && Number.isInteger(value.event.producer_seq)
          && value.event.payload && typeof value.event.payload === "object") {
          avatarEvents.push({
            type: value.event.event_type,
            seq: value.event.seq as number,
            producerSeq: value.event.producer_seq as number,
            payload: value.event.payload as Record<string, unknown>,
          });
        }
      } catch {
        receivedTypes.push("non-json");
      }
    });
  });

  await page.goto("/chat");
  await page.getByRole("button", { name: "Start Talk mode" }).click();
  await expect(page.getByRole("button", { name: "Stop Talk mode" })).toBeVisible();
  await page.waitForTimeout(15_000);
  const types = sentFrames.flatMap(({ payload }) => {
    try {
      const value = JSON.parse(payload) as { type?: unknown };
      return typeof value.type === "string" ? [value.type] : [];
    } catch {
      return [];
    }
  });
  const probe = await page.evaluate(() =>
    (window as Window & { __voiceWavProbe?: Record<string, unknown> }).__voiceWavProbe,
  );
  const bodyText = await page.locator("body").innerText();
  const serverDiagnosticLines = (services?.apiLogs.join("") ?? "")
    .split(/\r?\n/)
    .filter((line) => /Realtime input authorization failed|M4 input gate is not ready|M4 voice input gate completed|M4 voice response requested|Realtime output stream failed|HydrationUnavailable|VoiceInstructionError|QwenRealtimeProtocolError/.test(line))
    .map((line) => line.slice(0, 300));
  console.log(`M4 realtime frame types: ${JSON.stringify({ sent: types, received: receivedTypes, serverDiagnostics: serverDiagnosticLines })}`);
  console.log(`M4 avatar event envelope: ${JSON.stringify(avatarEvents.map(({ type, seq, producerSeq }) => ({ type, seq, producerSeq })))}`);
  await test.info().attach("voice-realtime-frame-types", {
    body: JSON.stringify({ sent: types, received: receivedTypes, avatarEvents }),
    contentType: "application/json",
  });
  expect(
    types.filter((type) => type === "utterance_end"),
    JSON.stringify({ types, socketUrls, probe, bodyText, browserErrors, sessionSetupFailures }),
  ).toHaveLength(1);

  const messages = sentFrames.map(({ payload }) => JSON.parse(payload) as Record<string, unknown>);
  expect(messages.some((message) => message.type === "capture_open")).toBe(true);
  const chunks = messages.filter((message) => message.type === "audio_chunk");
  const ends = messages.filter((message) => message.type === "utterance_end");
  expect(chunks.length).toBeGreaterThan(0);
  expect(ends).toHaveLength(1);
  const pcmParts: Buffer[] = [];
  let expectedFrame = Number(chunks[0].start_frame);
  for (const chunk of chunks) {
    const start = Number(chunk.start_frame);
    const end = Number(chunk.end_frame);
    const startMs = Number(chunk.start_monotonic_ms);
    const endMs = Number(chunk.end_monotonic_ms);
    const pcm = Buffer.from(String(chunk.pcm_base64), "base64");
    expect(start).toBe(expectedFrame);
    expect(end).toBeGreaterThan(start);
    expect(Number.isInteger(startMs)).toBe(true);
    expect(Number.isInteger(endMs)).toBe(true);
    expect(endMs).toBeGreaterThanOrEqual(startMs);
    expect(pcm).toHaveLength((end - start) * 2);
    pcmParts.push(pcm);
    expectedFrame = end;
  }
  const utteranceEnd = ends[0];
  expect(Number(utteranceEnd.start_frame)).toBe(Number(chunks[0].start_frame));
  expect(Number(utteranceEnd.end_frame)).toBe(expectedFrame);
  expect(utteranceEnd.audio_sha256).toBe(
    createHash("sha256").update(Buffer.concat(pcmParts)).digest("hex"),
  );
  expect(browserErrors).toEqual([]);
  const committedEvent = avatarEvents.find((event) => event.type === "voice_input_committed");
  expect(committedEvent).toBeDefined();
  const committedIdentity = committedEvent?.payload.identity as Record<string, unknown> | undefined;
  expect(committedIdentity).toMatchObject({
    session_id: expect.any(String),
    input_turn_id: expect.any(String),
    input_order: expect.any(Number),
    stimulus_revision: expect.any(Number),
  });
  expect(avatarEvents.some((event) => event.type === "voice_visual_window")).toBe(true);
  expect(avatarEvents.some((event) => event.type === "voice_model_call_started")).toBe(true);
  expect(avatarEvents.every((event, index, events) => index === 0 || event.seq > events[index - 1].seq)).toBe(true);
  expect(avatarEvents.every((event) => Number.isInteger(event.producerSeq))).toBe(true);
  await page.getByRole("button", { name: "Stop Talk mode" }).click();
  await expect(page.getByRole("button", { name: "Start Talk mode" })).toBeVisible();
});
