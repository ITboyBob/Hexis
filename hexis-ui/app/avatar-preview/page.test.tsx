import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

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

  it("真实POST成功后以服务端身份和来源事实更新标签并显示实际计数", async () => {
    let exported: Blob | undefined;
    const createObjectURL = vi.fn((blob: Blob) => { exported = blob; return "blob:service-evidence"; });
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const fetcher = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      const request = init?.body ? JSON.parse(String(init.body)) : JSON.parse(String(fetcher.mock.calls[0][1]?.body));
      return Response.json({ identity: request.identity, status: "valid", label: "curious", raw_business_value: "curious",
        wordset_version: "v1", emotion_source: "real", context_source: request.context_source,
        logical_call_count: 1, transport_attempts: null,
        visual_window: { status: "accepted", owner: "service", decision_seq: 1 } });
    });
    vi.stubGlobal("fetch", fetcher);
    await renderPage();
    fireEvent.change(screen.getByLabelText("消息文本"), { target: { value: "这次请求只发一次" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "使用合成fixture评估（真实服务）" })); });
    await waitFor(() => expect(screen.getByTestId("emotion-current")).toHaveTextContent("curious"));
    expect(fetcher).toHaveBeenCalledOnce();
    expect(fetcher.mock.calls.filter((call) => call[1]?.method === "POST")).toHaveLength(1);
    expect(fetcher.mock.calls[0][0]).toBe("/api/avatar-preview/evaluate");
    expect(screen.getByTestId("visual-qualification")).toHaveTextContent("accepted");
    expect(screen.getByTestId("emotion-origin")).toHaveTextContent("原输入来源 fixture；逻辑调用 1；传输尝试 未知");
    const acceptedReason = screen.getByTestId("evaluation-status").textContent;
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "查询原评估（不重放）" })); });
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("emotion-current")).toHaveTextContent("curious");
    expect(screen.getByTestId("evaluation-status").textContent).toBe(acceptedReason);
    expect(screen.getByTestId("diagnostics")).toHaveTextContent("原结果已接纳；此查询仅诊断");
    fireEvent.click(screen.getByRole("button", { name: "导出来源轨迹" }));
    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(click).toHaveBeenCalledOnce();
    const exportedText = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(reader.error ?? new Error("导出Blob读取失败"));
      reader.onload = () => resolve(String(reader.result));
      reader.readAsText(exported!);
    });
    const evidence = JSON.parse(exportedText);
    expect(evidence.source_mode).toContain("真实情绪服务上下文:fixture");
    expect(evidence.service_results).toEqual(expect.arrayContaining([
      expect.objectContaining({
        context_source: "fixture",
        logical_call_count: 1,
        transport_attempts: null,
        visual_window: { status: "accepted", owner: "service", decision_seq: 1 },
      }),
    ]));
  });

  it("POST断线与404查询保持unknown/open，只执行一次POST", async () => {
    const fetcher = vi.fn().mockRejectedValueOnce(new TypeError("network down"))
      .mockResolvedValueOnce(Response.json({ status: "unknown", message: "当前服务进程未找到；不会自动重新调用模型" }, { status: 404 }));
    vi.stubGlobal("fetch", fetcher);
    await renderPage();
    fireEvent.change(screen.getByLabelText("消息文本"), { target: { value: "断线输入" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "使用合成fixture评估（真实服务）" })); });
    expect(screen.getByTestId("visual-qualification")).toHaveTextContent("open");
    expect(screen.getByTestId("active-tasks")).toHaveTextContent("状态未知");
    expect(screen.getByRole("alert")).toHaveTextContent("不要重新发送");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "查询原评估（不重放）" })); });
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher.mock.calls[0][0]).toBe("/api/avatar-preview/evaluate");
    expect(String(fetcher.mock.calls[1][0])).toContain("/evaluations/");
    expect(screen.getByTestId("visual-qualification")).toHaveTextContent("open");
    expect(screen.getByTestId("active-tasks")).toHaveTextContent("状态未知");
    expect(screen.getByTestId("service-evidence")).toHaveTextContent("unknown");
  });

  it("真实服务owner不受本地30秒超时控制，pending查询沿用原来源", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const request = init?.body ? JSON.parse(String(init.body)) : undefined;
      return Response.json({ identity: request?.identity ?? JSON.parse(String(fetcher.mock.calls[0][1]?.body)).identity,
        status: "pending", wordset_version: "v1", emotion_source: "real", context_source: "fixture",
        visual_owner: "service", visual_window: { status: "open", owner: "service", decision_seq: 0 } });
    });
    vi.stubGlobal("fetch", fetcher);
    await renderPage();
    fireEvent.change(screen.getByLabelText("消息文本"), { target: { value: "长评估" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "使用合成fixture评估（真实服务）" })); });
    fireEvent.change(screen.getByLabelText("上下文来源"), { target: { value: "db" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(31000); });
    expect(screen.getByTestId("visual-qualification")).toHaveTextContent("open");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "查询原评估（不重放）" })); });
    expect(screen.getByTestId("visual-qualification")).toHaveTextContent("open");
    expect(screen.getByTestId("active-tasks")).toHaveTextContent("运行中");
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("超时闭窗后reset、角色重建和同身份查询valid均不能复活", async () => {
    let identity: Record<string, unknown> | undefined;
    const timeout = () => Response.json({ identity, status: "error", wordset_version: "v1", emotion_source: "real", context_source: "fixture",
      error_code: "ModelEvaluationTimeout", stage: "model", logical_call_count: 1, transport_attempts: null,
      message: "模型评估超时", visual_window: { status: "closed", owner: "service", decision_seq: 1, reason: "model_timeout" } });
    const fetcher = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (init?.body) { identity = JSON.parse(String(init.body)).identity; return timeout(); }
      return Response.json({ identity, status: "valid", label: "happy", raw_business_value: "happy", wordset_version: "v1", emotion_source: "real",
        context_source: "fixture", logical_call_count: 1, transport_attempts: null,
        visual_window: { status: "accepted", owner: "service", decision_seq: 1 } });
    });
    vi.stubGlobal("fetch", fetcher);
    await renderPage();
    fireEvent.change(screen.getByLabelText("消息文本"), { target: { value: "超时输入" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "使用合成fixture评估（真实服务）" })); });
    expect(screen.getByTestId("visual-qualification")).toHaveTextContent("closed");
    fireEvent.click(screen.getByRole("button", { name: "重置视觉（保留业务事实）" }));
    fireEvent.click(screen.getByRole("button", { name: "重建角色（保留输入与资格）" }));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "查询原评估（不重放）" })); });
    expect(screen.getByTestId("visual-qualification")).toHaveTextContent("closed");
    expect(screen.getByTestId("emotion-current")).toHaveTextContent("未评估");
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("旧请求晚到不会覆盖新输入，但其同会话活动会结束", async () => {
    const pending: Array<(response: Response) => void> = [];
    const requests: Array<{ identity: Record<string, unknown>; context_source: string }> = [];
    const fetcher = vi.fn((_url: RequestInfo | URL, init?: RequestInit) => {
      const request = JSON.parse(String(init?.body)); requests.push(request);
      return new Promise<Response>((resolve) => pending.push(resolve));
    });
    vi.stubGlobal("fetch", fetcher);
    await renderPage();
    fireEvent.change(screen.getByLabelText("消息文本"), { target: { value: "并发输入" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "使用合成fixture评估（真实服务）" })); });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "使用合成fixture评估（真实服务）" })); });
    const valid = (request: typeof requests[number], label: string) => Response.json({ identity: request.identity, status: "valid", label,
      raw_business_value: label, wordset_version: "v1", emotion_source: "real", context_source: "fixture", logical_call_count: 1,
      transport_attempts: null, visual_window: { status: "accepted", owner: "service", decision_seq: 1 } });
    await act(async () => { pending[0](valid(requests[0], "happy")); await Promise.resolve(); });
    expect(screen.getByTestId("emotion-current")).toHaveTextContent("未评估");
    expect(screen.getByTestId("active-tasks")).not.toHaveTextContent(JSON.stringify(Object.values(requests[0].identity)));
    expect(screen.getByTestId("active-tasks")).toHaveTextContent("运行中");
    await act(async () => { pending[1](valid(requests[1], "curious")); await Promise.resolve(); });
    expect(screen.getByTestId("emotion-current")).toHaveTextContent("curious");
  });

  it("停止后换音频保留明确音量/静音设置并开启新播放epoch", async () => {
    const players: Array<{ volume: number; muted: boolean; src: string; currentTime: number; play: () => Promise<void>; pause: () => void; addEventListener: EventTarget["addEventListener"]; removeEventListener: EventTarget["removeEventListener"]; removeAttribute: (name: string) => void; load: () => void }> = [];
    class MockAudio extends EventTarget {
      volume = 1; muted = false; src = ""; currentTime = 0;
      play = async () => undefined;
      pause = () => { this.dispatchEvent(new Event("pause")); };
      removeAttribute = (name: string) => { if (name === "src") this.src = ""; };
      load = () => undefined;
      constructor() { super(); players.push(this); }
    }
    const makeContext = () => ({ destination: {}, resume: async () => undefined, close: async () => undefined,
      createAnalyser: () => ({ fftSize: 32, connect: () => undefined, getByteTimeDomainData: () => undefined }),
      createMediaElementSource: () => ({ connect: () => undefined }) });
    const NativeURL = URL;
    vi.stubGlobal("Audio", MockAudio);
    vi.stubGlobal("AudioContext", vi.fn(makeContext));
    vi.stubGlobal("URL", Object.assign(class URLMock extends NativeURL {}, { createObjectURL: vi.fn(() => "blob:local"), revokeObjectURL: vi.fn() }));
    await renderPage();
    const picker = screen.getByLabelText("选择本地音频文件");
    fireEvent.change(picker, { target: { files: [new File(["one"], "one.wav", { type: "audio/wav" })] } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "播放所选本地音频" })); });
    fireEvent.change(screen.getByLabelText("播放音量"), { target: { value: "0.25" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "静音实际播放" }));
    expect(players[0]).toMatchObject({ volume: 0.25, muted: true });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "停止本地播放" })); await Promise.resolve(); });
    fireEvent.change(picker, { target: { files: [new File(["two"], "two.wav", { type: "audio/wav" })] } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "播放所选本地音频" })); });
    expect(players).toHaveLength(2);
    expect(players[1]).toMatchObject({ volume: 0.25, muted: true });
    expect(screen.getByTestId("playback-state")).toHaveTextContent("epoch 2");
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
    const oldCharacter = { setComposition: vi.fn(), getDiagnostics: vi.fn(), resetVisual: vi.fn(), stopVisualEffect: vi.fn(), destroy: vi.fn(), bounceOnce: vi.fn(), burstOnce: vi.fn() };
    await act(async () => { vi.mocked(Avatar).mock.calls.at(-1)?.[0]?.onReady?.(oldCharacter); });
    fireEvent.change(screen.getByLabelText("固定场景"), { target: { value: "C07" } });
    const next = screen.getByRole("button", { name: "发送下一条场景事件" });
    await act(async () => { fireEvent.click(next); }); // 排定绑定旧视觉代次的回调
    fireEvent.click(screen.getByRole("button", { name: "重建角色（保留输入与资格）" }));
    const newCharacter = { setComposition: vi.fn(), getDiagnostics: vi.fn(), resetVisual: vi.fn(), stopVisualEffect: vi.fn(), destroy: vi.fn(), bounceOnce: vi.fn(), burstOnce: vi.fn() };
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
    const character = { setComposition: vi.fn(), getDiagnostics: vi.fn(), resetVisual: vi.fn(), stopVisualEffect: vi.fn(), destroy: vi.fn(), bounceOnce: vi.fn(), burstOnce: vi.fn() };
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
