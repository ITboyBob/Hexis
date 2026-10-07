import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  mergeM5TaskEvent,
  useM5Tasks,
  type M5TaskEvent,
  type M5TaskSnapshot,
} from "./m5-tasks";

const snapshot: M5TaskSnapshot = {
  origin_session_id: "11111111-1111-4111-8111-111111111111",
  event_cursor: { "m5-tasks": 4 },
  tasks: [
    {
      task_id: "22222222-2222-4222-8222-222222222222",
      task_title: "整理访谈问题",
      task_state: "working",
      task_session_id: "33333333-3333-4333-8333-333333333333",
      revision: 8,
      pending: [],
    },
  ],
};

function event(overrides: Partial<M5TaskEvent> = {}): M5TaskEvent {
  return {
    event_id: "44444444-4444-4444-8444-444444444444",
    event_type: "m5_task_changed",
    session_id: snapshot.origin_session_id,
    stream_id: `m5-tasks:${snapshot.origin_session_id}`,
    seq: 5,
    task_id: snapshot.tasks[0].task_id,
    payload: {
      origin_session_id: snapshot.origin_session_id,
      task_id: snapshot.tasks[0].task_id,
      task_title: "整理访谈问题",
      task_state: "idle",
      task_session_id: snapshot.tasks[0].task_session_id,
      revision: 10,
      pending: [],
    },
    ...overrides,
  };
}

describe("M5 task event projection", () => {
  it("merges by origin and task revision while tracking the independent stream cursor", () => {
    const result = mergeM5TaskEvent(snapshot, event());

    expect(result.status).toBe("applied");
    expect(result.snapshot.event_cursor["m5-tasks"]).toBe(5);
    expect(result.snapshot.tasks[0].revision).toBe(10);
    expect(result.snapshot.tasks[0].task_state).toBe("idle");
  });

  it("accepts revision jumps because internal claims can occur in one transaction", () => {
    const result = mergeM5TaskEvent(snapshot, event({
      payload: { ...event().payload, revision: 12, task_state: "working" },
    }));

    expect(result.status).toBe("applied");
    expect(result.snapshot.tasks[0].revision).toBe(12);
  });

  it("advances the stream cursor without rolling back a newer per-task revision", () => {
    const result = mergeM5TaskEvent(
      snapshot,
      event({
        seq: 5,
        payload: { ...event().payload, revision: 7, task_state: "working" },
      })
    );

    expect(result.status).toBe("stale");
    expect(result.snapshot.event_cursor["m5-tasks"]).toBe(5);
    expect(result.snapshot.tasks[0]).toEqual(snapshot.tasks[0]);
  });

  it("ignores duplicate stream events and requests a snapshot on a sequence gap", () => {
    const duplicate = mergeM5TaskEvent(snapshot, event({ seq: 4 }));
    const gap = mergeM5TaskEvent(snapshot, event({ seq: 6 }));

    expect(duplicate.status).toBe("duplicate");
    expect(duplicate.snapshot.tasks[0]).toEqual(snapshot.tasks[0]);
    expect(gap.status).toBe("gap");
    expect(gap.snapshot).toBe(snapshot);
  });

  it("never merges a payload from another source conversation", () => {
    const foreignOrigin = "99999999-9999-4999-8999-999999999999";
    const result = mergeM5TaskEvent(
      snapshot,
      event({
        session_id: foreignOrigin,
        stream_id: `m5-tasks:${foreignOrigin}`,
        payload: { ...event().payload, origin_session_id: foreignOrigin },
      })
    );

    expect(result.status).toBe("wrong_origin");
    expect(result.snapshot).toBe(snapshot);
  });

  it("advances through selection events without changing the task projection", () => {
    const selection = event({
      event_type: "m5_selection_opened",
      task_id: null,
      payload: {
        origin_session_id: snapshot.origin_session_id,
        selection_id: "55555555-5555-4555-8555-555555555555",
        selection_version: 1,
      },
    });
    const result = mergeM5TaskEvent(snapshot, selection);

    expect(result.status).toBe("advanced");
    expect(result.snapshot.event_cursor["m5-tasks"]).toBe(5);
    expect(result.snapshot.tasks).toEqual(snapshot.tasks);
  });
});

describe("M5 task subscription lifecycle", () => {
  const origin = snapshot.origin_session_id;
  let fetchMock: ReturnType<typeof vi.fn>;
  let sources: FakeEventSource[];

  class FakeEventSource {
    static instances: FakeEventSource[] = [];
    onopen: ((this: EventSource, event: Event) => unknown) | null = null;
    onerror: ((this: EventSource, event: Event) => unknown) | null = null;
    listeners = new Map<string, Array<(event: MessageEvent<string>) => void>>();
    closed = false;
    url: string;

    constructor(url: string) {
      this.url = url;
      sources.push(this);
      queueMicrotask(() => this.onopen?.call(this as unknown as EventSource, new Event("open")));
    }

    addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
      this.listeners.set(type, [...(this.listeners.get(type) || []), listener]);
    }

    close() {
      this.closed = true;
    }

    dispatchError() {
      this.onerror?.call(this as unknown as EventSource, new Event("error"));
    }
  }

  beforeEach(() => {
    sources = [];
    FakeEventSource.instances = sources;
    fetchMock = vi.fn(async () => Response.json(snapshot));
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
    vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("does not subscribe while closed and closes the source when the list closes", async () => {
    const { rerender, unmount } = renderHook(
      ({ enabled }) => useM5Tasks(origin, enabled),
      { initialProps: { enabled: false } }
    );
    expect(fetchMock).not.toHaveBeenCalled();
    rerender({ enabled: true });
    await waitFor(() => expect(sources).toHaveLength(1));
    rerender({ enabled: false });
    expect(sources[0].closed).toBe(true);
    unmount();
  });

  it("uses a single two-second fallback poll and resnapshots before reconnecting", async () => {
    vi.useFakeTimers();
    let releaseFallback: ((response: Response) => void) | null = null;
    fetchMock.mockImplementationOnce(async () => Response.json(snapshot));
    fetchMock.mockImplementationOnce(
      () => new Promise<Response>((resolve) => { releaseFallback = resolve; })
    );
    const { result, unmount } = renderHook(() => useM5Tasks(origin, true));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.snapshot?.origin_session_id).toBe(origin);
    expect(sources).toHaveLength(1);

    act(() => sources[0].dispatchError());
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      releaseFallback?.(Response.json({ ...snapshot, event_cursor: { "m5-tasks": 7 } }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(sources).toHaveLength(2);
    expect(sources[1].url).toContain("after_seq=7");
    unmount();
    expect(sources[1].closed).toBe(true);
  });
});
