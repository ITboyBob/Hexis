import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Avatar } from "./avatar";
import Page from "./page";

vi.mock("./avatar", async () => {
  return { Avatar: vi.fn((props: { onReady?: (value: unknown) => void } = {}) => {
    void props;
    return <div data-testid="avatar" />;
  }) };
});

async function renderPage() {
  const view = render(<Page />);
  await act(async () => { await Promise.resolve(); });
  return view;
}

describe("独立 Avatar 预览页面", () => {
  beforeEach(() => vi.mocked(Avatar).mockClear());

  it("区分未评估与合法neutral，并展示全部14项中文语义标签", async () => {
    await renderPage();
    expect(screen.getByTestId("emotion-current")).toHaveTextContent("未评估");
    const buttons = screen.getAllByRole("button", { name: /^手工标签 / });
    expect(buttons).toHaveLength(14);
    expect(screen.getByRole("button", { name: /neutral.*本条消息未引起明显主导情绪反应/ })).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /手工标签 neutral/ })); });
    expect(screen.getByTestId("emotion-current")).toHaveTextContent("neutral");
    expect(screen.getByTestId("emotion-source")).toHaveTextContent("手工标签");
  });

  it("错误JSON显示修复建议且可恢复；编辑器不能伪装真实来源", async () => {
    await renderPage();
    const editor = screen.getByLabelText("事件JSON");
    fireEvent.change(editor, { target: { value: "{" } });
    fireEvent.click(screen.getByRole("button", { name: "注入手工或模拟事件" }));
    expect(screen.getByRole("alert")).toHaveTextContent("JSON格式无效");
    fireEvent.change(editor, { target: { value: JSON.stringify({ event_id: "x", session_id: "x", stream_id: "s", producer_seq: 0, payload: { type: "task", task: { activity_id: "a", task_id: null, kind: "tool", foreground: false, phase: "running", source: "real" } } }) } });
    fireEvent.click(screen.getByRole("button", { name: "注入手工或模拟事件" }));
    expect(screen.getByRole("alert")).toHaveTextContent("不能伪装真实来源");
    fireEvent.change(editor, { target: { value: "{}" } });
    fireEvent.click(screen.getByRole("button", { name: "注入手工或模拟事件" }));
    expect(screen.getByRole("alert")).toHaveTextContent("事件内容无效");
  });

  it("固定场景可逐条执行，两工具仅结束一个，reset保留业务事实而重开清空", async () => {
    await renderPage();
    fireEvent.change(screen.getByLabelText("固定场景"), { target: { value: "C02" } });
    const next = screen.getByRole("button", { name: "发送下一条场景事件" });
    for (let i = 0; i < 3; i += 1) {
      await act(async () => { fireEvent.click(next); await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
    expect(screen.getByTestId("active-tasks")).toHaveTextContent("tool-b");
    expect(screen.getByTestId("active-tasks")).not.toHaveTextContent("tool-a");
    fireEvent.click(screen.getByRole("button", { name: /手工标签 happy/ }));
    fireEvent.click(screen.getByRole("button", { name: /重置视觉/ }));
    expect(screen.getByTestId("emotion-current")).toHaveTextContent("happy");
    expect(screen.getByTestId("active-tasks")).toHaveTextContent("tool-b");
    fireEvent.click(screen.getByRole("button", { name: "显式重开预览会话" }));
    expect(screen.getByTestId("emotion-current")).toHaveTextContent("未评估");
    expect(screen.getByTestId("active-tasks")).toHaveTextContent("无活动");
  });

  it("导出可读完整轨迹并展示资格、来源和坏事件诊断", async () => {
    const createObjectURL = vi.fn(() => "blob:preview");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    await renderPage();
    fireEvent.click(screen.getByRole("button", { name: /手工标签 curious/ }));
    expect(screen.getByTestId("visual-qualification")).toHaveTextContent("accepted");
    expect(screen.getByTestId("diagnostics")).toHaveTextContent("手工");
    fireEvent.click(screen.getByRole("button", { name: "导出来源轨迹" }));
    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(click).toHaveBeenCalledOnce();
    vi.unstubAllGlobals();
    click.mockRestore();
  });

  it("C11仅展示显式服务配置边界，不自动发请求", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    await renderPage();
    fireEvent.change(screen.getByLabelText("固定场景"), { target: { value: "C11" } });
    expect(screen.getByText(/需显式配置预览服务/)).toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it("C12用首开期限裁定本地资格，截止后的合法label只记诊断", async () => {
    vi.useFakeTimers();
    await renderPage();
    fireEvent.change(screen.getByLabelText("固定场景"), { target: { value: "C12" } });
    const next = screen.getByRole("button", { name: "发送下一条场景事件" });
    await act(async () => { fireEvent.click(next); }); // 首次开窗，期限1ms
    await act(async () => { fireEvent.click(next); }); // 正式接纳身份
    await act(async () => { fireEvent.click(next); await vi.advanceTimersByTimeAsync(3); }); // 本地截止宿主关闭
    expect(screen.getByTestId("visual-qualification")).toHaveTextContent("closed");
    expect(screen.getByTestId("evaluation-status")).toHaveTextContent("情绪评估期限已到");
    await act(async () => { fireEvent.click(next); }); // 截止后valid只进入诊断
    expect(screen.getByTestId("emotion-current")).toHaveTextContent("未评估");
    expect(screen.getByTestId("diagnostics")).toHaveTextContent("happy");
    expect(screen.getByTestId("diagnostics")).toHaveTextContent("model_timeout");
    vi.useRealTimers();
  });

  it("角色渲染器重建保留已关闭资格，之后的pending与valid仍只作诊断", async () => {
    vi.useFakeTimers();
    await renderPage();
    fireEvent.change(screen.getByLabelText("固定场景"), { target: { value: "C12" } });
    const next = screen.getByRole("button", { name: "发送下一条场景事件" });
    await act(async () => { fireEvent.click(next); });
    await act(async () => { fireEvent.click(next); });
    await act(async () => { fireEvent.click(next); await vi.advanceTimersByTimeAsync(3); });
    const session = screen.getByTestId("session-id").textContent!.replace("会话：", "");
    const currentText = screen.getByText(/当前身份：/).textContent ?? "";
    const match = currentText.match(/当前身份：(.+) \/ 输入序号(\d+) \/ 修订(\d+)/);
    expect(match).not.toBeNull();
    const identity = { session_id: session, input_turn_id: match![1], input_order: Number(match![2]), stimulus_revision: Number(match![3]) };
    fireEvent.click(screen.getByLabelText("完整验收录制（上限10000条）"));
    fireEvent.change(screen.getByLabelText("播放微动限幅"), { target: { value: "12" } });
    fireEvent.change(screen.getByLabelText("效果最小间隔（毫秒）"), { target: { value: "20" } });
    const focusEvent = { event_id: "focus-after-close", session_id: session, stream_id: "focus", producer_seq: 1,
      payload: { type: "task", task: { activity_id: "focus-task", task_id: "focus-task", kind: "tool",
        foreground: true, phase: "running", source: "simulated" } } };
    fireEvent.change(screen.getByLabelText("事件JSON"), { target: { value: JSON.stringify(focusEvent) } });
    fireEvent.click(screen.getByRole("button", { name: "注入手工或模拟事件" }));
    expect(screen.getByText(/主要注意力：focus-task/)).toBeInTheDocument();
    expect(screen.getByTestId("session-id")).toHaveTextContent(session);
    expect(screen.getByTestId("visual-qualification")).toHaveTextContent("closed");
    const callsBefore = vi.mocked(Avatar).mock.calls.length;
    const generationBefore = JSON.parse(document.querySelector("pre")!.textContent!).projection.visual_generation;
    fireEvent.click(screen.getByRole("button", { name: "重建角色（保留输入与资格）" }));
    expect(vi.mocked(Avatar).mock.calls.length).toBeGreaterThan(callsBefore);
    expect(screen.getByTestId("visual-qualification")).toHaveTextContent("closed");
    expect(screen.getByTestId("session-id")).toHaveTextContent(session);
    expect(JSON.parse(document.querySelector("pre")!.textContent!).projection.visual_generation).toBe(generationBefore + 1);
    const editor = screen.getByLabelText("事件JSON");
    const send = (eventId: string, seq: number, status: "pending" | "valid") => {
      fireEvent.change(editor, { target: { value: JSON.stringify({ event_id: eventId, session_id: session,
        stream_id: "after-remount", producer_seq: seq,
        payload: { type: "emotion", identity, wordset_version: "v1", status, label: status === "valid" ? "happy" : undefined,
          source: "simulated", reason: "截止后诊断" } }) } });
      fireEvent.click(screen.getByRole("button", { name: "注入手工或模拟事件" }));
    };
    send("late-pending", 1, "pending");
    send("late-valid", 2, "valid");
    expect(screen.getByTestId("visual-qualification")).toHaveTextContent("closed");
    expect(screen.getByTestId("emotion-current")).toHaveTextContent("未评估");
    expect(screen.getByTestId("diagnostics")).toHaveTextContent("late-pending");
    expect(screen.getByTestId("diagnostics")).toHaveTextContent("late-valid");
    vi.useRealTimers();
  });

  it("重建角色后已排定的旧视觉回调不能触碰新渲染器", async () => {
    await renderPage();
    const oldCharacter = { setComposition: vi.fn(), resetVisual: vi.fn(), stopVisualEffect: vi.fn(), destroy: vi.fn(), bounceOnce: vi.fn(), burstOnce: vi.fn() };
    await act(async () => { vi.mocked(Avatar).mock.calls.at(-1)?.[0]?.onReady?.(oldCharacter); });
    fireEvent.change(screen.getByLabelText("固定场景"), { target: { value: "C07" } });
    const next = screen.getByRole("button", { name: "发送下一条场景事件" });
    await act(async () => { fireEvent.click(next); }); // 排定绑定旧视觉代次的回调
    fireEvent.click(screen.getByRole("button", { name: "重建角色（保留输入与资格）" }));
    const newCharacter = { setComposition: vi.fn(), resetVisual: vi.fn(), stopVisualEffect: vi.fn(), destroy: vi.fn(), bounceOnce: vi.fn(), burstOnce: vi.fn() };
    await act(async () => { vi.mocked(Avatar).mock.calls.at(-1)?.[0]?.onReady?.(newCharacter); });
    await act(async () => { fireEvent.click(next); }); // 再次推进代次
    const resetCalls = newCharacter.resetVisual.mock.calls.length;
    await act(async () => { fireEvent.click(next); }); // 执行之前排定的旧回调
    expect(newCharacter.resetVisual).toHaveBeenCalledTimes(resetCalls);
  });

  it("手工事件入口先绑定新身份，再由宿主接受同身份合法结果并保留原序号", async () => {
    await renderPage();
    const session = screen.getByTestId("session-id").textContent!.replace("会话：", "");
    const editor = screen.getByLabelText("事件JSON");
    const send = (event: unknown) => {
      fireEvent.change(editor, { target: { value: JSON.stringify(event) } });
      fireEvent.click(screen.getByRole("button", { name: "注入手工或模拟事件" }));
    };
    const identity = { session_id: session, input_turn_id: "editor-turn", input_order: 1, stimulus_revision: 1 };
    send({ event_id: "editor-input", session_id: session, stream_id: "editor", producer_seq: 40,
      payload: { type: "input_committed", identity } });
    expect(screen.getByTestId("visual-qualification")).toHaveTextContent("open");
    send({ event_id: "editor-result", session_id: session, stream_id: "editor", producer_seq: 41,
      payload: { type: "emotion", identity, wordset_version: "v1", status: "valid", label: "sad", source: "simulated" } });
    expect(screen.getByTestId("emotion-current")).toHaveTextContent("sad");
    expect(screen.getByTestId("visual-qualification")).toHaveTextContent("accepted");
    expect(screen.getByTestId("diagnostics")).toHaveTextContent("editor-result editor#41");
  });

  it("错词表valid不消耗本地裁定，合法词表同身份结果随后仍可接纳", async () => {
    await renderPage();
    const session = screen.getByTestId("session-id").textContent!.replace("会话：", "");
    const editor = screen.getByLabelText("事件JSON");
    const identity = { session_id: session, input_turn_id: "wordset-turn", input_order: 1, stimulus_revision: 1 };
    const send = (id: string, seq: number, payload: unknown) => {
      fireEvent.change(editor, { target: { value: JSON.stringify({ event_id: id, session_id: session,
        stream_id: "wordset-editor", producer_seq: seq, payload }) } });
      fireEvent.click(screen.getByRole("button", { name: "注入手工或模拟事件" }));
    };
    send("input", 1, { type: "input_committed", identity });
    send("wrong-wordset", 2, { type: "emotion", identity, wordset_version: "v0", status: "valid", label: "happy", source: "simulated" });
    expect(screen.getByTestId("visual-qualification")).toHaveTextContent("open");
    expect(screen.getByTestId("emotion-current")).toHaveTextContent("未评估");
    send("right-wordset", 3, { type: "emotion", identity, wordset_version: "v1", status: "valid", label: "curious", source: "simulated" });
    expect(screen.getByTestId("visual-qualification")).toHaveTextContent("accepted");
    expect(screen.getByTestId("emotion-current")).toHaveTextContent("curious");
  });

  it("报告旧结果拒绝并保持已有focus，只有原活动结束后才切换", async () => {
    await renderPage();
    const session = screen.getByTestId("session-id").textContent!.replace("会话：", "");
    const editor = screen.getByLabelText("事件JSON");
    const send = (id: string, seq: number, payload: unknown) => {
      fireEvent.change(editor, { target: { value: JSON.stringify({ event_id: id, session_id: session,
        stream_id: "focus-editor", producer_seq: seq, payload }) } });
      fireEvent.click(screen.getByRole("button", { name: "注入手工或模拟事件" }));
    };
    const tool = (activity_id: string, phase: "running" | "completed") => ({ type: "task", task: {
      activity_id, task_id: activity_id, kind: "tool", foreground: true, phase, source: "simulated",
    } });
    send("z", 1, tool("z-task", "running"));
    expect(screen.getByText(/主要注意力：z-task/)).toBeInTheDocument();
    send("a", 2, tool("a-task", "running"));
    expect(screen.getByText(/主要注意力：z-task/)).toBeInTheDocument();
    send("z-end", 3, tool("z-task", "completed"));
    expect(screen.getByText(/主要注意力：a-task/)).toBeInTheDocument();
  });

  it("C06的效果队列不伪装成业务task，结束A后显示B继续", async () => {
    await renderPage();
    const character = { setComposition: vi.fn(), resetVisual: vi.fn(), stopVisualEffect: vi.fn(), destroy: vi.fn(), bounceOnce: vi.fn(), burstOnce: vi.fn() };
    await act(async () => { vi.mocked(Avatar).mock.calls.at(-1)?.[0]?.onReady?.(character); });
    fireEvent.change(screen.getByLabelText("固定场景"), { target: { value: "C06" } });
    const next = screen.getByRole("button", { name: "发送下一条场景事件" });
    for (let i = 0; i < 3; i += 1) await act(async () => { fireEvent.click(next); await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(screen.getByTestId("active-tasks")).toHaveTextContent("无活动");
    expect(screen.getByTestId("active-tasks")).not.toHaveTextContent("effect-a");
    expect(screen.getByTestId("visual-effects")).toHaveTextContent("effect-b");
  });

  it("C08销毁后仍可执行一次已排定的迟到回调步骤，业务状态不复活", async () => {
    await renderPage();
    fireEvent.change(screen.getByLabelText("固定场景"), { target: { value: "C08" } });
    const next = screen.getByRole("button", { name: "发送下一条场景事件" });
    for (let i = 0; i < 4; i += 1) await act(async () => { fireEvent.click(next); await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(screen.getByTestId("emotion-current")).toHaveTextContent("未评估");
    expect(screen.getByTestId("diagnostics")).toHaveTextContent("预览已销毁");
    fireEvent.click(screen.getByRole("button", { name: "显式重开预览会话" }));
    expect(screen.getByTestId("emotion-current")).toHaveTextContent("未评估");
  });
});
