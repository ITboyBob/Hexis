import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { M5TaskSnapshot } from "@/lib/m5-tasks";
import type { VoiceChatText } from "./talk-mode";
import ChatPage from "./page";

const voice = vi.hoisted(() => ({
  onChatText: undefined as ((message: VoiceChatText) => void) | undefined,
  onVoiceTurnReady: undefined as ((turn: { sessionId: string; turnId: string }) => void) | undefined,
}));
vi.mock("./talk-mode", () => ({
  TalkMode: (props: typeof voice) => { Object.assign(voice, props); return null; },
}));

const origin = "11111111-1111-4111-8111-111111111111";
const taskSession = "22222222-2222-4222-8222-222222222222";
const base = { message_id: "base", role: "assistant", content: "来源历史" };
const report = (id = "report-message", content = "后台摘要") => ({
  message_id: id, role: "assistant", content,
  metadata: { message_kind: "m5_report", report_id: `report:${id}` },
});

class TaskEvents {
  static sources: TaskEvents[] = [];
  listeners = new Map<string, EventListener>();
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(readonly url: string) { TaskEvents.sources.push(this); }
  addEventListener(type: string, listener: EventListener) { this.listeners.set(type, listener); }
  close() { this.closed = true; }
  changed(seq: number, eventOrigin = origin) {
    this.listeners.get("m5-event")?.(new MessageEvent("m5-event", { data: JSON.stringify({
      event_id: `event:${seq}`, event_type: "m5_task_changed", session_id: eventOrigin,
      stream_id: `m5-tasks:${eventOrigin}`, seq, task_id: "task",
      payload: { ...snapshot.tasks[0], origin_session_id: eventOrigin, revision: seq + 1 },
    }) }));
  }
}

let snapshot: M5TaskSnapshot;
let messages: Record<string, unknown>[];
let hydrate: (() => Promise<Response>) | null;
let reads: number;

async function mounted() {
  render(<ChatPage />);
  await screen.findByText("来源历史");
  await waitFor(() => expect(TaskEvents.sources).toHaveLength(1));
  await waitFor(() => expect(reads).toBeGreaterThanOrEqual(2));
}

