"use client";

import { useCallback, useEffect, useState } from "react";

export type M5TaskPendingMessage = {
  message_id: string;
  receive_order: number;
  message: string;
};

export type M5Task = {
  task_id: string;
  task_title: string;
  task_state: "working" | "idle";
  task_session_id: string;
  revision: number;
  pending: M5TaskPendingMessage[];
};

export type M5TaskSnapshot = {
  origin_session_id: string;
  event_cursor: { "m5-tasks": number };
  tasks: M5Task[];
};

export type M5TaskEvent = {
  event_id: string;
  event_type: string;
  session_id: string;
  stream_id: string;
  seq: number;
  task_id: string | null;
  payload: Record<string, unknown>;
};

export type M5TaskMergeResult = {
  status: "applied" | "advanced" | "duplicate" | "stale" | "gap" | "wrong_origin" | "invalid";
  snapshot: M5TaskSnapshot;
};

const TASK_STREAM = "m5-tasks";

function validSnapshot(value: unknown, originSessionId: string): value is M5TaskSnapshot {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const snapshot = value as Partial<M5TaskSnapshot>;
  const cursor = snapshot.event_cursor?.[TASK_STREAM];
  return (
    snapshot.origin_session_id === originSessionId &&
    Number.isSafeInteger(cursor) &&
    (cursor as number) >= 0 &&
    Array.isArray(snapshot.tasks) &&
    snapshot.tasks.every(
      (task) =>
        task &&
        typeof task.task_id === "string" &&
        typeof task.task_title === "string" &&
        (task.task_state === "working" || task.task_state === "idle") &&
        typeof task.task_session_id === "string" &&
        Number.isSafeInteger(task.revision) &&
        Array.isArray(task.pending)
    )
  );
}

export async function fetchM5TaskSnapshot(
  originSessionId: string,
  signal?: AbortSignal
): Promise<M5TaskSnapshot> {
  const query = new URLSearchParams({ origin_session_id: originSessionId });
  const response = await fetch(`/api/m5/tasks?${query.toString()}`, {
    cache: "no-store",
    signal,
  });
  const body = (await response.json().catch(() => ({}))) as unknown;
  if (!response.ok) {
    const detail =
      body && typeof body === "object" && "error" in body && typeof body.error === "string"
        ? body.error
        : "Task state is temporarily unavailable.";
    throw new Error(detail);
  }
  if (!validSnapshot(body, originSessionId)) {
    throw new Error("The task snapshot did not match the active source chat.");
  }
  return body;
}

function taskFromEvent(event: M5TaskEvent, originSessionId: string): M5Task | null {
  if (event.event_type !== "m5_task_changed") return null;
  const payload = event.payload;
  if (
    payload.origin_session_id !== originSessionId ||
    payload.task_id !== event.task_id ||
    typeof payload.task_id !== "string" ||
    typeof payload.task_title !== "string" ||
    typeof payload.task_session_id !== "string" ||
    (payload.task_state !== "working" && payload.task_state !== "idle") ||
    !Number.isSafeInteger(payload.revision) ||
    !Array.isArray(payload.pending)
  ) {
    return null;
  }
  const pending = payload.pending;
  if (
    !pending.every(
      (message) =>
        message &&
        typeof message.message_id === "string" &&
        Number.isSafeInteger(message.receive_order) &&
        typeof message.message === "string"
    )
  ) {
    return null;
  }
  return {
    task_id: payload.task_id,
    task_title: payload.task_title,
    task_state: payload.task_state,
    task_session_id: payload.task_session_id,
    revision: payload.revision as number,
    pending: pending as M5TaskPendingMessage[],
  };
}

