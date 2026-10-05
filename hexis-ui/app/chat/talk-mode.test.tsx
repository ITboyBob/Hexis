import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TalkMode } from "./talk-mode";

const captureMocks = vi.hoisted(() => ({
  start: vi.fn().mockResolvedValue(undefined),
  close: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("@/lib/voice-realtime/capture", () => ({
  AudioCaptureController: vi.fn().mockImplementation(() => captureMocks),
}));

class FakeTrack {
  stop = vi.fn();
}

class FakeStream {
  constructor(readonly tracks: FakeTrack[] = [new FakeTrack()]) {}
  getTracks() {
    return this.tracks;
  }
  getAudioTracks() {
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
    this.listeners.close?.forEach((listener) => listener());
    this.onclose?.();
  }
}

describe("TalkMode", () => {
  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    Object.defineProperty(window, "isSecureContext", {
      value: true,
      configurable: true,
    });
  });

  afterEach(() => {
    captureMocks.start.mockClear();
    captureMocks.close.mockClear();
    vi.unstubAllGlobals();
    FakeWebSocket.instances = [];
    Object.defineProperty(window, "isSecureContext", {
      value: true,
      configurable: true,
    });
  });

  it("requests the microphone immediately and reaches realtime listening", async () => {
    const order: string[] = [];
    const track = new FakeTrack();
    const getUserMedia = vi.fn().mockImplementation(async () => {
      order.push("getUserMedia");
      return new FakeStream([track]);
    });
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia },
      configurable: true,
    });
    const fetchMock = vi.fn(async (url: string | URL | Request) => {
      const value = String(url);
      order.push(value);
      if (
        value.includes("/api/voice/transcribe") ||
        value.includes("/api/voice/synthesize")
      ) {
        throw new Error("legacy voice path must not be called");
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
    render(<TalkMode sessionId="session-1" />);

    fireEvent.click(screen.getByRole("button", { name: "Start Talk mode" }));

    await waitFor(() => expect(screen.getByText("Listening")).toBeInTheDocument());
    expect(order[0]).toBe("getUserMedia");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/voice/realtime/sessions",
      expect.objectContaining({ method: "POST" }),
    );

    fireEvent.click(screen.getByRole("button", { name: "Stop Talk mode" }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Start Talk mode" }),
      ).toBeInTheDocument(),
    );
    expect(track.stop).toHaveBeenCalled();
    expect(FakeWebSocket.instances[0].readyState).toBe(3);
  });

  it("does not create a realtime session when microphone permission is denied", async () => {
    const getUserMedia = vi.fn().mockRejectedValue(new Error("Permission denied"));
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia },
      configurable: true,
    });
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<TalkMode sessionId="session-1" />);

    fireEvent.click(screen.getByRole("button", { name: "Start Talk mode" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Permission denied");
    expect(screen.queryByText("Listening")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("closes the failed media session before retrying a recoverable input error", async () => {
    const getUserMedia = vi.fn().mockResolvedValue(new FakeStream());
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia },
      configurable: true,
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({
        session_id: "session-1",
        media_session_id: `media-${FakeWebSocket.instances.length + 1}`,
        websocket_url: "ws://127.0.0.1:43817/api/voice/realtime/media",
      }),
      { status: 200 },
    )));
    vi.stubGlobal("WebSocket", FakeWebSocket);
    render(<TalkMode sessionId="session-1" />);

    fireEvent.click(screen.getByRole("button", { name: "Start Talk mode" }));
    await screen.findByText("Listening");
    act(() => {
      FakeWebSocket.instances[0].onmessage?.({
        data: JSON.stringify({
          schema_version: 1,
          type: "error",
          session_id: "session-1",
          media_session_id: "media-1",
          producer_seq: 1,
          stage: "input",
          code: "input_or_context_failed",
          message: "Voice input could not be prepared. Stop Talk and retry.",
          recoverable: true,
        }),
      });
    });

    const retryButton = await screen.findByRole("button", { name: "Retry Talk" });
    await act(async () => {
      fireEvent.click(retryButton);
      await waitFor(() => {
        expect(getUserMedia).toHaveBeenCalledTimes(2);
        expect(FakeWebSocket.instances).toHaveLength(2);
      });
    });

    expect(getUserMedia).toHaveBeenCalledTimes(2);
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(FakeWebSocket.instances[0].readyState).toBe(3);
    expect(screen.getByText("Listening")).toBeInTheDocument();
  });

  it("opens a fresh media session after the realtime socket drops", async () => {
    const getUserMedia = vi.fn().mockResolvedValue(new FakeStream());
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia },
      configurable: true,
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({
        session_id: "session-1",
        media_session_id: `media-${FakeWebSocket.instances.length + 1}`,
        websocket_url: `ws://127.0.0.1:43817/api/voice/realtime/media-${FakeWebSocket.instances.length + 1}`,
      }),
      { status: 200 },
    )));
    vi.stubGlobal("WebSocket", FakeWebSocket);
    render(<TalkMode sessionId="session-1" />);

    fireEvent.click(screen.getByRole("button", { name: "Start Talk mode" }));
    await screen.findByText("Listening");
    act(() => FakeWebSocket.instances[0].close());

    const retry = await screen.findByRole("button", { name: "Retry Talk" });
    await act(async () => {
      fireEvent.click(retry);
      await waitFor(() => {
        expect(getUserMedia).toHaveBeenCalledTimes(2);
        expect(FakeWebSocket.instances).toHaveLength(2);
      });
    });
    await screen.findByText("Listening");

    expect(getUserMedia).toHaveBeenCalledTimes(2);
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(FakeWebSocket.instances[0].readyState).toBe(3);
    expect(FakeWebSocket.instances[1].url).toContain("media-2");
  });

  it("keeps Talk listening after an unrecognized speech segment", async () => {
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia: vi.fn().mockResolvedValue(new FakeStream()) },
      configurable: true,
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({
        session_id: "session-1",
        media_session_id: "media-1",
        websocket_url: "ws://127.0.0.1:43817/api/voice/realtime/media-1",
      }),
      { status: 200 },
    )));
    vi.stubGlobal("WebSocket", FakeWebSocket);
    render(<TalkMode sessionId="session-1" />);

    fireEvent.click(screen.getByRole("button", { name: "Start Talk mode" }));
    await screen.findByText("Listening");
    act(() => {
      FakeWebSocket.instances[0].onmessage?.({
        data: JSON.stringify({
          schema_version: 1,
          type: "error",
          session_id: "session-1",
          media_session_id: "media-1",
          producer_seq: 1,
          stage: "input",
          code: "speech_not_recognized",
          message: "I couldn't recognize that speech. Please say it again; Talk is still listening.",
          recoverable: true,
        }),
      });
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("Please say it again");
    expect(screen.getByRole("button", { name: "Stop Talk mode" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry Talk" })).not.toBeInTheDocument();
  });

  it("refuses a remote-device microphone without HTTPS", async () => {
    Object.defineProperty(window, "isSecureContext", {
      value: false,
      configurable: true,
    });
    render(<TalkMode sessionId="session-1" />);

    fireEvent.click(screen.getByRole("button", { name: "Start Talk mode" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("hexis tunnel start");
  });
});
