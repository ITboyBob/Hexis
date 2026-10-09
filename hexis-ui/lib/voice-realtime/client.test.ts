import { afterEach, describe, expect, it, vi } from "vitest";

import { VoiceRealtimeClient } from "./client";
import {
  parseRealtimeServerMessage,
  parseRealtimeSessionResponse,
  type BargeInCapability,
  type RealtimeServerMessage,
} from "./contracts";

const testCapability: BargeInCapability = {
  supported: true,
  configuration_id: "voice-barge-in-playback-initial-v1",
  maximum_receipt_prefix_items: 256,
};

function testSessionResponse(
  media_session_id = "media-1",
  barge_in_capability: BargeInCapability = testCapability,
) {
  return {
    session_id: "session-1",
    media_session_id,
    websocket_url: `ws://127.0.0.1:43817/api/voice/realtime/${media_session_id}`,
    barge_in_capability,
  };
}

function testReadyMessage(
  media_session_id: string,
  barge_in_capability: BargeInCapability = testCapability,
): RealtimeServerMessage {
  return {
    schema_version: 1,
    type: "ready",
    session_id: "session-1",
    media_session_id,
    producer_seq: 0,
    cloud_session_id: "qwen-session",
    instructions_hash: "a".repeat(64),
    barge_in_capability,
  };
}

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
  onmessage: ((event: { data: string }) => void) | null = null;
  private listeners: Record<string, Array<() => void>> = {};
  static nextReadyMessage: unknown | null = null;
  static suppressNextReady = false;
  static nextReadyBeforeOpen = false;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
    queueMicrotask(() => {
      const mediaSessionId = new URL(this.url).pathname.split("/").at(-1) ?? "media-1";
      const ready = () => this.receive(FakeWebSocket.nextReadyMessage ?? testReadyMessage(mediaSessionId));
      if (FakeWebSocket.nextReadyBeforeOpen && !FakeWebSocket.suppressNextReady) ready();
      this.open();
      if (!FakeWebSocket.nextReadyBeforeOpen && !FakeWebSocket.suppressNextReady) ready();
      FakeWebSocket.nextReadyMessage = null;
      FakeWebSocket.suppressNextReady = false;
      FakeWebSocket.nextReadyBeforeOpen = false;
    });
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

  receive(value: unknown) {
    this.onmessage?.({ data: JSON.stringify(value) });
  }
}

