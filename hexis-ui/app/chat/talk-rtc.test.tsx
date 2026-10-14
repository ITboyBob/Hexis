import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TalkMode } from "./talk-mode";

const rtc = vi.hoisted(() => ({ options: undefined as any, close: vi.fn(), capture: vi.fn() }));
vi.mock("@/lib/voice-realtime/client", () => ({ VoiceRealtimeClient: class {
  activeSessionId = "chat"; activeMediaSessionId = "media";
  constructor(options: any) { rtc.options = options; }
  async open() { rtc.options.onStateChange("connecting"); rtc.options.onStateChange("ready"); }
  async close() { rtc.close(); rtc.options.onStateChange("closed"); }
  setCaptureEnabled(value: boolean) { rtc.capture(value); }
} }));
vi.mock("./voice-avatar", () => ({ VoiceAvatar: (props: any) => <div data-testid="voice-avatar" data-emotion={props.emotionLabel} /> }));

beforeEach(() => vi.clearAllMocks());
describe("WebRTC Talk 页面", () => {
  it("用户最终转录和情绪走独立回调，保存回执可回读历史", async () => {
    const onChatText = vi.fn(); const onVoiceTurnReady = vi.fn();
    render(<TalkMode sessionId="chat" onChatText={onChatText} onVoiceTurnReady={onVoiceTurnReady} />);
    fireEvent.click(screen.getByRole("button", { name: "Start Talk mode" }));
    await screen.findByText("Talk connected");
    const identity = { schema_version: 2, session_id: "chat", media_session_id: "media", input_turn_id: "input", input_order: 0, turn_id: "turn" };
    act(() => rtc.options.onMessage({ ...identity, producer_seq: 1, type: "input_committed" }));
    act(() => rtc.options.onMessage({ ...identity, producer_seq: 2, type: "user_text", text: "你好" }));
    act(() => rtc.options.onMessage({ ...identity, producer_seq: 3, type: "emotion_result", label: "happy", wordset_version: "v1" }));
    expect(onChatText).toHaveBeenCalledWith(expect.objectContaining({ kind: "user", inputTurnId: "input", text: "你好" }));
    expect(screen.getByTestId("voice-avatar")).toHaveAttribute("data-emotion", "happy");
    act(() => rtc.options.onMessage({ ...identity, producer_seq: 4, type: "user_text_saved", text: "你好", message_id: "saved" }));
    expect(onVoiceTurnReady).toHaveBeenCalledWith({ sessionId: "chat", turnId: "turn" });
  });
  it("连接后只有会话状态，不出现 Speaking/Listening；用户停止会清理客户端", async () => {
    render(<TalkMode sessionId="chat" />);
    fireEvent.click(screen.getByRole("button", { name: "Start Talk mode" }));
    await screen.findByText("Talk connected");
    expect(screen.queryByText("Speaking")).toBeNull();
    expect(screen.queryByText("Listening")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Stop Talk mode" }));
    await waitFor(() => expect(rtc.close).toHaveBeenCalled());
  });
  it("只转发助手文字，保存确认不拼接重复文字", async () => {
    const onChatText = vi.fn(); const onVoiceTurnReady = vi.fn();
    render(<TalkMode sessionId="chat" onChatText={onChatText} onVoiceTurnReady={onVoiceTurnReady} />);
    fireEvent.click(screen.getByRole("button", { name: "Start Talk mode" }));
    await screen.findByText("Talk connected");
    act(() => rtc.options.onMessage({ type: "assistant_text", session_id: "chat", media_session_id: "media", turn_id: "turn", response_id: "response", text: "生成文字" }));
    act(() => rtc.options.onMessage({ type: "response_saved", session_id: "chat", media_session_id: "media", turn_id: "turn", response_id: "response", text: "生成文字", status: "completed" }));
    expect(onChatText).toHaveBeenCalledTimes(2);
    expect(onChatText.mock.calls[1][0]).toMatchObject({ kind: "assistant", final: true, text: "生成文字" });
    expect(onVoiceTurnReady).toHaveBeenCalledWith({ sessionId: "chat", turnId: "turn" });
  });
  it("选择任务只暂停麦克风；错误会显式展示", async () => {
    const view = render(<TalkMode sessionId="chat" />);
    fireEvent.click(screen.getByRole("button", { name: "Start Talk mode" }));
    await screen.findByText("Talk connected");
    view.rerender(<TalkMode sessionId="chat" selectionOpen />);
    expect(rtc.capture).toHaveBeenLastCalledWith(false);
    act(() => rtc.options.onError(new Error("受控错误")));
    expect(screen.getByRole("alert")).toHaveTextContent("受控错误");
  });
  it("结束或切换到新会话后，旧连接回调不会复活 Talk", async () => {
    const view = render(<TalkMode sessionId="chat" />);
    fireEvent.click(screen.getByRole("button", { name: "Start Talk mode" }));
    await screen.findByText("Talk connected");
    const old = rtc.options;
    view.rerender(<TalkMode sessionId={null} />);
    await waitFor(() => expect(rtc.close).toHaveBeenCalled());
    act(() => old.onStateChange("ready"));
    expect(screen.queryByText("Talk connected")).toBeNull();
  });
});