export function mergeM5TaskEvent(
  snapshot: M5TaskSnapshot,
  event: M5TaskEvent
): M5TaskMergeResult {
  if (
    event.session_id !== snapshot.origin_session_id ||
    event.stream_id !== `m5-tasks:${snapshot.origin_session_id}` ||
    event.payload.origin_session_id !== snapshot.origin_session_id
  ) {
    return { status: "wrong_origin", snapshot };
  }
  if (!Number.isSafeInteger(event.seq) || event.seq < 1) {
    return { status: "invalid", snapshot };
  }
  const cursor = snapshot.event_cursor[TASK_STREAM];
  if (event.seq <= cursor) return { status: "duplicate", snapshot };
  if (event.seq !== cursor + 1) return { status: "gap", snapshot };

  const nextSnapshot: M5TaskSnapshot = {
    ...snapshot,
    event_cursor: { ...snapshot.event_cursor, [TASK_STREAM]: event.seq },
  };
  if (event.event_type !== "m5_task_changed") {
    return { status: "advanced", snapshot: nextSnapshot };
  }
  const task = taskFromEvent(event, snapshot.origin_session_id);
  if (!task) return { status: "invalid", snapshot };
  const existingIndex = snapshot.tasks.findIndex((item) => item.task_id === task.task_id);
  if (existingIndex < 0) {
    return {
      status: "applied",
      snapshot: { ...nextSnapshot, tasks: [task, ...snapshot.tasks] },
    };
  }
  if (snapshot.tasks[existingIndex].revision >= task.revision) {
    return { status: "stale", snapshot: nextSnapshot };
  }
  const tasks = [...snapshot.tasks];
  tasks[existingIndex] = task;
  return { status: "applied", snapshot: { ...nextSnapshot, tasks } };
}

export function useM5Tasks(originSessionId: string | null, enabled: boolean) {
  const [snapshot, setSnapshot] = useState<M5TaskSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  const reload = useCallback(() => setRefresh((value) => value + 1), []);

  useEffect(() => {
    if (!enabled || !originSessionId) return;
    let closed = false;
    let source: EventSource | null = null;
    let fallbackTimer: number | null = null;
    let request: AbortController | null = null;
    let reconnecting = false;

    const closeSource = () => {
      source?.close();
      source = null;
    };
    const clearFallback = () => {
      if (fallbackTimer !== null) window.clearInterval(fallbackTimer);
      fallbackTimer = null;
    };
    const mergeEvent = (message: MessageEvent<string>) => {
      try {
        const event = JSON.parse(message.data) as M5TaskEvent;
        setSnapshot((current) => {
          if (!current) return current;
          const result = mergeM5TaskEvent(current, event);
          if (result.status === "gap" || result.status === "wrong_origin" || result.status === "invalid") {
            window.queueMicrotask(() => {
              closeSource();
              void connect();
            });
            return current;
          }
          return result.snapshot;
        });
      } catch {
        setError("Task updates could not be read. Refresh the task list to retry.");
        closeSource();
      }
    };
    const startFallback = () => {
      if (fallbackTimer !== null || closed) return;
      fallbackTimer = window.setInterval(() => void connect(), 2000);
    };
    const connect = async () => {
      if (closed || reconnecting) return;
      reconnecting = true;
      request?.abort();
      request = new AbortController();
      setLoading(true);
      try {
        const next = await fetchM5TaskSnapshot(originSessionId, request.signal);
        if (closed) return;
        setSnapshot(next);
        setError(null);
        closeSource();
        const query = new URLSearchParams({
          origin_session_id: originSessionId,
          after_seq: String(next.event_cursor[TASK_STREAM]),
        });
        source = new EventSource(`/api/m5/tasks/events?${query.toString()}`);
        source.addEventListener("m5-event", mergeEvent as EventListener);
        source.addEventListener("m5-reset", () => {
          closeSource();
          void connect();
        });
        source.onopen = () => clearFallback();
        source.onerror = () => {
          closeSource();
          startFallback();
        };
      } catch (cause) {
        if (!closed && !(cause instanceof DOMException && cause.name === "AbortError")) {
          setError(cause instanceof Error ? cause.message : "Task state is temporarily unavailable.");
          startFallback();
        }
      } finally {
        reconnecting = false;
        if (!closed) setLoading(false);
      }
    };

    void connect();
    return () => {
      closed = true;
      request?.abort();
      closeSource();
      clearFallback();
    };
  }, [originSessionId, enabled, refresh]);

  return { snapshot, loading, error, reload };
}
