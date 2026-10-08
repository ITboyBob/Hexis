import { describe, expect, it } from "vitest";

import {
  mergeToolActivityLogs,
  normalizeToolActivityPage,
  type ToolActivityLog,
} from "./tool-activity";

const sessionA = "10800000-0000-4000-8000-000000000001";
const sessionB = "10800000-0000-4000-8000-000000000002";

function row(eventId: string, streamId: string, seq: number, detail: string, sessionId = sessionA) {
  return {
    event_id: eventId,
    event_type: "TOOL_RESULT",
    stream_id: streamId,
    session_id: sessionId,
    seq,
    recorded_at: "2026-10-09T00:00:00.000Z",
    activity: {
      id: eventId,
      kind: "tool_result",
      title: "recall",
      detail,
    },
  };
}

describe("persistent tool Activity normalization", () => {
  it("preserves same-sequence events from independent streams", () => {
    const normalized = normalizeToolActivityPage(
      {
        session_id: sessionA,
        cursor: { stream_a: 3, stream_b: 3 },
        events: [row("event-a", "stream_a", 3, "A"), row("event-b", "stream_b", 3, "B")],
      },
      sessionA,
    );

    expect(normalized.logs.map((event) => event.id)).toEqual(["event-a", "event-b"]);
    expect(normalized.cursor).toEqual({ stream_a: 3, stream_b: 3 });
  });

  it("uses persisted event IDs to deduplicate request and replay pages", () => {
    const current: ToolActivityLog[] = [
      { id: "event-a", category: "tool", title: "recall", detail: "A", ts: 10, session_id: sessionA },
    ];
    const incoming = normalizeToolActivityPage(
      { session_id: sessionA, cursor: { stream_a: 2 }, events: [row("event-a", "stream_a", 1, "duplicate")] },
      sessionA,
    );

    expect(mergeToolActivityLogs(current, incoming.logs, sessionA)).toHaveLength(1);
    expect(mergeToolActivityLogs(current, incoming.logs, sessionA)[0].detail).toBe("A");
  });

  it("consumes accepted results through the cursor without creating a log row", () => {
    const normalized = normalizeToolActivityPage(
      {
        session_id: sessionA,
        cursor: { stream_a: 8 },
        events: [{ ...row("accepted-id", "stream_a", 8, "", sessionA), activity: null }],
      },
      sessionA,
    );

    expect(normalized.logs).toEqual([]);
    expect(normalized.cursor).toEqual({ stream_a: 8 });
  });

  it("discards late pages for a different current session", () => {
    const normalized = normalizeToolActivityPage(
      { session_id: sessionA, cursor: { stream_a: 1 }, events: [row("event-a", "stream_a", 1, "A")] },
      sessionB,
    );

    expect(normalized.logs).toEqual([]);
    expect(normalized.cursor).toEqual({});
  });

  it("keeps event identity and current session when merging out-of-order page results", () => {
    const first: ToolActivityLog[] = [
      { id: "event-2", category: "tool", title: "recall", detail: "2", ts: 20, session_id: sessionA },
    ];
    const second: ToolActivityLog[] = [
      { id: "event-1", category: "tool", title: "recall", detail: "1", ts: 10, session_id: sessionA },
      { id: "foreign", category: "tool", title: "recall", detail: "x", ts: 5, session_id: sessionB },
    ];

    expect(mergeToolActivityLogs(first, second, sessionA).map((event) => event.id)).toEqual(["event-1", "event-2"]);
  });
});