describe("Chat 后台摘要同步", () => {
  beforeEach(() => {
    TaskEvents.sources = [];
    reads = 0;
    hydrate = null;
    messages = [base];
    snapshot = {
      origin_session_id: origin, event_cursor: { "m5-tasks": 0 }, selections: [],
      tasks: [{ task_id: "task", task_title: "后台工作", task_state: "idle",
        task_session_id: taskSession, revision: 1, pending: [] }],
    };
    sessionStorage.setItem("hexis-chat-session-id", origin);
    vi.stubGlobal("EventSource", TaskEvents);
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    })));
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      const url = String(input);
      if (url === `/api/chat/session/${origin}`) {
        if (options?.method === "POST") {
          const cleared = messages.length;
          messages = [];
          hydrate = null;
          return Response.json({ cleared_messages: cleared });
        }
        reads++;
        return hydrate ? hydrate() : Response.json({ messages });
      }
      if (url === `/api/chat/session/${taskSession}`) return Response.json({ messages: [
        { message_id: "task-body", role: "assistant", content: "任务正文" },
      ] });
      if (url === "/api/chat/session") return Response.json({ session_id: "new-session" });
      if (url.startsWith("/api/m5/tasks?")) return Response.json(snapshot);
      if (url === "/api/status") return Response.json({ configured: true, agent_name: "Samantha" });
      if (url === "/api/outbox") return Response.json({ unread: 0, messages: [] });
      return Response.json({});
    }));
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    sessionStorage.clear();
  });

  it("未打开或关闭面板时，状态不变的通知仍合并报告且保留预览", async () => {
    messages.push({ message_id: "tool", role: "assistant", content: "", content_blocks: [
      { type: "toolCall", id: "call", name: "remember", arguments: { content: "事实" } },
    ] });
    await mounted();
    act(() => voice.onChatText?.({ sessionId: origin, kind: "assistant", turnId: "live", responseId: "live-response", text: "实时预览" }));
    messages = [base, report(), { message_id: "ordinary", role: "assistant", content: "不应额外导入" }];
    act(() => TaskEvents.sources[0].changed(1));
    await screen.findByText("后台摘要");
    expect(screen.getByText("实时预览")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Tool call: remember" })).toBeInTheDocument();
    expect(screen.queryByText("不应额外导入")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "任务列表" }));
    fireEvent.click(screen.getByRole("button", { name: "关闭任务列表" }));
    messages.push(report("second", "第二条摘要"));
    act(() => TaskEvents.sources[0].changed(2));
    await screen.findByText("第二条摘要");
    expect(TaskEvents.sources).toHaveLength(1);
    expect(TaskEvents.sources[0].closed).toBe(false);
    expect(screen.getAllByText("后台摘要")).toHaveLength(1);
    expect(screen.getAllByText("来源历史")).toHaveLength(1);
  });

  it("初始恢复未完成时暂缓同步，恢复后补入期间通知的报告", async () => {
    let releaseInitial: ((response: Response) => void) | undefined;
    hydrate = () => new Promise((resolve) => { releaseInitial = resolve; });
    render(<ChatPage />);
    await waitFor(() => expect(TaskEvents.sources).toHaveLength(1));
    expect(reads).toBe(1);
    messages = [base, report()];
    act(() => TaskEvents.sources[0].changed(1));
    expect(reads).toBe(1);
    hydrate = null;
    await act(async () => releaseInitial?.(Response.json({ messages: [base] })));
    await screen.findByText("后台摘要");
    expect(screen.getAllByText("来源历史")).toHaveLength(1);
    expect(reads).toBe(2);
  });

  it("缺少稳定数据库 ID 或非 assistant 的报告记录不自动并入", async () => {
    await mounted();
    const before = reads;
    messages.push(
      { role: "assistant", content: "缺少 ID", metadata: { message_kind: "m5_report" } },
      { ...report(""), content: "空 ID" },
      { ...report("user-report"), role: "user", content: "错误角色" },
    );
    act(() => TaskEvents.sources[0].changed(1));
    await waitFor(() => expect(reads).toBe(before + 1));
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByText("缺少 ID")).toBeNull();
    expect(screen.queryByText("空 ID")).toBeNull();
    expect(screen.queryByText("错误角色")).toBeNull();
  });

  it("卸载时关闭通知连接并取消在途报告请求", async () => {
    const view = render(<ChatPage />);
    await screen.findByText("来源历史");
    await waitFor(() => expect(reads).toBeGreaterThanOrEqual(2));
    hydrate = () => new Promise(() => {});
    const before = reads;
    act(() => TaskEvents.sources[0].changed(1));
    await waitFor(() => expect(reads).toBe(before + 1));
    const requests = vi.mocked(fetch).mock.calls.filter(([url]) => String(url) === `/api/chat/session/${origin}`);
    const signal = requests[requests.length - 1][1]?.signal;
    view.unmount();
    expect(signal?.aborted).toBe(true);
    expect(TaskEvents.sources[0].closed).toBe(true);
  });

  it("语音补拉与任务通知交叉时按数据库 ID 去重并保持消息顺序", async () => {
    await mounted();
    act(() => {
      voice.onChatText?.({ sessionId: origin, kind: "user", inputTurnId: "input", text: "用户语音" });
      voice.onChatText?.({ sessionId: origin, kind: "assistant", turnId: "turn", responseId: "response", text: "语音预览" });
    });
    messages = [base,
      { message_id: "user", role: "user", content: "用户语音", metadata: { channel: "voice", input_turn_id: "input" } },
      report(),
      { message_id: "assistant", role: "assistant", content: "语音最终文本", metadata: { channel: "voice", agent_turn_id: "turn" } },
    ];
    act(() => voice.onVoiceTurnReady?.({ sessionId: origin, turnId: "turn" }));
    await screen.findByText("语音最终文本");
    expect(screen.getByText("后台摘要")).toBeInTheDocument();
    expect(screen.getByText("用户语音").compareDocumentPosition(screen.getByText("后台摘要"))
      & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText("后台摘要").compareDocumentPosition(screen.getByText("语音最终文本"))
      & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    act(() => {
      TaskEvents.sources[0].changed(1);
      voice.onVoiceTurnReady?.({ sessionId: origin, turnId: "turn" });
    });
    await waitFor(() => expect(reads).toBeGreaterThanOrEqual(4));
    expect(screen.getAllByText("后台摘要")).toHaveLength(1);
  });

  it("SSE 故障时，关闭的面板仍通过两秒轮询补入摘要", async () => {
    await mounted();
    vi.useFakeTimers();
    messages.push(report());
    snapshot = { ...snapshot, event_cursor: { "m5-tasks": 1 } };
    act(() => TaskEvents.sources[0].onerror?.());
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(screen.getByText("后台摘要")).toBeInTheDocument();
    expect(TaskEvents.sources).toHaveLength(2);
    act(() => TaskEvents.sources[1].onopen?.());
    const calls = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).startsWith("/api/m5/tasks?" )).length;
    await act(async () => vi.advanceTimersByTimeAsync(4000));
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).startsWith("/api/m5/tasks?")).length).toBe(calls);
  });

  it("并发通知复用请求，完成后补拉一次且不按摘要文本去重", async () => {
    await mounted();
    let release: ((response: Response) => void) | undefined;
    const before = reads;
    hydrate = () => new Promise((resolve) => { release = resolve; });
    act(() => TaskEvents.sources[0].changed(1));
    await waitFor(() => expect(reads).toBe(before + 1));
    act(() => TaskEvents.sources[0].changed(2));
    expect(reads).toBe(before + 1);
    hydrate = null;
    messages = [base, report(), report("another-id")];
    await act(async () => release?.(Response.json({ messages: [base, report()] })));
    await waitFor(() => expect(reads).toBe(before + 2));
    expect(screen.getAllByText("后台摘要")).toHaveLength(2);
  });

  it("错误来源事件重取权威快照，任务子会话不显示来源摘要", async () => {
    await mounted();
    messages.push(report());
    act(() => TaskEvents.sources[0].changed(1, "wrong-origin"));
    await waitFor(() => expect(TaskEvents.sources).toHaveLength(2));
    fireEvent.click(screen.getByRole("button", { name: "任务列表" }));
    fireEvent.click(await screen.findByRole("button", { name: /进入任务：后台工作/ }));
    await screen.findByText("任务正文");
    act(() => TaskEvents.sources[1].changed(1));
    expect(screen.queryByText("后台摘要")).toBeNull();
  });

  it.each(["切换会话", "清理上下文"])("%s后丢弃迟到的报告响应", async (action) => {
    await mounted();
    let release: ((response: Response) => void) | undefined;
    hydrate = () => new Promise((resolve) => { release = resolve; });
    const before = reads;
    act(() => TaskEvents.sources[0].changed(1));
    await waitFor(() => expect(reads).toBe(before + 1));
    const staleResponse = release;
    if (action === "切换会话") {
      fireEvent.click(screen.getByRole("button", { name: "Start new conversation" }));
      await waitFor(() => expect(sessionStorage.getItem("hexis-chat-session-id")).toBe("new-session"));
    } else {
      vi.spyOn(window, "confirm").mockReturnValue(true);
      fireEvent.click(screen.getByRole("button", { name: "Clear active conversation context" }));
      await screen.findByText("Conversation context cleared; long-term memory preserved.");
    }
    await act(async () => staleResponse?.(Response.json({ messages: [base, report()] })));
    expect(screen.queryByText("后台摘要")).toBeNull();
    expect(screen.queryByText("来源历史")).toBeNull();
  });

  it("报告读取失败有限重试，保留原消息并提供重试入口", async () => {
    await mounted();
    vi.useFakeTimers();
    const before = reads;
    hydrate = async () => Response.json({ error: "temporary failure" }, { status: 503 });
    act(() => TaskEvents.sources[0].changed(1));
    await act(async () => { await Promise.resolve(); });
    await act(async () => vi.advanceTimersByTimeAsync(250));
    await act(async () => vi.advanceTimersByTimeAsync(750));
    await act(async () => vi.advanceTimersByTimeAsync(1500));
    expect(reads).toBe(before + 4);
    expect(screen.getByText("来源历史")).toBeInTheDocument();
    expect(screen.getByText(/后台摘要同步暂不可用/)).toBeInTheDocument();
    hydrate = null;
    messages.push(report());
    fireEvent.click(screen.getByRole("button", { name: "任务列表" }));
    fireEvent.click(screen.getByRole("button", { name: "重试任务列表" }));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(screen.getByText("后台摘要")).toBeInTheDocument();
  });
});
