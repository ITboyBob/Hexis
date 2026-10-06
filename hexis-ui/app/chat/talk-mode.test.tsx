import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TalkMode } from "./talk-mode";

const captureMocks = vi.hoisted(() => ({
  start: vi.fn().mockResolvedValue(undefined),
  close: vi.fn().mockResolvedValue(undefined),
  onBargeIn: undefined as (() => void) | undefined,
}));

vi.mock("@/lib/voice-realtime/capture", () => ({
  AudioCaptureController: vi.fn().mockImplementation((_client, _id, _onError, onBargeIn) => {
    captureMocks.onBargeIn = onBargeIn;
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
    vi.useRealTimers();
    captureMocks.start.mockClear();
    captureMocks.close.mockClear();
    captureMocks.onBargeIn = undefined;
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

    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Stop Talk mode" })));
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
    return new Response(JSON.stringify({ session_id: "session-1", media_session_id: "media-1", websocket_url: "ws://127.0.0.1:43817/api/voice/realtime/media-1" }), { status: 200 });
  }));
  vi.stubGlobal("WebSocket", FakeWebSocket);
  render(<TalkMode sessionId="session-1" />);
  fireEvent.click(screen.getByRole("button", { name: "Start Talk mode" }));
  await screen.findByText("Listening");
  return receipts;
}

function sendOutput(response_id: string, chunk_seq: number, pcm_base64: string) {
  FakeWebSocket.instances[0].onmessage?.({ data: JSON.stringify({ schema_version: 1, type: "audio_chunk", session_id: "session-1", media_session_id: "media-1", producer_seq: chunk_seq + 1, response_id, playback_id: `play-${response_id}`, output_epoch: 1, segment_id: "segment-1", segment_index: 0, chunk_seq, pcm_base64 }) });
}

function sendGeneration(response_id: string, kind: "done" | "failed") {
  FakeWebSocket.instances[0].onmessage?.({ data: JSON.stringify({ schema_version: 1, type: "generation", session_id: "session-1", media_session_id: "media-1", producer_seq: 100, response_id, playback_id: `play-${response_id}`, output_epoch: 1, kind }) });
}