describe("parseRealtimeServerMessage", () => {
  const envelope = {
    schema_version: 1 as const,
    session_id: "session-1",
    media_session_id: "media-1",
    producer_seq: 1,
  };

  it("accepts actual media, capture, and M5 selection shapes", () => {
    const audio: RealtimeServerMessage = {
      ...envelope,
      type: "audio_chunk",
      response_id: "response-1",
      playback_id: "playback-1",
      turn_id: null,
      output_epoch: 1,
      segment_id: "segment-1",
      segment_index: 0,
      chunk_seq: 1,
      pcm_base64: "AAAA",
    };
    const suspended: RealtimeServerMessage = {
      ...envelope,
      type: "capture_suspended",
      capture_instance_id: "capture-1",
      control_id: "control-1",
      discarded_range: { start_frame: 16, end_frame: 32 },
    };
    const resumed: RealtimeServerMessage = {
      ...envelope,
      type: "capture_resumed",
      capture_instance_id: "capture-1",
      control_id: "control-2",
    };
    const selection: RealtimeServerMessage = {
      ...envelope,
      type: "m5_selection_opened",
      selection: {
        selection_id: "selection-1",
        version: 2,
        message: "请选择下一步",
        state: "open",
        origin_session_id: "session-1",
      },
    };

    for (const message of [audio, suspended, resumed, selection]) {
      expect(parseRealtimeServerMessage(message)).toEqual(message);
    }
  });

  it("parses one strict REST session capability snapshot", () => {
    const response = testSessionResponse();
    const missingCapability = { ...response } as Record<string, unknown>;
    delete missingCapability.barge_in_capability;
    expect(parseRealtimeSessionResponse(response)).toEqual(response);
    expect(() => parseRealtimeSessionResponse({ ...response, unexpected: true })).toThrow(/unsupported/);
    expect(() => parseRealtimeSessionResponse(missingCapability)).toThrow(/missing barge_in_capability/);
    expect(() => parseRealtimeSessionResponse({ ...response, barge_in_capability: { ...testCapability, maximum_receipt_prefix_items: 0 } })).toThrow(/maximum_receipt_prefix_items/);
  });

  it("requires production generation variants and rejects missing or extra fields", () => {
    const common = {
      ...envelope,
      type: "generation" as const,
      response_id: "response-1",
      playback_id: "playback-1",
      turn_id: null,
      output_epoch: 1,
    };
    const delta: RealtimeServerMessage = {
      ...common,
      kind: "text_delta",
      text: null,
    };
    const done: RealtimeServerMessage = {
      ...common,
      kind: "done",
      reason: null,
    };

    expect(parseRealtimeServerMessage(delta)).toEqual(delta);
    expect(parseRealtimeServerMessage(done)).toEqual(done);
    expect(() => parseRealtimeServerMessage({ ...common, kind: "text_delta" })).toThrow(/missing text/);
    expect(() => parseRealtimeServerMessage({ ...common, kind: "done" })).toThrow(/missing reason/);
    expect(() => parseRealtimeServerMessage({ ...done, text: "unexpected" })).toThrow(/unsupported text/);
    expect(() => parseRealtimeServerMessage({ ...audioWithExtra(envelope), unsupported: true })).toThrow(/unsupported/);
  });

  it("parses exact confirmed barge-in results and rejects unknown statuses or fields", () => {
    const result = {
      schema_version: 1,
      type: "barge_in_result",
      session_id: "session-1",
      media_session_id: "media-1",
      producer_seq: 1,
      command_id: "command-1",
      capture_instance_id: "capture-1",
      response_id: "response-1",
      output_epoch: 3,
      playback_id: "playback-1",
      status: "playback_only",
    };
    expect(parseRealtimeServerMessage(result)).toEqual(result);
    expect(() => parseRealtimeServerMessage({ ...result, status: "done" })).toThrow(/status/i);
    expect(() => parseRealtimeServerMessage({ ...result, cancellation_complete: true })).toThrow(/unsupported/);
  });

  it("parses strict memory recall messages and rejects inconsistent previews", () => {
    const recall = {
      ...envelope,
      type: "memory_recall",
      input_turn_id: "input-1",
      stimulus_revision: 2,
      count: 1,
      memories: [{
        id: "11111111-1111-4111-8111-111111111111",
        type: "semantic",
        content: "用户偏好中文",
        similarity: 0.91,
        source: "vector",
      }],
      hydrate_ms: 12.5,
      recall_performed: true,
    };

    expect(parseRealtimeServerMessage(recall)).toEqual(recall);
    expect(() => parseRealtimeServerMessage({ ...recall, count: 2 })).toThrow(/count/i);
    expect(() => parseRealtimeServerMessage({ ...recall, stimulus_revision: -1 })).toThrow(/stimulus_revision/i);
    expect(() => parseRealtimeServerMessage({ ...recall, memories: [{ content: "missing identity" }] })).toThrow(/missing id/i);
    expect(() => parseRealtimeServerMessage({ ...recall, recall_performed: false })).toThrow(/recall_performed/i);
    expect(() => parseRealtimeServerMessage({ ...recall, memories: [{ ...recall.memories[0], content: "长".repeat(261) }] })).toThrow(/content/i);
    expect(() => parseRealtimeServerMessage({ ...recall, memories: [{ ...recall.memories[0], id: "invalid" }] })).toThrow(/id/i);
    expect(() => parseRealtimeServerMessage({ ...recall, hydrate_ms: Number.NaN })).toThrow(/hydrate_ms/i);
    expect(() => parseRealtimeServerMessage({ ...recall, count: -1 })).toThrow(/count/i);
    expect(() => parseRealtimeServerMessage({ ...recall, memories: [{ ...recall.memories[0], similarity: "0.91" }] })).toThrow(/similarity/i);
    expect(parseRealtimeServerMessage({ ...recall, count: 0, memories: [] })).toEqual({ ...recall, count: 0, memories: [] });
    expect(parseRealtimeServerMessage({ ...recall, count: 0, memories: [], recall_performed: false })).toEqual({ ...recall, count: 0, memories: [], recall_performed: false });
  });
});

