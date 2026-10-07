"use client";

import { useState } from "react";

import type { M5Task, M5TaskSelection } from "@/lib/m5-tasks";

type TaskSelectionProps = {
  selection: M5TaskSelection;
  tasks: M5Task[];
  loading: boolean;
  unavailable: string | null;
  onHandled: () => void;
};

async function sendSelectionAction(
  selection: M5TaskSelection,
  action: "confirm" | "cancel",
  taskId?: string
): Promise<{ accepted?: boolean; error?: { code?: string; message?: string } }> {
  const response = await fetch(
    `/api/m5/selections/${encodeURIComponent(selection.selection_id)}/${action}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        version: selection.version,
        ...(action === "confirm" && taskId ? { task_id: taskId } : {}),
      }),
    }
  );
  const body = (await response.json().catch(() => ({}))) as {
    accepted?: boolean;
    error?: { code?: string; message?: string };
  };
  if (!response.ok) {
    throw new Error(body.error?.message || `The selection request failed (${response.status}).`);
  }
  return body;
}

export function TaskSelection({
  selection,
  tasks,
  loading,
  unavailable,
  onHandled,
}: TaskSelectionProps) {
  const [taskId, setTaskId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const finish = async (action: "confirm" | "cancel") => {
    if (busy || (action === "confirm" && !taskId)) return;
    setBusy(true);
    setError(null);
    try {
      const result = await sendSelectionAction(selection, action, taskId ?? undefined);
      if (result.accepted) {
        onHandled();
        return;
      }
      if (result.error?.code === "SELECTION_EXPIRED") onHandled();
      setError(result.error?.message || result.error?.code || "The selection is no longer available.");
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : "The selection request failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/25 p-4">
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="m5-task-selection-title"
        className="max-h-[min(78vh,42rem)] w-full max-w-lg overflow-y-auto rounded-xl border border-[var(--outline)] bg-white p-5 shadow-2xl"
      >
        <h2 id="m5-task-selection-title" className="text-base font-semibold text-[var(--foreground)]">
          选择要继续的任务
        </h2>
        <p className="mt-2 whitespace-pre-wrap break-words text-sm text-[var(--ink-soft)]">
          {selection.message}
        </p>

        {unavailable ? (
          <p role="status" className="mt-4 rounded-md bg-amber-50 p-3 text-sm text-amber-900">
            状态暂不可用：{unavailable}
          </p>
        ) : loading ? (
          <p role="status" className="mt-4 text-sm text-[var(--ink-soft)]">正在读取当前来源的任务…</p>
        ) : tasks.length === 0 ? (
          <p role="status" className="mt-4 text-sm text-[var(--ink-soft)]">当前来源会话没有可选任务。</p>
        ) : (
          <fieldset className="mt-4 space-y-2">
            <legend className="mb-2 text-xs font-semibold text-[var(--ink-soft)]">当前来源的任务</legend>
            {tasks.map((task) => (
              <label
                key={task.task_id}
                className="flex cursor-pointer items-start gap-3 rounded-lg border border-[var(--outline)] p-3 hover:bg-[#f8faf8]"
              >
                <input
                  type="radio"
                  name={`m5-selection-${selection.selection_id}`}
                  value={task.task_id}
                  checked={taskId === task.task_id}
                  onChange={() => setTaskId(task.task_id)}
                  disabled={busy}
                  className="mt-1"
                />
                <span className="min-w-0 flex-1">
                  <span className="block break-all font-mono text-[10px] text-[var(--ink-soft)]">
                    {task.task_id}
                  </span>
                  <span className="mt-1 block text-sm font-medium text-[var(--foreground)]">
                    {task.task_title}
                  </span>
                  <span className="mt-1 block text-xs text-[var(--ink-soft)]">
                    {task.task_state === "working" ? "Working" : "Idle"}
                  </span>
                </span>
              </label>
            ))}
          </fieldset>
        )}

        {error ? <p role="alert" className="mt-3 text-sm text-red-700">{error}</p> : null}
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={() => void finish("cancel")}
            disabled={busy}
            className="rounded-md border border-[var(--outline)] px-3 py-2 text-sm text-[var(--foreground)] disabled:opacity-50"
          >
            取消，不追加
          </button>
          <button
            type="button"
            onClick={() => void finish("confirm")}
            disabled={busy || !taskId || loading || Boolean(unavailable)}
            className="rounded-md bg-[var(--foreground)] px-3 py-2 text-sm font-medium text-white disabled:opacity-40"
          >
            {busy ? "保存中…" : "确认追加"}
          </button>
        </div>
      </section>
    </div>
  );
}
