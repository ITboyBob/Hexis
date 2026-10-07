import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TaskSelection } from "./task-selection";

const selection = {
  selection_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  version: 3,
  message: "Continue with the next numbered question and preserve the time limit.",
};
const tasks = [
  {
    task_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    task_title: "准备访谈问题",
    task_state: "working" as const,
    task_session_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    revision: 2,
    pending: [],
  },
  {
    task_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    task_title: "整理访谈记录",
    task_state: "idle" as const,
    task_session_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    revision: 1,
    pending: [],
  },
];

afterEach(() => vi.unstubAllGlobals());

describe("TaskSelection", () => {
  it("confirms only the selected current-source task and closes after the server accepts", async () => {
    const fetchMock = vi.fn(async () => Response.json({ accepted: true }));
    const onHandled = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(
      <TaskSelection
        selection={selection}
        tasks={tasks}
        loading={false}
        unavailable={null}
        onHandled={onHandled}
      />
    );

    expect(screen.getByText(selection.message)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: /整理访谈记录/ }));
    fireEvent.click(screen.getByRole("button", { name: "确认追加" }));

    await waitFor(() => expect(onHandled).toHaveBeenCalledOnce());
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/m5/selections/${selection.selection_id}/confirm`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ version: 3, task_id: tasks[1].task_id }),
      })
    );
  });

  it.each([false, true])("cancels without sending a task id even after choosing a task: %s", async (chooseFirst) => {
    const fetchMock = vi.fn(async () => Response.json({ accepted: true, state: "selection_cancelled" }));
    const onHandled = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(
      <TaskSelection
        selection={selection}
        tasks={tasks}
        loading={false}
        unavailable={null}
        onHandled={onHandled}
      />
    );

    if (chooseFirst) fireEvent.click(screen.getByRole("radio", { name: /准备访谈问题/ }));
    fireEvent.click(screen.getByRole("button", { name: "取消，不追加" }));

    await waitFor(() => expect(onHandled).toHaveBeenCalledOnce());
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/m5/selections/${selection.selection_id}/cancel`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ version: 3 }),
      })
    );
  });

  it("requires an explicit task choice and keeps the modal open after refusal", async () => {
    const fetchMock = vi.fn(async () => Response.json({
      accepted: false,
      error: { code: "SELECTION_VERSION_CONFLICT", message: "选择已过期，请重新选择。" },
    }));
    const onHandled = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(
      <TaskSelection
        selection={selection}
        tasks={tasks}
        loading={false}
        unavailable={null}
        onHandled={onHandled}
      />
    );

    expect(screen.getByRole("button", { name: "确认追加" })).toBeDisabled();
    fireEvent.click(screen.getByRole("radio", { name: /准备访谈问题/ }));
    fireEvent.click(screen.getByRole("button", { name: "确认追加" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("选择已过期");
    expect(onHandled).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "选择要继续的任务" })).toBeInTheDocument();
  });
});
