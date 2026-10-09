import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { VoiceMemoryRecallMessage } from "@/lib/voice-memory-activity";
import ChatPage from "./page";

const talk = vi.hoisted(() => ({
  onMemoryRecall: undefined as ((message: VoiceMemoryRecallMessage) => void) | undefined,
}));
vi.mock("./talk-mode", () => ({
  TalkMode: ({ onMemoryRecall }: { onMemoryRecall?: (message: VoiceMemoryRecallMessage) => void }) => {
    talk.onMemoryRecall = onMemoryRecall;
    return null;
  },
}));

const sessionId = "11111111-1111-4111-8111-111111111111";
const nextSessionId = "33333333-3333-4333-8333-333333333333";

function recall(inputTurnId: string, content: string): VoiceMemoryRecallMessage {
  return {
    schema_version: 1,
    type: "memory_recall",
    session_id: sessionId,
    media_session_id: "media-1",
    producer_seq: 1,
    input_turn_id: inputTurnId,
    stimulus_revision: 0,
    count: 1,
    memories: [{ id: "22222222-2222-4222-8222-222222222222", type: "semantic", content }],
    hydrate_ms: 8.5,
    recall_performed: true,
  };
}

describe("ChatPage 语音水合 Activity", () => {
  beforeEach(() => {
    sessionStorage.clear();
    sessionStorage.setItem("hexis-chat-session-id", sessionId);
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    })));
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/status") return Response.json({ configured: true, agent_name: "Hexis" });
      if (url.pathname === "/api/outbox") return Response.json({ unread: 0, messages: [] });
      if (url.pathname.startsWith("/api/chat/session/")) return Response.json({ messages: [] });
      if (url.pathname === "/api/chat/session") return Response.json({ session_id: nextSessionId });
      if (url.pathname === "/api/chat/activity") return Response.json({
        session_id: url.searchParams.get("session_id"), cursor: {}, events: [], has_more: false,
      });
      return Response.json({});
    }));
  });

  afterEach(() => {
    talk.onMemoryRecall = undefined;
    vi.unstubAllGlobals();
    sessionStorage.clear();
  });

  it("接收真实页面回调，去重并保留两轮结果和刷新缓存", async () => {
    const view = render(<ChatPage />);
    await screen.findByRole("button", { name: /^Memory/ });
    act(() => talk.onMemoryRecall?.(recall("input-1", "第一轮水合内容")));
    await screen.findByText("第一轮水合内容");
    act(() => talk.onMemoryRecall?.(recall("input-1", "重复投递不得覆盖")));
    expect(screen.queryByText("重复投递不得覆盖")).toBeNull();
    act(() => talk.onMemoryRecall?.(recall("input-2", "第二轮水合内容")));
    fireEvent.click(screen.getByRole("button", { name: /^Memory/ }));
    expect(screen.getByText("第一轮水合内容")).toBeInTheDocument();
    expect(screen.getByText("第二轮水合内容")).toBeInTheDocument();
    expect(screen.getByText("2 recent events")).toBeInTheDocument();
    const cached = JSON.parse(sessionStorage.getItem("hexis-chat-activity-events") || "[]");
    expect(cached).toHaveLength(2);
    expect(cached.every((event: { persisted?: boolean }) => !event.persisted)).toBe(true);
    view.unmount();
    render(<ChatPage />);
    await screen.findByText("第二轮水合内容");
    fireEvent.click(screen.getByRole("button", { name: /^Memory/ }));
    expect(screen.getByText("第一轮水合内容")).toBeInTheDocument();
    expect(screen.getByText("2 recent events")).toBeInTheDocument();
  });

  it("切换会话后拒绝来源会话的迟到回调", async () => {
    render(<ChatPage />);
    await screen.findByRole("button", { name: /^Memory/ });
    const oldCallback = talk.onMemoryRecall;
    fireEvent.click(screen.getByRole("button", { name: "Start new conversation" }));
    await waitFor(() => expect(sessionStorage.getItem("hexis-chat-session-id")).toBe(nextSessionId));
    act(() => oldCallback?.(recall("late-input", "旧会话迟到内容")));
    expect(screen.queryByText("旧会话迟到内容")).toBeNull();
    const cached = JSON.parse(sessionStorage.getItem("hexis-chat-activity-events") || "[]");
    expect(cached.some((event: { id: string }) => event.id === "voice-memory:late-input:0")).toBe(false);
  });

  it("零结果及禁用召回都更新 Memory，过期缓存不恢复", async () => {
    const view = render(<ChatPage />);
    await screen.findByRole("button", { name: /^Memory/ });
    const zero = { ...recall("zero", ""), count: 0, memories: [] };
    act(() => talk.onMemoryRecall?.(zero));
    expect(screen.getByText("Retrieved 0 relevant memories")).toBeInTheDocument();
    act(() => talk.onMemoryRecall?.({ ...zero, input_turn_id: "restricted", recall_performed: false }));
    expect(screen.getByText("Memory recall was not performed by the selected policy")).toBeInTheDocument();
    view.unmount();
    const expired = JSON.parse(sessionStorage.getItem("hexis-chat-activity-events") || "[]")
      .map((event: Record<string, unknown>) => ({ ...event, ts: Date.now() - 31 * 60 * 1000 }));
    sessionStorage.setItem("hexis-chat-activity-events", JSON.stringify(expired));
    render(<ChatPage />);
    await screen.findByRole("button", { name: /^Memory/ });
    expect(screen.queryByText("Memory recall was not performed by the selected policy")).toBeNull();
    expect(screen.getByText("0 recent events")).toBeInTheDocument();
  });
});
