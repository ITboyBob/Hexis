"use client";

import { useEffect, useState } from "react";

import { useM5Tasks, type M5Task, type M5TaskSnapshot } from "@/lib/m5-tasks";

type TaskListProps = {
  originSessionId: string | null;
  currentTaskSessionId?: string | null;
  onOpenTask: (task: M5Task) => void;
  onReturnToOrigin?: () => void;
  onSnapshot?: (snapshot: M5TaskSnapshot) => void;
};

function TaskRow({ task, onOpen }: { task: M5Task; onOpen: () => void }) {
  const working = task.task_state === "working";
  return (
    <div className="rounded-md border border-[var(--outline)] bg-white p-3">
      <button
        type="button"
        aria-label={`进入任务：${task.task_title}（${working ? "working" : "idle"}）`}
        title={`打开固定任务会话 ${task.task_session_id}`}
        onClick={onOpen}
        className="flex w-full items-start justify-between gap-3 text-left"
      >
        <span className="min-w-0 flex-1">
          <span className="block break-all font-mono text-[10px] text-[var(--ink-soft)]">
            {task.task_id}
          </span>
          <span className="mt-1 block text-sm font-medium text-[var(--foreground)]">
            {task.task_title}
          </span>
        </span>
        <span
          className={`shrink-0 rounded-full px-2 py-1 text-[11px] font-medium ${
            working
              ? "bg-[var(--teal)]/10 text-[var(--teal)]"
              : "bg-amber-100 text-amber-800"
          }`}
        >
          {working ? "Working" : "Idle"}
        </span>
      </button>
      {task.pending.length > 0 ? (
        <ol className="mt-2 space-y-1 border-t border-[var(--outline)] pt-2">
          {task.pending.map((message) => (
            <li
              key={message.message_id}
              data-receive-order={message.receive_order}
              className="flex gap-2 text-xs text-[var(--ink-soft)]"
            >
              <span className="shrink-0 tabular-nums">#{message.receive_order}</span>
              <span className="min-w-0 break-words">{message.message}</span>
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}

export function TaskList({
  originSessionId,
  currentTaskSessionId = null,
  onOpenTask,
  onReturnToOrigin,
  onSnapshot,
}: TaskListProps) {
  const [open, setOpen] = useState(false);
  const { snapshot, loading, error, reload } = useM5Tasks(originSessionId, open);
  const usableSnapshot =
    snapshot?.origin_session_id === originSessionId ? snapshot : null;

  useEffect(() => {
    if (usableSnapshot) onSnapshot?.(usableSnapshot);
  }, [usableSnapshot, onSnapshot]);

  return (
    <div className="relative flex-none">
      <button
        type="button"
        aria-label="任务列表"
        aria-expanded={open}
        aria-controls="m5-task-list-panel"
        disabled={!originSessionId}
        onClick={() => setOpen((value) => !value)}
        className="h-10 rounded-md px-3 text-xs font-medium text-[var(--ink-soft)] hover:bg-[var(--outline)] hover:text-[var(--foreground)] disabled:opacity-40"
      >
        任务列表
      </button>
      {open ? (
        <section
          id="m5-task-list-panel"
          role="region"
          aria-label="持续任务"
          className="absolute bottom-12 right-0 z-30 max-h-[min(70vh,34rem)] w-[min(22rem,calc(100vw-2rem))] overflow-y-auto rounded-xl border border-[var(--outline)] bg-[#f8faf8] p-3 shadow-xl"
        >
          <header className="mb-3 flex items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-[var(--foreground)]">持续任务</h2>
            <div className="flex items-center gap-1">
              {currentTaskSessionId && onReturnToOrigin ? (
                <button
                  type="button"
                  onClick={() => {
                    setOpen(false);
                    onReturnToOrigin();
                  }}
                  className="rounded-md px-2 py-1 text-xs text-[var(--teal)] hover:bg-[var(--teal)]/10"
                >
                  返回来源会话
                </button>
              ) : null}
              {error ? (
                <button
                  type="button"
                  aria-label="重试任务列表"
                  onClick={reload}
                  className="rounded-md px-2 py-1 text-xs text-[var(--teal)] hover:bg-[var(--teal)]/10"
                >
                  重试
                </button>
              ) : null}
              <button
                type="button"
                aria-label="关闭任务列表"
                onClick={() => setOpen(false)}
                className="rounded-md px-2 py-1 text-xs text-[var(--ink-soft)] hover:bg-[var(--outline)]"
              >
                关闭
              </button>
            </div>
          </header>

          {error ? (
            <p role="status" className="rounded-md bg-amber-50 p-3 text-xs text-amber-900">
              {error.includes("状态暂不可用") ? error : `状态暂不可用：${error}`}
            </p>
          ) : loading && !usableSnapshot ? (
            <p role="status" className="p-3 text-xs text-[var(--ink-soft)]">
              正在读取任务状态…
            </p>
          ) : usableSnapshot ? (
            <div className="space-y-4">
              {([
                ["working", "进行中"],
                ["idle", "等待处理"],
              ] as const).map(([state, heading]) => {
                const tasks = usableSnapshot.tasks.filter(
                  (task) => task.task_state === state
                );
                return (
                  <section key={state} aria-label={heading}>
                    <h3 className="mb-2 text-xs font-semibold text-[var(--ink-soft)]">
                      {heading}
                    </h3>
                    {tasks.length > 0 ? (
                      <div className="space-y-2">
                        {tasks.map((task) => (
                          <TaskRow
                            key={task.task_id}
                            task={task}
                            onOpen={() => {
                              setOpen(false);
                              onOpenTask(task);
                            }}
                          />
                        ))}
                      </div>
                    ) : (
                      <p className="text-xs text-[var(--ink-soft)]">暂无任务</p>
                    )}
                  </section>
                );
              })}
            </div>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}
