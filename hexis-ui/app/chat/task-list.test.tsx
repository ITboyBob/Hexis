import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";

import { TaskList as TaskListView } from "./task-list";

type TaskState = Pick<ComponentProps<typeof TaskListView>, "snapshot" | "loading" | "error">;
let taskState: TaskState;
const reload = vi.fn();
function TaskList(props: Omit<ComponentProps<typeof TaskListView>, keyof TaskState | "reload">) {
  return <TaskListView {...props} {...taskState} reload={reload} />;
}

const origin = "11111111-1111-4111-8111-111111111111";

beforeEach(() => {
  reload.mockClear();
  taskState = {
    snapshot: {
      origin_session_id: origin,
      event_cursor: { "m5-tasks": 5 },
      tasks: [
        {
          task_id: "22222222-2222-4222-8222-222222222222",
          task_title: "整理访谈问题",
          task_state: "working",
          task_session_id: "33333333-3333-4333-8333-333333333333",
          revision: 3,
          pending: [
            {
              message_id: "66666666-6666-4666-8666-666666666666",
              receive_order: 2,
              message: "补充竞品比较",
            },
            {
              message_id: "77777777-7777-4777-8777-777777777777",
              receive_order: 4,
              message: "保留来源链接",
            },
          ],
        },
        {
          task_id: "88888888-8888-4888-8888-888888888888",
          task_title: "核对访谈记录",
          task_state: "idle",
          task_session_id: "99999999-9999-4999-8999-999999999999",
          revision: 6,
          pending: [],
        },
      ],
    },
    loading: false,
    error: null,
  };
});

describe("M5 task list", () => {
  it("opens from the composer button and groups authoritative working and idle rows", () => {
    render(<TaskList originSessionId={origin} onOpenTask={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "任务列表" }));

    expect(screen.getByRole("region", { name: "持续任务" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "进行中" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "等待处理" })).toBeInTheDocument();
    expect(screen.getByText("整理访谈问题")).toBeInTheDocument();
    expect(screen.getByText("核对访谈记录")).toBeInTheDocument();
  });

  it("shows identity, textual state, and pending messages in receive order without task controls", () => {
    render(<TaskList originSessionId={origin} onOpenTask={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "任务列表" }));

    const rows = screen.getAllByRole("button", { name: /进入任务/ });
    expect(rows).toHaveLength(2);
    expect(screen.getByText("Working")).toBeInTheDocument();
    expect(screen.getByText("Idle")).toBeInTheDocument();
    expect(screen.getByText("22222222-2222-4222-8222-222222222222")).toBeInTheDocument();
    expect(screen.getByText("补充竞品比较").closest("li")).toHaveAttribute("data-receive-order", "2");
    expect(screen.getByText("保留来源链接").closest("li")).toHaveAttribute("data-receive-order", "4");
    expect(screen.queryByRole("button", { name: /取消|删除|重试|完成任务/ })).toBeNull();
  });

  it("navigates to the selected fixed task session and closes without touching TalkMode", () => {
    const onOpenTask = vi.fn();
    render(<TaskList originSessionId={origin} onOpenTask={onOpenTask} />);
    fireEvent.click(screen.getByRole("button", { name: "任务列表" }));
    fireEvent.click(screen.getByRole("button", { name: /进入任务：整理访谈问题/ }));

    expect(onOpenTask).toHaveBeenCalledWith(
      expect.objectContaining({
        task_id: "22222222-2222-4222-8222-222222222222",
        task_session_id: "33333333-3333-4333-8333-333333333333",
        task_state: "working",
      })
    );
    expect(screen.queryByRole("region", { name: "持续任务" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "任务列表" }));
    fireEvent.click(screen.getByRole("button", { name: "关闭任务列表" }));
    expect(screen.queryByRole("region", { name: "持续任务" })).toBeNull();
  });

  it("reports unavailable data without fabricating an empty list", () => {
    taskState = { snapshot: null, loading: false, error: "状态暂不可用" };
    render(<TaskList originSessionId={origin} onOpenTask={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "任务列表" }));

    expect(screen.getByText("状态暂不可用")).toBeInTheDocument();
    expect(screen.queryByText("暂无任务")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重试任务列表" }));
    expect(reload).toHaveBeenCalledOnce();
  });

  it("shows the M5 pend selection over the task list and keeps the list origin bound", () => {
    render(
      <TaskList
        originSessionId={origin}
        onOpenTask={vi.fn()}
        selection={{
          selection_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          origin_session_id: origin,
          version: 1,
          message: "继续下一道题并保留时间限制。",
        }}
      />
    );

    expect(screen.getByRole("dialog", { name: "选择要继续的任务" })).toBeInTheDocument();
    expect(screen.getByText("继续下一道题并保留时间限制。")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /整理访谈问题/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "任务列表" })).toHaveAttribute("aria-expanded", "false");
  });
});
