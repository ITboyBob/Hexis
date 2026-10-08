export type ToolActivityCursor = Record<string, number>;

export type ToolActivityLog = {
  id: string;
  category: "tool";
  title: string;
  detail: string;
  raw?: unknown;
  ts: number;
  session_id: string;
};

export type NormalizedToolActivityPage = {
  cursor: ToolActivityCursor;
  logs: ToolActivityLog[];
  has_more: boolean;
};

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function cursorFrom(value: unknown): ToolActivityCursor {
  const record = asRecord(value);
  if (!record) return {};
  const cursor: ToolActivityCursor = {};
  for (const [streamId, seq] of Object.entries(record)) {
    if (streamId.trim() && Number.isInteger(seq) && typeof seq === "number" && seq >= 0) {
      cursor[streamId] = seq;
    }
  }
  return cursor;
}

export function normalizeToolActivityPage(
  value: unknown,
  activeSessionId: string,
): NormalizedToolActivityPage {
  const page = asRecord(value);
  if (!page || page.session_id !== activeSessionId) {
    return { cursor: {}, logs: [], has_more: false };
  }

  const cursor = cursorFrom(page.cursor);
  const rows = Array.isArray(page.events) ? page.events : [];
  const logs: ToolActivityLog[] = [];
  for (const value of rows) {
    const row = asRecord(value);
    if (!row || row.session_id !== activeSessionId) continue;
    const activity = asRecord(row.activity);
    if (!activity || typeof row.event_id !== "string") continue;
    if (activity.kind !== "tool_call" && activity.kind !== "tool_result") continue;
    const title = typeof activity.title === "string" ? activity.title : "Tool";
    const detail = typeof activity.detail === "string" ? activity.detail : "";
    const recordedAt = typeof row.recorded_at === "string" ? Date.parse(row.recorded_at) : NaN;
    const eventTime = typeof row.ts === "string" ? Date.parse(row.ts) : NaN;
    const ts = Number.isFinite(recordedAt) ? recordedAt : Number.isFinite(eventTime) ? eventTime : Date.now();
    logs.push({
      id: row.event_id,
      category: "tool",
      title,
      detail,
      raw: activity,
      ts,
      session_id: activeSessionId,
    });
  }
  return { cursor, logs, has_more: page.has_more === true };
}

export function mergeToolActivityLogs(
  current: ToolActivityLog[],
  incoming: ToolActivityLog[],
  activeSessionId: string,
): ToolActivityLog[] {
  const merged = new Map(current.map((event) => [event.id, event]));
  for (const event of incoming) {
    if (event.session_id === activeSessionId && !merged.has(event.id)) {
      merged.set(event.id, event);
    }
  }
  return [...merged.values()].sort((left, right) => left.ts - right.ts);
}
