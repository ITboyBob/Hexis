import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { VoiceRealtimeClient } from "./client";

const descriptor = {
  schema_version: 2, transport: "webrtc", session_id: "chat", media_session_id: "media",
  control_url: "ws://127.0.0.1/control", offer_url: "/api/offer", close_url: "/api/close",
  timeout_ms: 30000, context_version: "a".repeat(64), model: "selected",
  session_config: { instructions: "上下文", modalities: ["text", "audio"], tools: [],
    input_audio_transcription: null, turn_detection: { type: "semantic_vad" } },
};

let sockets: FakeSocket[];
let peers: FakePeer[];
let blocked: Promise<void> | undefined;
let stop: ReturnType<typeof vi.fn>;
let track: MediaStreamTrack;
class FakeSocket {
  static OPEN = 1;
  readyState = 1; bufferedAmount = 0;
  onmessage?: (message: { data: string }) => void;
  constructor() { sockets.push(this); }
  send = vi.fn(); close = vi.fn();
  ready() { this.onmessage?.({ data: JSON.stringify({ schema_version: 2, session_id: "chat",
    media_session_id: "media", producer_seq: 0, type: "control_ready" }) }); }
}
class FakePeer {
  iceGatheringState = "complete";
  localDescription = { sdp: "v=0\r\n" };
  sender = { replaceTrack: vi.fn(() => blocked ?? Promise.resolve()) };
  constructor() { peers.push(this); }
  addTrack = vi.fn(() => this.sender);
  createDataChannel = vi.fn(() => ({ close: vi.fn() }));
  createOffer = vi.fn(async () => ({ type: "offer", sdp: "v=0\r\n" }));
  setLocalDescription = vi.fn(async () => undefined);
  close = vi.fn();
}

beforeEach(() => {
  sockets = []; peers = []; blocked = undefined; stop = vi.fn();
  track = { kind: "audio", stop } as unknown as MediaStreamTrack;
  vi.stubGlobal("isSecureContext", true);
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal("RTCPeerConnection", FakePeer);
  vi.stubGlobal("fetch", vi.fn(async (_url: string, options?: RequestInit) =>
    options?.method === "DELETE" ? { ok: true } : { ok: true, json: async () => descriptor }));
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: {
    getUserMedia: vi.fn(async () => ({ getTracks: () => [track], getAudioTracks: () => [track] })),
  } });
});
afterEach(() => { vi.unstubAllGlobals(); });

describe("RTC 建连期间关闭", () => {
  it("control_ready 与结束同时发生时，不再创建 PeerConnection", async () => {
    const client = new VoiceRealtimeClient();
    const opening = client.open("chat");
    await vi.waitFor(() => expect(sockets).toHaveLength(1));
    sockets[0].ready();
    await client.close();
    await opening.catch(() => undefined);
    expect(peers).toHaveLength(0);
    expect(stop).toHaveBeenCalled();
    expect(client.state).toBe("closed");
  });
  it("等待音轨门控时结束，恢复的旧任务不再生成 SDP", async () => {
    let release!: () => void;
    blocked = new Promise<void>((resolve) => { release = resolve; });
    const client = new VoiceRealtimeClient();
    const opening = client.open("chat");
    await vi.waitFor(() => expect(sockets).toHaveLength(1));
    sockets[0].ready();
    await vi.waitFor(() => expect(peers).toHaveLength(1));
    await client.close();
    release();
    await opening.catch(() => undefined);
    expect(peers[0].createOffer).not.toHaveBeenCalled();
    expect(peers[0].close).toHaveBeenCalled();
    expect(client.state).toBe("closed");
  });
});
