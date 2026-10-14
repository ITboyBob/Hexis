import { test, expect, type Page } from "@playwright/test";
import { createServer, type Server } from "node:http";
import { spawn, type ChildProcess } from "node:child_process";
import path from "node:path";
import fs from "node:fs";
import os from "node:os";

const uiPort = Number(process.env.HEXIS_UI_PORT || 3477);
const fixturePort = Number(process.env.HEXIS_RTC_FIXTURE_PORT || uiPort + 1);
const fixtureUrl = `http://127.0.0.1:${fixturePort}`;
const sourceRoot = path.resolve(__dirname, "../..");
const artifacts = path.join(process.env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache"), "hexis", "voice-rtc", "browser");
let gateway: Server;
let backend: ChildProcess;
let activePage: Page | null = null;

// 测试对端使用浏览器原生 RTC，不替换 createOffer/ICE/音轨/DataChannel。
async function answerInBrowser(page: Page, offer: string): Promise<string> {
  return page.evaluate(async (sdp) => {
    const w = window as any;
    const peer = new RTCPeerConnection({ iceServers: [] });
    const context = new AudioContext();
    const tone = context.createOscillator();
    const destination = context.createMediaStreamDestination();
    tone.connect(destination); tone.start(); await context.resume();
    let control: RTCDataChannel;
    let config: Record<string, unknown> | null = null;
    const commands: Record<string, unknown>[] = [];
    let created = false;
    const send = (event: Record<string, unknown>) => control.send(JSON.stringify({ event_id: crypto.randomUUID(), ...event }));
    const release = () => {
      if (!config) throw new Error("测试配置尚未到达");
      send({ type: "session.updated", session: { ...config, id: "controlled-cloud" } });
    };
    const wire = (channel: RTCDataChannel) => {
      channel.onopen = () => {
        if (channel.label === "txt" && !created) {
          created = true; control = channel;
          send({ type: "session.created", session: { id: "controlled-cloud", model: "controlled-fixture-model" } });
        }
      };
      channel.onmessage = (event) => {
        const message = JSON.parse(String(event.data)); commands.push(message);
        if (message.type === "session.update") {
          config = message.session;
          if (!w.__rtcHoldConfig) release();
        } else if (message.type === "response.create") {
          send({ type: "response.created", response: { id: "tool-followup" } });
          send({ type: "response.audio_transcript.delta", response_id: "tool-followup", item_id: "followup-text", delta: "受控工具续答" });
          send({ type: "response.done", response: { id: "tool-followup", status: "completed", output: [
            { id: "followup-text", type: "message", content: [{ type: "audio", transcript: "受控工具续答" }] },
          ] } });
        }
      };
    };
    peer.ondatachannel = (event) => wire(event.channel);
    control = peer.createDataChannel("txt", { ordered: true }); wire(control);
    await peer.setRemoteDescription({ type: "offer", sdp });
    destination.stream.getAudioTracks().forEach((track) => peer.addTrack(track, destination.stream));
    await peer.setLocalDescription(await peer.createAnswer());
    if (peer.iceGatheringState !== "complete") await new Promise<void>((resolve) => {
      peer.onicegatheringstatechange = () => { if (peer.iceGatheringState === "complete") resolve(); };
    });
    w.__rtcProvider = { peer, context, tone, commands, release, send, get config() { return config; } };
    return peer.localDescription!.sdp;
  }, offer);
}

test.beforeAll(async () => {
  fs.mkdirSync(artifacts, { recursive: true });
  gateway = createServer(async (request, response) => {
    try {
      const chunks: Buffer[] = [];
      for await (const chunk of request) chunks.push(Buffer.from(chunk));
      if (!activePage) throw new Error("本地 RTC 测试页面尚未准备");
      const answer = await answerInBrowser(activePage, Buffer.concat(chunks).toString("utf8"));
      response.writeHead(200, { "Content-Type": "application/sdp" }); response.end(answer);
    } catch (error) { response.writeHead(500); response.end(String(error)); }
  });
  await new Promise<void>((resolve) => gateway.listen(0, "127.0.0.1", resolve));
  const address = gateway.address();
  if (!address || typeof address === "string") throw new Error("测试 gateway 地址无效");
  const log = fs.openSync(path.join(artifacts, "fixture.log"), "w");
  backend = spawn(path.join(sourceRoot, ".venv/bin/python"), ["-m", "uvicorn", "tests.voice_realtime.rtc_browser_fixture:app", "--host", "127.0.0.1", "--port", String(fixturePort)], {
    cwd: sourceRoot, stdio: ["ignore", log, log], env: { ...process.env,
      HEXIS_RTC_GATEWAY_PORT: String(address.port), HEXIS_RTC_FIXTURE_PORT: String(fixturePort),
      HEXIS_UI_ORIGIN: `http://127.0.0.1:${uiPort}` },
  });
  await expect.poll(async () => {
    try { return (await fetch(`${fixtureUrl}/health`)).ok; } catch { return false; }
  }, { timeout: 30000 }).toBe(true);
});

test.afterAll(async () => {
  backend?.kill("SIGTERM");
  await new Promise<void>((resolve) => gateway?.close(() => resolve()));
});

test.beforeEach(async ({ page }) => {
  activePage = page;
  await page.addInitScript(() => {
    sessionStorage.setItem("hexis-chat-session-id", "11111111-1111-4111-8111-111111111111");
    const w = window as any;
    const NativePeer = window.RTCPeerConnection;
    w.__rtcPeers = [];
    window.RTCPeerConnection = class extends NativePeer {
      constructor(configuration?: RTCConfiguration) { super(configuration); w.__rtcPeers.push(this); }
    };
  });
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith("/api/voice/realtime/")) { await route.continue(); return; }
    if (url.pathname === "/api/status") { await route.fulfill({ json: { configured: true, agent_name: "RTC Fixture", mood: "Ready", valence: 0 } }); return; }
    if (url.pathname.startsWith("/api/chat/session/")) {
      const facts = await (await fetch(`${fixtureUrl}/diagnostics`)).json();
      await route.fulfill({ json: { messages: [...facts.transcripts.map((input: any) => ({
        message_id: input.message_id, role: "user", content: input.text,
        metadata: { channel: "voice", input_turn_id: input.input_turn_id, agent_turn_id: input.turn_id },
      })), ...facts.outputs.map((output: any) => ({
        message_id: output.response_id, role: "assistant", content: output.text,
        metadata: { channel: "voice", transport: "webrtc", agent_turn_id: output.turn_id,
          response_id: output.response_id, playback_scope: "unknown", content_source: "model_generation" },
      }))] } }); return;
    }
    await route.fulfill({ json: { messages: [], sessions: [], unread: 0, pending_requests: [], artifacts: [], tasks: [] } });
  });
  await page.goto("/chat");
  await expect(page.getByRole("button", { name: "Start Talk mode" })).toBeVisible();
});

