import { afterEach, describe, expect, it, vi } from "vitest";

import { VoiceRealtimeClient } from "./client";

class FakeTrack {
  stop = vi.fn();
}

class FakeStream {
  constructor(readonly tracks: FakeTrack[] = [new FakeTrack()]) {}
  getTracks() {
    return this.tracks;
  }
}

class FakeWebSocket {
  static readonly OPEN = 1;
  static instances: FakeWebSocket[] = [];
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  private listeners: Record<string, Array<() => void>> = {};

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
    queueMicrotask(() => this.open());
  }

  open() {
    this.readyState = 1;
    this.onopen?.();
  }

  send(data: string) {
    this.sent.push(data);
    const value = JSON.parse(data) as { type?: string; action?: string };
    if (value.type === "control" && value.action === "close") this.close();
  }

  addEventListener(type: string, listener: () => void) {
    this.listeners[type] = [...(this.listeners[type] || []), listener];
  }

  close() {
    this.readyState = 3;
    queueMicrotask(() => {
      this.listeners.close?.forEach((listener) => listener());
      this.onclose?.();
    });
  }
}

describe("VoiceRealtimeClient", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    FakeWebSocket.instances = [];
  });

  it("requests the microphone before creating a session and closes all resources", async () => {
    const track = new FakeTrack();
    const getUserMedia = vi.fn().mockResolvedValue(new FakeStream([track]));
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia },
      configurable: true,
    });
    const fetchMock = vi.fn(async (url: string | URL | Request) => {
      const value = String(url);
      if (value.endsWith("/api/chat/session")) {
        return new Response(JSON.stringify({ session_id: "session-1" }), {
          status: 200,
        });
      }
      return new Response(
        JSON.stringify({
          session_id: "session-1",
          media_session_id: "media-1",
          websocket_url: "ws://127.0.0.1:43817/api/voice/realtime/media-1",
        }),
        { status: 200 },
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const states: string[] = [];
    const client = new VoiceRealtimeClient({
      onStateChange: (state) => states.push(state),
    });

    await client.open(null);
    const socket = FakeWebSocket.instances[0];
    socket.open();

    expect(getUserMedia).toHaveBeenCalled();
    expect(fetchMock.mock.calls[0][0]).toContain("/api/chat/session");
    expect(fetchMock.mock.calls[1][0]).toContain("/api/voice/realtime/sessions");
    expect(client.state).toBe("ready");
    client.sendControl("barge_in", "speech_start");
    client.send({
      schema_version: 1,
      type: "control",
      session_id: "session-1",
      media_session_id: "media-1",
      producer_seq: 0,
      action: "close",
      reason: "manual",
    });
    expect(socket.sent).toHaveLength(2);
    expect(JSON.parse(socket.sent[0])).toMatchObject({
      type: "control",
      action: "barge_in",
      reason: "speech_start",
      session_id: "session-1",
      media_session_id: "media-1",
    });

    client.close();
    expect(track.stop).toHaveBeenCalled();
    expect(socket.readyState).toBe(3);
    expect(client.state).toBe("closed");
  });

  it("does not create a realtime session when microphone permission is denied", async () => {
    const getUserMedia = vi.fn().mockRejectedValue(new Error("denied"));
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia },
      configurable: true,
    });
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const client = new VoiceRealtimeClient();

    await expect(client.open("session-1")).rejects.toThrow("denied");

    expect(fetchMock).not.toHaveBeenCalled();
    expect(client.state).toBe("failed");
  });

  it("invalidates an old open when close happens first", async () => {
    let resolveMicrophone: (stream: FakeStream) => void = () => undefined;
    const getUserMedia = vi.fn().mockImplementation(
      () => new Promise<FakeStream>((resolve) => {
        resolveMicrophone = resolve;
      }),
    );
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia },
      configurable: true,
    });
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const client = new VoiceRealtimeClient();
    const opening = client.open("session-1");

    await vi.waitFor(() => expect(getUserMedia).toHaveBeenCalled());
    client.close();
    resolveMicrophone(new FakeStream());
    await opening;

    expect(fetchMock).not.toHaveBeenCalled();
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(client.state).toBe("closed");
  });

  it("stops capture and exposes an explicit retry after the realtime socket drops", async () => {
    const track = new FakeTrack();
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia: vi.fn().mockResolvedValue(new FakeStream([track])) },
      configurable: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(
        JSON.stringify({
          session_id: "session-1",
          media_session_id: "media-1",
          websocket_url: "ws://127.0.0.1:43817/api/voice/realtime/media-1",
        }),
        { status: 200 },
      )),
    );
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const messages: unknown[] = [];
    const client = new VoiceRealtimeClient({
      onMessage: (message) => messages.push(message),
    });

    await client.open("session-1");
    FakeWebSocket.instances[0].close();
    await vi.waitFor(() => expect(client.state).toBe("failed"));

    expect(track.stop).toHaveBeenCalledOnce();
    expect(messages).toContainEqual(expect.objectContaining({
      type: "error",
      stage: "client",
      code: "connection_failed",
      recoverable: true,
    }));
  });
});