function audioWithExtra(envelope: { schema_version: number; session_id: string; media_session_id: string; producer_seq: number }) {
  return {
    ...envelope,
    type: "audio_chunk",
    response_id: "response-1",
    playback_id: "playback-1",
    turn_id: null,
    output_epoch: 1,
    segment_id: "segment-1",
    segment_index: 0,
    chunk_seq: 1,
    pcm_base64: "AAAA",
  };
}

describe("VoiceRealtimeClient", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    FakeWebSocket.instances = [];
    FakeWebSocket.nextReadyMessage = null;
    FakeWebSocket.suppressNextReady = false;
    FakeWebSocket.nextReadyBeforeOpen = false;
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
      return new Response(JSON.stringify(testSessionResponse()), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const states: string[] = [];
    const receivedMessages: RealtimeServerMessage[] = [];
    const client = new VoiceRealtimeClient({
      onStateChange: (state) => states.push(state),
      onMessage: (message) => receivedMessages.push(message),
    });

    await client.open(null);
    const socket = FakeWebSocket.instances[0];
    socket.open();

    expect(getUserMedia).toHaveBeenCalled();
    expect(fetchMock.mock.calls[0][0]).toContain("/api/chat/session");
    expect(fetchMock.mock.calls[1][0]).toContain("/api/voice/realtime/sessions");
    expect(client.state).toBe("ready");
    const audioMessage: RealtimeServerMessage = {
      schema_version: 1,
      type: "audio_chunk",
      session_id: "session-1",
      media_session_id: "media-1",
      producer_seq: 1,
      response_id: "response-1",
      playback_id: "playback-1",
      turn_id: null,
      output_epoch: 1,
      segment_id: "segment-1",
      segment_index: 0,
      chunk_seq: 1,
      pcm_base64: "AAAA",
    };
    socket.receive(audioMessage);
    expect(receivedMessages).toEqual([audioMessage]);
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

  it("accepts a matching ready message that arrives before the WebSocket open event", async () => {
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia: vi.fn().mockResolvedValue(new FakeStream()) },
      configurable: true,
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(testSessionResponse()), { status: 200 })));
    vi.stubGlobal("WebSocket", FakeWebSocket);
    FakeWebSocket.nextReadyBeforeOpen = true;
    const client = new VoiceRealtimeClient();

    await client.open("session-1");

    expect(client.state).toBe("ready");
    await client.close();
  });

  it("cancels the pending ready handshake when Talk closes", async () => {
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia: vi.fn().mockResolvedValue(new FakeStream()) },
      configurable: true,
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(testSessionResponse()), { status: 200 })));
    vi.stubGlobal("WebSocket", FakeWebSocket);
    FakeWebSocket.suppressNextReady = true;
    const client = new VoiceRealtimeClient();
    const opening = client.open("session-1");

    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    await client.close();
    await expect(opening).resolves.toBeUndefined();
    expect(client.state).toBe("closed");
    expect(FakeWebSocket.instances[0].readyState).toBe(3);
  });

  it("fails closed when a server message omits a production field", async () => {
    const track = new FakeTrack();
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia: vi.fn().mockResolvedValue(new FakeStream([track])) },
      configurable: true,
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(testSessionResponse()), { status: 200 })));
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const messages: RealtimeServerMessage[] = [];
    const client = new VoiceRealtimeClient({ onMessage: (message) => messages.push(message) });

    await client.open("session-1");
    FakeWebSocket.instances[0].receive({
      schema_version: 1,
      type: "audio_chunk",
      session_id: "session-1",
      media_session_id: "media-1",
      producer_seq: 1,
      response_id: "response-1",
      playback_id: "playback-1",
      output_epoch: 1,
      segment_id: "segment-1",
      segment_index: 0,
      chunk_seq: 1,
      pcm_base64: "AAAA",
    });

    expect(client.state).toBe("failed");
    expect(track.stop).toHaveBeenCalledOnce();
    expect(FakeWebSocket.instances[0].readyState).toBe(3);
    expect(messages.at(-1)).toMatchObject({
      type: "error",
      stage: "client",
      message: "The voice service message is missing turn_id.",
      recoverable: true,
    });
  });

  it("rejects and closes when WS ready disagrees with the REST capability snapshot", async () => {
    const track = new FakeTrack();
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia: vi.fn().mockResolvedValue(new FakeStream([track])) },
      configurable: true,
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(testSessionResponse()), { status: 200 })));
    vi.stubGlobal("WebSocket", FakeWebSocket);
    FakeWebSocket.nextReadyMessage = testReadyMessage("media-1", {
      ...testCapability,
      maximum_receipt_prefix_items: 255,
    });
    const client = new VoiceRealtimeClient();

    await expect(client.open("session-1")).rejects.toThrow(/capability.*match/i);
    expect(track.stop).toHaveBeenCalledOnce();
    expect(FakeWebSocket.instances[0].readyState).toBe(3);
    expect(client.state).toBe("failed");
  });

  it("rejects a matched but unsupported capability instead of opening Talk", async () => {
    const track = new FakeTrack();
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia: vi.fn().mockResolvedValue(new FakeStream([track])) },
      configurable: true,
    });
    const unsupported = { ...testCapability, supported: false };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(testSessionResponse("media-1", unsupported)), { status: 200 })));
    vi.stubGlobal("WebSocket", FakeWebSocket);
    FakeWebSocket.nextReadyMessage = testReadyMessage("media-1", unsupported);
    const client = new VoiceRealtimeClient();

    await expect(client.open("session-1")).rejects.toThrow(/does not support confirmed voice barge-in/i);
    expect(track.stop).toHaveBeenCalledOnce();
    expect(client.state).toBe("failed");
  });

  it.each([
    [{ detail: "M5 realtime tools are unavailable because the task worker is not ready." }, "M5 realtime tools are unavailable because the task worker is not ready."],
    [{ error: "Selected error", detail: "Other detail" }, "Selected error"],
    [{ detail: { message: "Not a user-facing string" } }, "Could not start the realtime voice service."],
    [null, "Could not start the realtime voice service."],
  ])("preserves the session startup error and releases capture: %j", async (payload, message) => {
    const track = new FakeTrack();
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia: vi.fn().mockResolvedValue(new FakeStream([track])) },
      configurable: true,
    });
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(payload), { status: 503 }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const client = new VoiceRealtimeClient({ toolSurface: "m5" });

    await expect(client.open("session-1")).rejects.toThrow(message);
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(track.stop).toHaveBeenCalledOnce();
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(client.state).toBe("failed");
  });

  it("selects M5 tools and waits for matching suspend/resume acknowledgements", async () => {
    const track = new FakeTrack();
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia: vi.fn().mockResolvedValue(new FakeStream([track])) },
      configurable: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify(testSessionResponse()), { status: 200 }))
    );
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const client = new VoiceRealtimeClient({ toolSurface: "m5" });

    await client.open("session-1");
    const socket = FakeWebSocket.instances[0];
    const fetchBody = JSON.parse(String((vi.mocked(fetch).mock.calls[0]?.[1] as RequestInit)?.body));
    expect(fetchBody.tool_surface).toBe("m5");

    const suspending = client.suspendCapture("capture-1");
    const suspendMessage = JSON.parse(socket.sent.at(-1) || "{}");
    expect(suspendMessage).toMatchObject({
      type: "capture_suspend",
      session_id: "session-1",
      media_session_id: "media-1",
      capture_instance_id: "capture-1",
    });
    socket.receive({
      schema_version: 1,
      type: "capture_suspended",
      session_id: "session-1",
      media_session_id: "media-1",
      producer_seq: 1,
      capture_instance_id: "capture-1",
      control_id: suspendMessage.control_id,
      discarded_range: null,
    });
    await expect(suspending).resolves.toBeUndefined();

    const resuming = client.resumeCapture("capture-1");
    const resumeMessage = JSON.parse(socket.sent.at(-1) || "{}");
    socket.receive({
      schema_version: 1,
      type: "capture_resumed",
      session_id: "session-1",
      media_session_id: "media-1",
      producer_seq: 2,
      capture_instance_id: "capture-1",
      control_id: resumeMessage.control_id,
    });
    await expect(resuming).resolves.toBeUndefined();
    expect(track.stop).not.toHaveBeenCalled();
    await client.close();
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
      vi.fn(async () => new Response(JSON.stringify(testSessionResponse()), { status: 200 })),
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
