import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TalkMode } from "./talk-mode";

vi.mock("../avatar-preview/avatar", () => ({
  Avatar: ({ value }: { value: { eye: string; pose: string } }) => (
    <div data-testid="talk-avatar-engine" aria-label={`眼形 ${value.eye}，活动 ${value.pose}`}>{value.pose}</div>
  ),
}));

const captureMocks = vi.hoisted(() => ({
  start: vi.fn().mockResolvedValue(undefined),
  close: vi.fn().mockResolvedValue(undefined),
  pause: vi.fn(() => ({
    captureInstanceId: "capture-1",
    trackId: "track-1",
    ready: Promise.resolve(),
  })),
  resume: vi.fn().mockResolvedValue(undefined),
  onBargeIn: undefined as ((target?: { response_id: string; playback_id: string; output_epoch: number }) => void) | undefined,
  captureInstanceId: "capture-1",
  id: "capture-1",
}));

vi.mock("@/lib/voice-realtime/capture", () => ({
  AudioCaptureController: vi.fn().mockImplementation((_client, id, _onError, onBargeIn) => {
    captureMocks.onBargeIn = onBargeIn;
    captureMocks.captureInstanceId = id;
    captureMocks.id = id;
    captureMocks.pause.mockImplementation(() => ({
      captureInstanceId: id,
      trackId: "track-1",
      ready: Promise.resolve(),
    }));
    return captureMocks;
  }),
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
  static nextServerSequence = 1;
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  private messageHandler: ((event: { data: string }) => void) | null = null;
  set onmessage(handler: ((event: { data: string }) => void) | null) {
    this.messageHandler = handler && ((event) => {
      const message = JSON.parse(event.data) as { type?: string; producer_seq?: number };
      if (message.type !== "ready") message.producer_seq = FakeWebSocket.nextServerSequence++;
      handler({ data: JSON.stringify(message) });
    });
  }
  get onmessage() {
    return this.messageHandler;
  }
  private listeners: Record<string, Array<() => void>> = {};

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
    queueMicrotask(() => {
      this.open();
      const mediaSessionId = new URL(this.url).pathname.split("/").at(-1) ?? "media-1";
      this.messageHandler?.({ data: JSON.stringify({
        schema_version: 1,
        type: "ready",
        session_id: "session-1",
        media_session_id: mediaSessionId,
        producer_seq: 0,
        cloud_session_id: "qwen-session",
        instructions_hash: "a".repeat(64),
        barge_in_capability: {
          supported: true,
          configuration_id: "voice-barge-in-playback-initial-v1",
          maximum_receipt_prefix_items: 256,
        },
      }) });
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
    this.listeners.close?.forEach((listener) => listener());
    this.onclose?.();
  }
}

function configureSelectionMedia() {
  const getUserMedia = vi.fn().mockResolvedValue(new FakeStream());
  Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia }, configurable: true });
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
    session_id: "session-1", media_session_id: "media-1",
    websocket_url: "ws://127.0.0.1:43817/api/voice/realtime/media-1",
    barge_in_capability: { supported: true, configuration_id: "voice-barge-in-playback-initial-v1", maximum_receipt_prefix_items: 256 },
  }), { status: 200 })));
  vi.stubGlobal("WebSocket", FakeWebSocket);
  return getUserMedia;
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
    vi.useRealTimers();
    captureMocks.start.mockClear();
    captureMocks.close.mockReset().mockResolvedValue(undefined);
    captureMocks.pause.mockClear();
    captureMocks.resume.mockReset().mockResolvedValue(undefined);
    captureMocks.onBargeIn = undefined;
    vi.unstubAllGlobals();
    FakeWebSocket.instances = [];
    FakeWebSocket.nextServerSequence = 1;
    Object.defineProperty(window, "isSecureContext", {
      value: true,
      configurable: true,
    });
  });

  it("keeps a closed microphone closed when the task selection closes", () => {
    const getUserMedia = configureSelectionMedia();
    const view = render(<TalkMode sessionId="session-1" selectionOpen />);
    expect(screen.getByRole("button", { name: "Start Talk mode" })).toBeDisabled();
    view.rerender(<TalkMode sessionId="session-1" selectionOpen={false} />);
    expect(screen.getByRole("button", { name: "Start Talk mode" })).toBeEnabled();
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(captureMocks.pause).not.toHaveBeenCalled();
    expect(captureMocks.resume).not.toHaveBeenCalled();
  });

  it.each(["manual-close", "session-switch"])("does not resume a selection token invalidated by %s", async (action) => {
    const getUserMedia = configureSelectionMedia();
    const view = render(<TalkMode sessionId="session-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Start Talk mode" }));
    await screen.findByText("Listening");
    view.rerender(<TalkMode sessionId="session-1" selectionOpen />);
    await screen.findByText("Talk paused for task selection");
    const nextSession = action === "session-switch" ? "session-2" : "session-1";
    if (action === "manual-close") {
      fireEvent.click(screen.getByRole("button", { name: "Stop Talk mode" }));
    } else {
      view.rerender(<TalkMode sessionId={nextSession} selectionOpen />);
    }
    await screen.findByRole("button", { name: "Start Talk mode" });
    view.rerender(<TalkMode sessionId={nextSession} selectionOpen={false} />);
    expect(captureMocks.resume).not.toHaveBeenCalled();
    expect(getUserMedia).toHaveBeenCalledOnce();
    expect(captureMocks.close).toHaveBeenCalledOnce();
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
          barge_in_capability: { supported: true, configuration_id: "voice-barge-in-playback-initial-v1", maximum_receipt_prefix_items: 256 },
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

    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Stop Talk mode" })));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Start Talk mode" }),
      ).toBeInTheDocument(),
    );
    expect(track.stop).toHaveBeenCalled();
    expect(FakeWebSocket.instances[0].readyState).toBe(3);
  });



  it("pauses the active capture for an M5 selection and resumes only after it closes", async () => {
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia: vi.fn().mockResolvedValue(new FakeStream()) },
      configurable: true,
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({
        session_id: "session-1",
        media_session_id: "media-1",
        websocket_url: "ws://127.0.0.1:43817/api/voice/realtime/media-1",
        barge_in_capability: { supported: true, configuration_id: "voice-barge-in-playback-initial-v1", maximum_receipt_prefix_items: 256 },
      }),
      { status: 200 },
    )));
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const onSelectionOpened = vi.fn();
    const view = render(
      <TalkMode sessionId="session-1" onSelectionOpened={onSelectionOpened} />
    );
    fireEvent.click(screen.getByRole("button", { name: "Start Talk mode" }));
    await screen.findByText("Listening");

    act(() => FakeWebSocket.instances[0].onmessage?.({
      data: JSON.stringify({
        schema_version: 1,
        type: "m5_selection_opened",
        session_id: "session-1",
        media_session_id: "media-1",
        producer_seq: 3,
        selection: {
          selection_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          version: 1,
          message: "Continue with the next question.",
          state: "open",
          origin_session_id: "session-1",
        },
      }),
    }));

    await screen.findByText("Talk paused for task selection");
    expect(captureMocks.pause).toHaveBeenCalledOnce();
    expect(onSelectionOpened).toHaveBeenCalledWith(expect.objectContaining({
      selection_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    }));

    view.rerender(
      <TalkMode
        sessionId="session-1"
        selectionOpen
        onSelectionOpened={onSelectionOpened}
      />
    );
    expect(captureMocks.pause).toHaveBeenCalledOnce();
    view.rerender(
      <TalkMode
        sessionId="session-1"
        selectionOpen={false}
        onSelectionOpened={onSelectionOpened}
      />
    );

    await waitFor(() => expect(screen.getByText("Listening")).toBeInTheDocument());
    expect(captureMocks.resume).toHaveBeenCalledOnce();
  });

  it("shows the avatar while microphone permission is still pending without claiming listening", async () => {
    let rejectPermission: ((error: Error) => void) | undefined;
    const getUserMedia = vi.fn(() => new Promise((_resolve, reject) => { rejectPermission = reject; }));
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia },
      configurable: true,
    });
    vi.stubGlobal("fetch", vi.fn());
    render(<TalkMode sessionId="session-1" />);

    fireEvent.click(screen.getByRole("button", { name: "Start Talk mode" }));

    expect(await screen.findByTestId("voice-avatar")).toBeInTheDocument();
    expect(screen.getByTestId("talk-avatar-engine")).toHaveTextContent("idle");
    expect(screen.queryByText("Listening")).not.toBeInTheDocument();
    await act(async () => rejectPermission?.(new Error("Permission denied")));
    expect(await screen.findByRole("alert")).toHaveTextContent("Permission denied");
  });

  it("hides the avatar immediately when closing while media cleanup continues", async () => {
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia: vi.fn().mockResolvedValue(new FakeStream()) },
      configurable: true,
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({
        session_id: "session-1",
        media_session_id: "media-1",
        websocket_url: "ws://127.0.0.1:43817/api/voice/realtime/media-1",
        barge_in_capability: { supported: true, configuration_id: "voice-barge-in-playback-initial-v1", maximum_receipt_prefix_items: 256 },
      }),
      { status: 200 },
    )));
    vi.stubGlobal("WebSocket", FakeWebSocket);
    let finishClose: (() => void) | undefined;
    captureMocks.close.mockImplementation(() => new Promise<void>((resolve) => { finishClose = resolve; }));
    render(<TalkMode sessionId="session-1" />);

    fireEvent.click(screen.getByRole("button", { name: "Start Talk mode" }));
    await screen.findByText("Listening");
    expect(screen.getByTestId("voice-avatar")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Stop Talk mode" }));

    expect(screen.queryByTestId("voice-avatar")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stopping Talk mode" })).toBeDisabled();
    await act(async () => finishClose?.());
    await screen.findByRole("button", { name: "Start Talk mode" });
  });

  it("projects only current production activities and gives recalling precedence", async () => {
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia: vi.fn().mockResolvedValue(new FakeStream()) },
      configurable: true,
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({
        session_id: "session-1",
        media_session_id: "media-1",
        websocket_url: "ws://127.0.0.1:43817/api/voice/realtime/media-1",
        barge_in_capability: { supported: true, configuration_id: "voice-barge-in-playback-initial-v1", maximum_receipt_prefix_items: 256 },
      }),
      { status: 200 },
    )));
    vi.stubGlobal("WebSocket", FakeWebSocket);
    render(<TalkMode sessionId="session-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Start Talk mode" }));
    await screen.findByText("Listening");

    const identity = { session_id: "session-1", input_turn_id: "input-1", input_order: 1, stimulus_revision: 0 };
    const send = (seq: number, event_type: string, payload: Record<string, unknown>) => {
      act(() => FakeWebSocket.instances[0].onmessage?.({
        data: JSON.stringify({
          schema_version: 1,
          type: "event",
          session_id: "session-1",
          media_session_id: "media-1",
          producer_seq: seq,
          event: {
            event_id: `event-${seq}`,
            schema_version: 1,
            event_type,
            stream_id: "voice-realtime-session",
            seq,
            session_id: "session-1",
            source: "server",
            producer_id: "hexis.voice-realtime",
            producer_seq: seq,
            payload,
            media_session_id: "media-1",
          },
        }),
      }));
    };

    send(1, "voice_input_committed", { identity });
    send(2, "voice_visual_window", {
      identity,
      wordset_version: "v1",
      fact: { status: "open", owner: "service", decision_seq: 0 },
    });
    send(3, "voice_visual_window", {
      identity,
      wordset_version: "v1",
      fact: { status: "accepted", owner: "service", decision_seq: 1 },
      label: "happy",
    });
    send(4, "voice_model_call_started", { identity, activity_id: "response-1", state: "started" });
    send(5, "voice_recall_started", { identity, activity_id: "call-1", state: "started" });
    expect(screen.getByTestId("voice-stage-bubble")).toHaveTextContent("recalling");
    expect(screen.getByTestId("voice-emotion-bubble")).toHaveTextContent("happy");
    expect(screen.getByTestId("talk-avatar-engine")).toHaveAttribute("aria-label", "眼形 happy，活动 thinking");

    send(6, "voice_recall_ended", { identity, activity_id: "call-1", state: "ended" });
    expect(screen.getByTestId("voice-stage-bubble")).toHaveTextContent("thinking");
    const nextIdentity = { ...identity, input_turn_id: "input-2", input_order: 2 };
    send(7, "voice_input_committed", { identity: nextIdentity });
    expect(screen.queryByTestId("voice-stage-bubble")).not.toBeInTheDocument();
    expect(screen.queryByTestId("voice-emotion-bubble")).not.toBeInTheDocument();
    send(8, "voice_recall_started", { identity, activity_id: "old-recall", state: "started" });
    send(9, "voice_model_call_started", { identity: nextIdentity, activity_id: "response-2", state: "started" });
    send(10, "waiting_for_user", { identity: nextIdentity, state: "started" });
    expect(screen.getByTestId("voice-stage-bubble")).toHaveTextContent("thinking");
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
        websocket_url: `ws://127.0.0.1:43817/api/voice/realtime/media-${FakeWebSocket.instances.length + 1}`,
        barge_in_capability: { supported: true, configuration_id: "voice-barge-in-playback-initial-v1", maximum_receipt_prefix_items: 256 },
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
        barge_in_capability: { supported: true, configuration_id: "voice-barge-in-playback-initial-v1", maximum_receipt_prefix_items: 256 },
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
        barge_in_capability: { supported: true, configuration_id: "voice-barge-in-playback-initial-v1", maximum_receipt_prefix_items: 256 },
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

  it("快速接收超过16块合法PCM，打断后丢弃旧包并能继续下一轮", async () => {
    const receipts = await startPlaybackTalk();
    const pcm = btoa("\0".repeat(960)); // 20ms@24kHz
    act(() => {
      for (let index = 0; index < 32; index++) sendOutput("response-1", index, pcm);
    });
    await waitFor(() => expect(PlaybackContext.instances[0]?.sources).toHaveLength(32));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    act(() => captureMocks.onBargeIn?.());
    await waitFor(() => expect(receipts.some((event) => event.reason === "barge_in")).toBe(true));
    act(() => {
      sendOutput("response-1", 32, pcm);
      sendOutput("response-2", 0, pcm);
    });
    await waitFor(() => expect(PlaybackContext.instances).toHaveLength(2));
    expect(PlaybackContext.instances[0].close).toHaveBeenCalled();
    expect(PlaybackContext.instances[1].sources).toHaveLength(1);
    expect(await screen.findByText("Speaking")).toBeInTheDocument();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Stop Talk mode" })));
  });

  it("先停止匹配播放器，再把真实未ACK回执原样带入目标确认", async () => {
    await startPlaybackTalk();
    const socket = FakeWebSocket.instances[0];
    const order: string[] = [];
    const pendingReceipts: Array<(response: Response) => void> = [];
    vi.mocked(fetch).mockImplementation(async (url, options) => {
      if (url === "/api/voice/playback") {
        const body = JSON.parse(String(options?.body)) as { action: string };
        order.push(`receipt:${body.action}`);
        return new Promise<Response>((resolve) => pendingReceipts.push(resolve));
      }
      throw new Error(`unexpected fetch ${String(url)}`);
    });
    const originalSend = socket.send.bind(socket);
    socket.send = vi.fn((data: string) => {
      order.push("websocket-confirm");
      originalSend(data);
    });
    await act(async () => sendOutput("response-1", 0, btoa("\0".repeat(4800))));
    await waitFor(() => expect(PlaybackContext.instances[0]?.sources).toHaveLength(1));
    PlaybackContext.instances[0].sources[0].stop.mockImplementation(() => order.push("local-stop"));

    act(() => captureMocks.onBargeIn?.({
      response_id: "response-1",
      playback_id: "play-response-1",
      output_epoch: 1,
    }));

    const confirmation = JSON.parse(socket.sent.at(-1) || "{}") as {
      type: string;
      capture_instance_id: string;
      response_id: string;
      playback_id: string;
      output_epoch: number;
      local_playback_state: string;
      receipt_prefix: Array<Record<string, unknown>>;
    };
    expect(confirmation).toMatchObject({
      type: "barge_in_confirmed",
      session_id: "session-1",
      media_session_id: "media-1",
      capture_instance_id: captureMocks.id,
      response_id: "response-1",
      playback_id: "play-response-1",
      output_epoch: 1,
      local_playback_state: "started",
    });
    expect(confirmation.receipt_prefix.map((receipt) => receipt.action)).toEqual(["open", "interrupt"]);
    expect(order.indexOf("local-stop")).toBeLessThan(order.indexOf("websocket-confirm"));
    expect(JSON.parse(socket.sent.at(-1) || "{}").producer_seq).toBe(0);
    for (const resolve of pendingReceipts) resolve(new Response("{}", { status: 200 }));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Stop Talk mode" })));
  });

  it("无播放器目标使用空prefix并先隔离迟到音频", async () => {
    await startPlaybackTalk();
    const socket = FakeWebSocket.instances[0];

    act(() => captureMocks.onBargeIn?.({
      response_id: "response-1",
      playback_id: "play-response-1",
      output_epoch: 1,
    }));

    const confirmation = JSON.parse(socket.sent.at(-1) || "{}") as {
      type: string;
      local_playback_state: string;
      receipt_prefix: unknown[];
    };
    expect(confirmation).toMatchObject({
      type: "barge_in_confirmed",
      local_playback_state: "not_started",
      receipt_prefix: [],
    });
    act(() => sendOutput("response-1", 0, btoa("\0\0")));
    expect(PlaybackContext.instances).toHaveLength(0);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Stop Talk mode" })));
  });

  it("首可听时显示Speaking，生成结束后保持到播放完毕才恢复Listening", async () => {
    await startPlaybackTalk();
    vi.useFakeTimers();
    await act(async () => sendOutput("response-1", 0, btoa("\0".repeat(4800))));
    expect(screen.getByText("Listening")).toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(20));
    expect(screen.getByText("Speaking")).toBeInTheDocument();
    expect(screen.queryByText(/Talk is starting/)).not.toBeInTheDocument();
    await act(async () => sendGeneration("response-1", "done"));
    expect(screen.getByText("Speaking")).toBeInTheDocument();
    const context = PlaybackContext.instances[0];
    context.currentTime = 0.12;
    await act(async () => context.sources[0].onended?.());
    expect(screen.getByText("Listening")).toBeInTheDocument();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Stop Talk mode" })));
    expect(screen.queryByText("Speaking")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start Talk mode" })).toBeInTheDocument();
  });

  it("waits for the open receipt before sending later playback lifecycle receipts", async () => {
    await startPlaybackTalk();
    const originalFetch = vi.mocked(fetch).getMockImplementation()!;
    const actions: string[] = [];
    let releaseOpen: () => void = () => undefined;
    vi.mocked(fetch).mockImplementation(async (url, options) => {
      if (url === "/api/voice/playback") {
        const event = JSON.parse(String(options?.body)) as { action: string };
        actions.push(event.action);
        if (event.action === "open") await new Promise<void>((resolve) => { releaseOpen = resolve; });
      }
      return originalFetch(url, options);
    });
    vi.useFakeTimers();
    await act(async () => sendOutput("response-1", 0, btoa("\0".repeat(4800))));
    await act(async () => vi.advanceTimersByTimeAsync(25));
    await act(async () => sendGeneration("response-1", "done"));
    const context = PlaybackContext.instances[0];
    context.currentTime = 0.12;
    await act(async () => context.sources[0].onended?.());
    expect(actions).toEqual(["open"]);
    await act(async () => releaseOpen());
    await vi.waitFor(() => expect(actions).toContain("end"));
    expect(actions[0]).toBe("open");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Stop Talk mode" })));
  });

  it("说话打断后立即恢复Listening，迟到包不改变状态，下一轮再显示Speaking", async () => {
    await startPlaybackTalk();
    vi.useFakeTimers();
    await act(async () => sendOutput("response-1", 0, btoa("\0".repeat(4800))));
    await act(async () => vi.advanceTimersByTimeAsync(20));
    expect(screen.getByText("Speaking")).toBeInTheDocument();
    await act(async () => captureMocks.onBargeIn?.());
    expect(screen.getByText("Listening")).toBeInTheDocument();
    await act(async () => sendOutput("response-1", 1, btoa("\0\0")));
    expect(screen.getByText("Listening")).toBeInTheDocument();
    await act(async () => sendOutput("response-2", 0, btoa("\0".repeat(4800))));
    await act(async () => vi.advanceTimersByTimeAsync(20));
    expect(screen.getByText("Speaking")).toBeInTheDocument();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Stop Talk mode" })));
    expect(screen.queryByText("Speaking")).not.toBeInTheDocument();
  });

  it("某个响应播放结束时，其他仍在播放的响应保持Speaking", async () => {
    await startPlaybackTalk();
    vi.useFakeTimers();
    await act(async () => {
      sendOutput("response-1", 0, btoa("\0".repeat(4800)));
      sendOutput("response-2", 0, btoa("\0".repeat(4800)));
    });
    await act(async () => vi.advanceTimersByTimeAsync(20));
    await act(async () => sendGeneration("response-1", "done"));
    PlaybackContext.instances[0].currentTime = 0.12;
    await act(async () => PlaybackContext.instances[0].sources[0].onended?.());
    expect(screen.getByText("Speaking")).toBeInTheDocument();
    await act(async () => sendGeneration("response-2", "failed"));
    expect(screen.getByText("Listening")).toBeInTheDocument();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Stop Talk mode" })));
  });

  it("超限显示首因，停止失败响应且忽略迟到包，下一轮恢复播放", async () => {
    const receipts = await startPlaybackTalk();
    act(() => sendOutput("response-1", 0, btoa("\0".repeat(4800))));
    await waitFor(() => expect(receipts.some((event) => event.action === "open")).toBe(true));
    // 超过120秒预算的大块必须在交付待命读者前被拒绝。
    act(() => sendOutput("response-1", 1, btoa("\0".repeat(24_000 * 2 * 121))));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/缓冲.*上限/);
    expect(alert).not.toHaveTextContent("检查音频格式");
    const firstError = alert.textContent;
    await waitFor(() => expect(PlaybackContext.instances[0].close).toHaveBeenCalled());
    act(() => sendOutput("response-1", 2, btoa("\0\0")));
    expect(screen.getByRole("alert").textContent).toBe(firstError);
    expect(PlaybackContext.instances).toHaveLength(1);
    expect(receipts.filter((event) => event.action === "interrupt")).toEqual([
      expect.objectContaining({ response_id: "response-1", reason: "error" }),
    ]);
    act(() => sendOutput("response-2", 0, btoa("\0\0")));
    await waitFor(() => expect(PlaybackContext.instances).toHaveLength(2));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("Listening")).toBeInTheDocument();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Stop Talk mode" })));
  });

  it("正常drained后释放音频上下文，迟到包不能重新启动已完成响应", async () => {
    const receipts = await startPlaybackTalk();
    act(() => sendOutput("response-1", 0, btoa("\0".repeat(4800))));
    await waitFor(() => expect(PlaybackContext.instances[0]?.sources).toHaveLength(1));
    act(() => sendGeneration("response-1", "done"));
    await act(async () => { await Promise.resolve(); });
    const context = PlaybackContext.instances[0];
    context.currentTime = 0.12;
    act(() => context.sources[0].onended?.());
    await waitFor(() => expect(receipts.some((event) => event.action === "end")).toBe(true));
    expect(context.close).toHaveBeenCalledTimes(1);
    act(() => sendOutput("response-1", 1, btoa("\0\0")));
    expect(PlaybackContext.instances).toHaveLength(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Stop Talk mode" })));
  });

  it("播放器内部故障显示真实首因并释放上下文，新的响应仍能播放", async () => {
    const receipts = await startPlaybackTalk();
    act(() => sendOutput("response-1", 0, btoa("\0".repeat(4800))));
    await waitFor(() => expect(PlaybackContext.instances[0]?.sources).toHaveLength(1));
    vi.spyOn(PlaybackContext.instances[0], "createBuffer").mockImplementation(() => { throw new Error("audio device failed"); });
    act(() => sendOutput("response-1", 1, btoa("\0\0")));
    expect(await screen.findByRole("alert")).toHaveTextContent("audio device failed");
    expect(screen.getByRole("alert")).not.toHaveTextContent("检查音频格式");
    await waitFor(() => expect(PlaybackContext.instances[0].close).toHaveBeenCalledTimes(1));
    expect(receipts.filter((event) => event.action === "interrupt")).toEqual([
      expect.objectContaining({ reason: "error" }),
    ]);
    act(() => sendOutput("response-1", 2, btoa("\0\0")));
    expect(screen.getByRole("alert")).toHaveTextContent("audio device failed");
    act(() => sendOutput("response-2", 0, btoa("\0\0")));
    await waitFor(() => expect(PlaybackContext.instances).toHaveLength(2));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Stop Talk mode" })));
  });

  it("生成失败立即停播，终态回执失败不会覆盖原始原因", async () => {
    const receipts = await startPlaybackTalk();
    act(() => sendOutput("response-1", 0, btoa("\0".repeat(4800))));
    await waitFor(() => expect(receipts.some((event) => event.action === "open")).toBe(true));
    vi.mocked(fetch).mockResolvedValueOnce(new Response("{}", { status: 409 }));
    await act(async () => sendGeneration("response-1", "failed"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Voice generation failed.");
    expect(screen.getByRole("alert")).not.toHaveTextContent("receipt was rejected");
    expect(PlaybackContext.instances[0].sources[0].stop).toHaveBeenCalledTimes(1);
    expect(PlaybackContext.instances[0].close).toHaveBeenCalledTimes(1);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Stop Talk mode" })));
  });
});

class PlaybackContext {
  static instances: PlaybackContext[] = [];
  currentTime = 0;
  destination = {};
  resume = vi.fn(async () => undefined);
  close = vi.fn(async () => undefined);
  sources: Array<{ buffer: unknown; onended: (() => void) | null; start: ReturnType<typeof vi.fn>; stop: ReturnType<typeof vi.fn>; connect: ReturnType<typeof vi.fn>; disconnect: ReturnType<typeof vi.fn> }> = [];
  constructor() { PlaybackContext.instances.push(this); }
  createAnalyser() { return { fftSize: 32, connect: vi.fn(), getByteTimeDomainData: vi.fn() }; }
  createBuffer(_channels: number, frames: number) { return { getChannelData: () => new Float32Array(frames) }; }
  createBufferSource() {
    const node = { buffer: undefined as unknown, onended: null as (() => void) | null, start: vi.fn(), stop: vi.fn(), connect: vi.fn(), disconnect: vi.fn() };
    this.sources.push(node);
    return node;
  }
}

async function startPlaybackTalk() {
  PlaybackContext.instances = [];
  vi.stubGlobal("AudioContext", PlaybackContext);
  vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia: vi.fn().mockResolvedValue(new FakeStream()) }, configurable: true });
  const receipts: Array<{ action: string; response_id: string; reason?: string }> = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, options?: RequestInit) => {
    if (url === "/api/voice/playback") {
      receipts.push(JSON.parse(String(options?.body)));
      return new Response("{}", { status: 200 });
    }
    return new Response(JSON.stringify({ session_id: "session-1", media_session_id: "media-1", websocket_url: "ws://127.0.0.1:43817/api/voice/realtime/media-1", barge_in_capability: { supported: true, configuration_id: "voice-barge-in-playback-initial-v1", maximum_receipt_prefix_items: 256 } }), { status: 200 });
  }));
  vi.stubGlobal("WebSocket", FakeWebSocket);
  render(<TalkMode sessionId="session-1" />);
  fireEvent.click(screen.getByRole("button", { name: "Start Talk mode" }));
  await screen.findByText("Listening");
  return receipts;
}

function sendOutput(response_id: string, chunk_index: number, pcm_base64: string) {
  FakeWebSocket.instances[0].onmessage?.({ data: JSON.stringify({ schema_version: 1, type: "audio_chunk", session_id: "session-1", media_session_id: "media-1", producer_seq: chunk_index + 1, response_id, playback_id: `play-${response_id}`, turn_id: null, output_epoch: 1, segment_id: "segment-1", segment_index: 0, chunk_seq: chunk_index + 1, pcm_base64 }) });
}

function sendGeneration(response_id: string, kind: "done" | "failed") {
  FakeWebSocket.instances[0].onmessage?.({ data: JSON.stringify({ schema_version: 1, type: "generation", session_id: "session-1", media_session_id: "media-1", producer_seq: 100, response_id, playback_id: `play-${response_id}`, turn_id: null, output_epoch: 1, kind, reason: null }) });
}
