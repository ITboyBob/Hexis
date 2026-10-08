import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ControlledToolActivityScenario from "./scenario";

const push = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

const sessionId = "10800000-0000-4000-8000-000000000001";
const log = (kind: string, title: string) => `event: log\ndata: ${JSON.stringify({ id: kind, kind, title, detail: "详情" })}\n\n`;

function mockScenario(sse: string) {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce({ ok: true, json: async () => ({ session_id: sessionId }) })
    .mockResolvedValueOnce({ ok: true })
    .mockResolvedValueOnce({ ok: true, text: async () => sse }));
  render(<ControlledToolActivityScenario />);
  fireEvent.click(screen.getByRole("button", { name: "Run controlled recall" }));
}

describe("受控工具场景必须由真实工具事件与成功终态证实", () => {
  beforeEach(() => { vi.clearAllMocks(); sessionStorage.clear(); });

  it("只保存实际 request/result，保留 SSE event ID 后进入 Chat", async () => {
    mockScenario(log("memory_recall", "Memory Recall") + log("tool_call", "recall") + log("tool_result", "recall") + "event: done\ndata: {}\n\n");
    await waitFor(() => expect(push).toHaveBeenCalledWith("/chat"));
    const rows = JSON.parse(sessionStorage.getItem("hexis-chat-activity-events")!);
    expect(rows.map((row: { id: string }) => row.id)).toEqual(["tool_call", "tool_result"]);
    expect(rows).toHaveLength(2);
  });

  it("API error/failed 留在场景页并显示失败原因", async () => {
    mockScenario(log("memory_recall", "Memory Recall") + 'event: error\ndata: {"message":"Provider stream ended"}\n\nevent: failed\ndata: {}\n\n');
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Provider stream ended"));
    expect(push).not.toHaveBeenCalled();
    expect(sessionStorage.getItem("hexis-chat-activity-events")).toBeNull();
  });

  it.each([
    log("memory_recall", "Memory Recall") + "event: done\ndata: {}\n\n",
    log("tool_call", "recall") + log("tool_result", "recall"),
  ])("缺少真实工具事件或 done 时不标记完成", async (sse) => {
    mockScenario(sse);
    await waitFor(() => expect(screen.getByRole("button", { name: "Run controlled recall" })).toBeEnabled());
    expect(push).not.toHaveBeenCalled();
    expect(sessionStorage.getItem("hexis-chat-activity-events")).toBeNull();
  });
});