async function stats(page: Page) {
  return page.evaluate(async () => {
    const w = window as any;
    const client: RTCPeerConnection = w.__rtcPeers[0];
    const provider: RTCPeerConnection = w.__rtcProvider.peer;
    const result = { connection: client.connectionState, outbound: 0, inbound: 0, providerInbound: 0,
      sendersDetached: client.getSenders().filter((sender) => sender.track?.kind === "audio").length === 0 };
    (await client.getStats()).forEach((entry) => {
      if (entry.type === "outbound-rtp" && entry.kind === "audio") result.outbound += entry.bytesSent || 0;
      if (entry.type === "inbound-rtp" && entry.kind === "audio") result.inbound += entry.bytesReceived || 0;
    });
    (await provider.getStats()).forEach((entry) => {
      if (entry.type === "inbound-rtp" && entry.kind === "audio") result.providerInbound += entry.bytesReceived || 0;
    });
    return result;
  });
}

test("原生 RTC 双向媒体、配置门控、元数据与工具控制回传", async ({ page }, info) => {
  const baseline = await (await fetch(`${fixtureUrl}/diagnostics`)).json();
  const requests: string[] = [];
  page.on("request", (request) => { if (request.url().includes("/api/voice/")) requests.push(request.url()); });
  await page.evaluate(() => { (window as any).__rtcHoldConfig = true; });
  await page.getByRole("button", { name: "Start Talk mode" }).click();
  await expect.poll(() => page.evaluate(() => !!(window as any).__rtcProvider?.config)).toBe(true);
  await expect.poll(async () => (await stats(page)).connection).toBe("connected");
  const before = await stats(page);
  expect(before.sendersDetached).toBe(true);
  expect(before.providerInbound).toBe(0);
  await page.evaluate(() => (window as any).__rtcProvider.release());
  await expect(page.getByText("Talk connected", { exact: true })).toBeVisible();
  await expect.poll(async () => (await stats(page)).outbound).toBeGreaterThan(0);
  await expect.poll(async () => (await stats(page)).inbound).toBeGreaterThan(0);
  await expect.poll(async () => (await stats(page)).providerInbound).toBeGreaterThan(0);
  const config = await page.evaluate(() => (window as any).__rtcProvider.config);
  expect(config.input_audio_transcription).toEqual({ model: "qwen3-asr-flash-realtime" });
  expect(config.turn_detection).toEqual({ type: "semantic_vad" });
  await expect(page.getByText("Speaking", { exact: true })).not.toBeVisible();
  await expect(page.getByText("Listening", { exact: true })).not.toBeVisible();
  await page.evaluate(() => {
    const p = (window as any).__rtcProvider;
    p.send({ type: "input_audio_buffer.committed", item_id: "controlled-input" });
    p.send({ type: "conversation.item.input_audio_transcription.completed", item_id: "controlled-input", transcript: "受控用户输入" });
    p.send({ type: "response.created", response: { id: "controlled-response" } });
    p.send({ type: "response.audio_transcript.delta", response_id: "controlled-response", item_id: "text", delta: "受控助手回复" });
    p.send({ type: "response.done", response: { id: "controlled-response", status: "completed",
      output: [{ type: "message", id: "text", content: [{ type: "audio", transcript: "受控助手回复" }] }] } });
  });
  await expect(page.locator("p").filter({ hasText: /^受控助手回复$/ })).toBeVisible();
  await expect(page.locator("p").filter({ hasText: /^受控用户输入$/ })).toBeVisible();
  await page.evaluate(() => {
    const p = (window as any).__rtcProvider;
    p.send({ type: "input_audio_buffer.speech_started", item_id: "tool-input" });
    p.send({ type: "input_audio_buffer.speech_stopped", item_id: "tool-input" });
    p.send({ type: "input_audio_buffer.committed", item_id: "tool-input" });
    p.send({ type: "response.created", response: { id: "tool-response" } });
    p.send({ type: "response.function_call_arguments.done", response_id: "tool-response", call_id: "controlled-call", name: "voice_recall", arguments: '{"query":"受控问题"}' });
    p.send({ type: "response.done", response: { id: "tool-response", status: "completed", output: [] } });
  });
  await expect(page.locator("p").filter({ hasText: /^受控工具续答$/ })).toBeVisible();
  const commands = await page.evaluate(() => (window as any).__rtcProvider.commands);
  expect(commands.filter((message: any) => message.type === "session.update")).toHaveLength(1);
  expect(commands.filter((message: any) => message.type === "response.create")).toHaveLength(1);
  expect(commands.filter((message: any) => message.type === "conversation.item.create")).toHaveLength(1);
  expect(commands.some((message: any) => message.type === "response.cancel" || message.type === "input_audio_buffer.append")).toBe(false);
  const after = await stats(page);
  await page.screenshot({ path: path.join(artifacts, "talk-connected.png"), fullPage: true });
  await info.attach("native-rtc-stats", { body: JSON.stringify({ before, after }), contentType: "application/json" });
  const facts = await (await fetch(`${fixtureUrl}/diagnostics`)).json();
  expect(facts.transcripts.length - baseline.transcripts.length).toBe(1);
  expect(facts.emotions.length - baseline.emotions.length).toBe(1);
  expect(facts.bootstraps - baseline.bootstraps).toBe(1);
  await info.attach("host-diagnostics", { body: JSON.stringify(facts), contentType: "application/json" });
  expect(facts.tool_calls - baseline.tool_calls).toBe(1);
  expect(facts.real_provider_requests).toBe(0);
  expect(facts.inputs.at(-1)).not.toHaveProperty("user_text");
  expect(facts.outputs.at(-1).playback_scope).toBe("unknown");
  expect(requests.some((url) => url.includes("/offer"))).toBe(true);
  await page.getByRole("button", { name: "Stop Talk mode" }).click();
  await expect(page.getByRole("button", { name: "Start Talk mode" })).toBeVisible();
  await expect.poll(async () => (await (await fetch(`${fixtureUrl}/diagnostics`)).json()).active_sessions).toBe(0);
  expect(await page.evaluate(() => (window as any).__rtcPeers[0].connectionState)).toBe("closed");
});

test("拒绝麦克风权限时不创建 RTC 会话", async ({ page, context }) => {
  const baseline = await (await fetch(`${fixtureUrl}/diagnostics`)).json();
  const cdp = await context.newCDPSession(page);
  const target = await cdp.send("Target.getTargetInfo");
  await cdp.send("Browser.setPermission", { permission: { name: "microphone" }, setting: "denied",
    origin: `http://127.0.0.1:${uiPort}`, browserContextId: target.targetInfo.browserContextId });
  await page.getByRole("button", { name: "Start Talk mode" }).click();
  await expect(page.getByRole("alert").filter({ has: page.getByRole("button", { name: "Retry Talk" }) })).toBeVisible();
  expect((await (await fetch(`${fixtureUrl}/diagnostics`)).json()).bootstraps).toBe(baseline.bootstraps);
  expect(await page.evaluate(() => (window as any).__rtcPeers.length)).toBe(0);
});

test("DataChannel 断线释放客户端和宿主会话，不自动重连", async ({ page }) => {
  await page.getByRole("button", { name: "Start Talk mode" }).click();
  await expect(page.getByText("Talk connected", { exact: true })).toBeVisible();
  const baseline = await (await fetch(`${fixtureUrl}/diagnostics`)).json();
  await page.evaluate(() => (window as any).__rtcProvider.peer.close());
  await expect(page.getByRole("alert").filter({ has: page.getByRole("button", { name: "Retry Talk" }) })).toBeVisible();
  await expect.poll(async () => (await (await fetch(`${fixtureUrl}/diagnostics`)).json()).active_sessions).toBe(0);
  expect((await (await fetch(`${fixtureUrl}/diagnostics`)).json()).bootstraps).toBe(baseline.bootstraps);
});
