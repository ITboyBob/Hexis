import { describe, expect, it } from "vitest";
import { activeTasks, applyEvent, attention, eventReceipt, identityKey, initial, reduce } from "./projection";
import type { Event, Identity, Payload, Task } from "./contracts";

const identity = (input_order: number, stimulus_revision = 1): Identity => ({
  session_id: "session-a",
  input_turn_id: `turn-${input_order}`,
  input_order,
  stimulus_revision,
});

const makeStream = (stream_id: string) => {
  let seq = 0;
  return (payload: Payload, session_id = "session-a"): Event => ({
    event_id: `${stream_id}-${++seq}`,
    session_id,
    stream_id,
    producer_seq: seq,
    payload: payload.type === "emotion" && payload.status === "valid" && !payload.visual_window
      ? { ...payload, visual_window: { status: "accepted", owner: "local", decision_seq: 1 } }
      : payload,
  });
};

const task = (activity_id: string, phase: Task["phase"], foreground = true): Task => ({
  activity_id,
  task_id: `task-${activity_id}`,
  kind: "tool",
  foreground,
  phase,
  source: "simulated",
});

describe("异步状态投影", () => {
  it("明确报告旧身份结果被拒，且不把被拒事件标成已接纳", () => {
    const send = makeStream("receipt");
    let state = initial("session-a");
    state = reduce(state, send({ type: "input_committed", identity: identity(1) }));
    state = reduce(state, send({ type: "input_committed", identity: identity(2) }));
    const late = send({ type: "emotion", identity: identity(1), wordset_version: "v1", status: "valid", label: "happy", source: "simulated" });
    expect(eventReceipt(state, late)).toMatchObject({ accepted: false, reason: "拒绝情绪归属或词表版本不匹配" });
    expect(applyEvent(state, late).receipt.accepted).toBe(false);
  });

  it("authoritative close receipt优先于普通stream去重，accepted后的pending/error明确拒绝", () => {
    const send = makeStream("receipt");
    const first = send({ type: "input_committed", identity: identity(1) });
    let state = reduce(initial("session-a"), first);
    const close: Event = { ...first, payload: { type: "visual_window", identity: identity(1), fact: {
      status: "closed", owner: "local", decision_seq: 1, reason: "model_timeout",
    } } };
    expect(eventReceipt(state, close)).toMatchObject({ accepted: true, reason: "model_timeout", fact_applied: true });
    state = applyEvent(state, close).state;
    expect(state.visual_windows[identityKey(identity(1))]?.status).toBe("closed");

    let acceptedState = reduce(initial("session-a"), send({ type: "input_committed", identity: identity(1) }));
    acceptedState = reduce(acceptedState, send({ type: "visual_window", identity: identity(1), fact: {
      status: "accepted", owner: "local", decision_seq: 1,
    } }));
    const pending = send({ type: "emotion", identity: identity(1), wordset_version: "v1", status: "pending", source: "simulated" });
    const applied = applyEvent(acceptedState, pending);
    expect(applied.receipt).toMatchObject({ accepted: false, reason: "视觉资格已终结" });
    expect(applied.state.evaluation).toBe("pending");
    const error = send({ type: "emotion", identity: identity(1), wordset_version: "v1", status: "error", source: "simulated", reason: "late failure" });
    expect(eventReceipt(acceptedState, error)).toMatchObject({ accepted: false, reason: "视觉资格已终结" });
  });

  it("keeps_last_valid_on_error", () => {
    const send = makeStream("emotion");
    let state = initial("session-a");
    state = reduce(state, send({ type: "input_committed", identity: identity(1) }));
    state = reduce(state, send({
      type: "emotion", identity: identity(1), wordset_version: "v1", status: "valid",
      label: "happy", source: "real",
    }));
    state = reduce(state, send({ type: "input_committed", identity: identity(2) }));
    state = reduce(state, send({
      type: "emotion", identity: identity(2), wordset_version: "v1", status: "error",
      source: "real", reason: "timeout",
    }));

    expect(state.evaluation).toBe("error");
    expect(state.last_valid?.label).toBe("happy");
    expect(state.last_valid?.identity.input_order).toBe(1);
  });

  it("rejects_old_revision_after_new_input_error", () => {
    const send = makeStream("emotion");
    let state = initial("session-a");
    state = reduce(state, send({ type: "input_committed", identity: identity(3) }));
    state = reduce(state, send({
      type: "emotion", identity: identity(3), wordset_version: "v1", status: "error", source: "real",
    }));
    state = reduce(state, send({ type: "input_committed", identity: identity(4) }));
    state = reduce(state, send({
      type: "emotion", identity: identity(3), wordset_version: "v1", status: "valid",
      label: "sad", source: "real",
    }));

    expect(state.current?.input_order).toBe(4);
    expect(state.evaluation).toBe("pending");
    expect(state.last_valid).toBeUndefined();
  });

  it("accepts a higher revision for the same turn and rejects the same revision", () => {
    const send = makeStream("inputs");
    let state = initial("session-a");
    const first = identity(1, 1);
    const revised = { ...first, stimulus_revision: 2 };
    state = reduce(state, send({ type: "input_committed", identity: first }));
    state = reduce(state, send({ type: "input_committed", identity: revised }));
    const afterDuplicateRevision = reduce(state, send({ type: "input_committed", identity: revised }));
    expect(state.current?.stimulus_revision).toBe(2);
    expect(afterDuplicateRevision.current?.stimulus_revision).toBe(2);
    expect(afterDuplicateRevision.reason).toContain("拒绝旧输入");
  });

  it("rejects an older input order", () => {
    const send = makeStream("inputs");
    let state = initial("session-a");
    state = reduce(state, send({ type: "input_committed", identity: identity(2) }));
    state = reduce(state, send({ type: "input_committed", identity: identity(1) }));
    expect(state.current?.input_order).toBe(2);
    expect(state.reason).toContain("拒绝旧输入");
  });

  it("keeps_other_task_on_terminal", () => {
    const send = makeStream("tasks");
    let state = initial("session-a");
    state = reduce(state, send({ type: "task", task: task("a", "running") }));
    state = reduce(state, send({ type: "task", task: task("b", "running") }));
    state = reduce(state, send({ type: "task", task: task("a", "completed") }));
    expect(activeTasks(state).map(({ activity_id }) => activity_id)).toEqual(["b"]);
    expect(state.tasks.a.phase).toBe("completed");
  });

  it("does not revive a terminal task instance", () => {
    const send = makeStream("tasks");
    let state = initial("session-a");
    state = reduce(state, send({ type: "task", task: task("a", "running") }));
    state = reduce(state, send({ type: "task", task: task("a", "failed") }));
    state = reduce(state, send({ type: "task", task: task("a", "running") }));
    expect(state.tasks.a.phase).toBe("failed");
    expect(activeTasks(state)).toEqual([]);
  });

  it("does_not_end_cancelling_or_unknown", () => {
    const send = makeStream("tasks");
    let state = initial("session-a");
    state = reduce(state, send({ type: "task", task: task("c", "cancelling") }));
    state = reduce(state, send({ type: "task", task: task("u", "unknown") }));
    expect(activeTasks(state).map(({ activity_id }) => activity_id).sort()).toEqual(["c", "u"]);
  });

  it("rejects_progress_after_playback_terminal", () => {
    const send = makeStream("player");
    let state = initial("session-a");
    state = reduce(state, send({ type: "playback_open", playback_id: "p", output_epoch: 2, source: "simulated" }));
    state = reduce(state, send({ type: "playback", feedback: {
      playback_id: "p", output_epoch: 2, phase: "cancel", progress: 0, amplitude: 0, source: "simulated",
    } }));
    state = reduce(state, send({ type: "playback", feedback: {
      playback_id: "p", output_epoch: 2, phase: "playing", progress: 0.5, amplitude: 1, source: "simulated",
    } }));
    expect(state.playback?.phase).toBe("cancel");
    expect(state.playback?.amplitude).toBe(0);
  });

  it("clamps playback amplitude and clears it outside the playing phase", () => {
    const send = makeStream("player");
    let state = initial("session-a");
    state = reduce(state, send({ type: "playback_open", playback_id: "p", output_epoch: 1, source: "simulated" }));
    state = reduce(state, send({ type: "playback", feedback: {
      playback_id: "p", output_epoch: 1, phase: "playing", progress: 0.4, amplitude: 4, source: "simulated",
    } }));
    expect(state.playback?.amplitude).toBe(1);
    state = reduce(state, send({ type: "playback", feedback: {
      playback_id: "p", output_epoch: 1, phase: "buffering", progress: 0.4, amplitude: 0.7, source: "simulated",
    } }));
    expect(state.playback?.amplitude).toBe(0);
  });

  it("rejects_old_epoch_and_session", () => {
    const send = makeStream("player");
    let state = initial("session-a");
    state = reduce(state, send({ type: "playback_open", playback_id: "p2", output_epoch: 2, source: "simulated" }));
    const afterOldEpoch = reduce(state, send({ type: "playback_open", playback_id: "p1", output_epoch: 1, source: "simulated" }));
    const afterOtherSession = reduce(state, send({ type: "playback_open", playback_id: "other", output_epoch: 3, source: "simulated" }, "session-b"));
    expect(afterOldEpoch.playback?.playback_id).toBe("p2");
    expect(afterOtherSession.playback?.playback_id).toBe("p2");
  });

  it("拒绝模拟来源伪造当前真实播放器epoch", () => {
    const send = makeStream("player");
    let state = initial("session-a");
    state = reduce(state, send({ type: "playback_open", playback_id: "real-1", output_epoch: 1, source: "real" }));
    const forged = send({ type: "playback", feedback: {
      playback_id: "real-1", output_epoch: 1, phase: "playing", progress: 2, amplitude: 1, source: "simulated",
    } });
    expect(eventReceipt(state, forged).accepted).toBe(false);
    const next = reduce(state, forged);
    expect(next.playback).toMatchObject({ playback_id: "real-1", phase: "buffering", source: "real" });
  });

  it("does_not_compare_sequences_across_streams", () => {
    const emotion = makeStream("emotion");
    const tasks = makeStream("tasks");
    let state = initial("session-a");
    state = reduce(state, emotion({ type: "input_committed", identity: identity(1) }));
    state = reduce(state, emotion({
      type: "emotion", identity: identity(1), wordset_version: "v1", status: "valid",
      label: "curious", source: "real",
    }));
    state = reduce(state, tasks({ type: "task", task: task("a", "running") }));
    expect(state.last_valid?.label).toBe("curious");
    expect(state.tasks.a.phase).toBe("running");
  });

  it("rejects duplicate event IDs and stale sequence numbers within one stream", () => {
    const send = makeStream("events");
    let state = initial("session-a");
    const first = send({ type: "task", task: task("a", "running") });
    state = reduce(state, first);
    const duplicateId = reduce(state, { ...first, payload: { type: "task", task: task("b", "running") } });
    const staleSeq = reduce(state, {
      ...first,
      event_id: "events-stale",
      producer_seq: 0,
      payload: { type: "task", task: task("c", "running") },
    });
    expect(duplicateId).toBe(state);
    expect(staleSeq).toBe(state);
    expect(state.tasks.b).toBeUndefined();
    expect(state.tasks.c).toBeUndefined();
  });

  it("keeps the last valid label for pending, invalid, and mismatched results", () => {
    const send = makeStream("emotion");
    const current = identity(1);
    let state = initial("session-a");
    state = reduce(state, send({ type: "input_committed", identity: current }));
    state = reduce(state, send({
      type: "emotion", identity: current, wordset_version: "v1", status: "valid",
      label: "happy", source: "real",
    }));
    state = reduce(state, send({
      type: "emotion", identity: current, wordset_version: "v1", status: "pending", source: "real",
    }));
    expect(state.evaluation).toBe("valid");
    expect(state.last_valid?.label).toBe("happy");
    state = reduce(state, send({
      type: "emotion", identity: current, wordset_version: "v1", status: "valid",
      label: "joy" as never, source: "real",
    }));
    expect(state.evaluation).toBe("valid");
    expect(state.last_valid?.label).toBe("happy");
    state = reduce(state, send({
      type: "emotion", identity: current, wordset_version: "v2", status: "valid",
      label: "sad", source: "real",
    }));
    expect(state.last_valid?.label).toBe("happy");
  });

  it("destroy_is_terminal", () => {
    const send = makeStream("controls");
    const state = reduce(initial("session-a"), send({ type: "control", action: "destroy" }));
    const after = reduce(state, send({ type: "input_committed", identity: identity(1) }));
    expect(after).toBe(state);
    expect(after.current).toBeUndefined();
  });

  it("uses stable foreground attention and preserves the still-active selection", () => {
    const send = makeStream("tasks");
    let state = initial("session-a");
    state = reduce(state, send({ type: "task", task: task("z", "running") }));
    state = reduce(state, send({ type: "task", task: task("a", "running") }));
    expect(attention(state)).toBe("a");
    expect(attention(state, "z")).toBe("z");
    state = reduce(state, send({ type: "input_activity", activity_id: "mic", speaking: true, source: "manual" }));
    expect(attention(state, "a")).toBe("listening:mic");
    state = reduce(state, send({ type: "input_activity", activity_id: "mic", speaking: false, source: "manual" }));
    expect(attention(state, "z")).toBe("z");
  });

  it("reset advances visual generation without clearing business facts", () => {
    const send = makeStream("controls");
    let state = initial("session-a");
    state = reduce(state, send({ type: "input_committed", identity: identity(1) }));
    state = reduce(state, send({
      type: "emotion", identity: identity(1), wordset_version: "v1", status: "valid",
      label: "proud", source: "manual",
    }));
    state = reduce(state, send({ type: "control", action: "reset" }));
    expect(state.visual_generation).toBe(1);
    expect(state.last_valid?.label).toBe("proud");
    expect(state.current?.input_order).toBe(1);
  });

  it("reduced motion and stop effect preserve emotion and activity facts", () => {
    const send = makeStream("controls");
    let state = initial("session-a");
    state = reduce(state, send({ type: "input_committed", identity: identity(1) }));
    state = reduce(state, send({
      type: "emotion", identity: identity(1), wordset_version: "v1", status: "valid",
      label: "curious", source: "manual",
    }));
    state = reduce(state, send({ type: "task", task: task("tool", "running") }));
    state = reduce(state, send({ type: "control", action: "reduced_motion", enabled: true }));
    state = reduce(state, send({ type: "control", action: "stop_effect", effect_id: "bounce-1" }));
    expect(state.reduced_motion).toBe(true);
    expect(state.last_valid?.label).toBe("curious");
    expect(state.tasks.tool.phase).toBe("running");
  });

  it("starts with idle attention and does not let background tasks take focus", () => {
    const send = makeStream("tasks");
    let state = initial("session-a");
    expect(attention(state)).toBe("idle");
    state = reduce(state, send({ type: "task", task: task("background", "running", false) }));
    expect(attention(state)).toBe("idle");
  });

  it("closes complete-identity visual eligibility on timeout and treats late valid as diagnostic", () => {
    const send = makeStream("projection");
    let state = initial("session-a");
    const prior = identity(4, 1);
    const current = identity(5, 1);
    state = reduce(state, send({ type: "input_committed", identity: prior, visual_owner: "local" } as never));
    state = reduce(state, send({
      type: "emotion", identity: prior, wordset_version: "v1", status: "valid", label: "sad", source: "real",
      visual_window: { status: "accepted", owner: "local", decision_seq: 1 },
    } as never));
    state = reduce(state, send({ type: "input_committed", identity: current, visual_owner: "local" } as never));

    const timeoutValid: Event = {
      event_id: "late-valid-first",
      session_id: "session-a",
      stream_id: "emotion-result",
      producer_seq: 0,
      payload: {
        type: "emotion", identity: current, wordset_version: "v1", status: "valid",
        label: "angry", source: "real",
        visual_window: { status: "closed", owner: "local", decision_seq: 1, reason: "model_timeout" },
      },
    };
    state = reduce(state, timeoutValid);
    const afterLatePending = reduce(state, {
      ...timeoutValid,
      event_id: "late-pending",
      stream_id: "query",
      payload: {
        type: "emotion", identity: current, wordset_version: "v1", status: "pending", source: "real",
        visual_window: { status: "open", owner: "local", decision_seq: 0 },
      },
    } as never);

    expect(state.last_valid?.label).toBe("sad");
    expect(state.evaluation).toBe("error");
    expect(state.visual_windows?.[identityKey(current)]).toMatchObject({
      status: "closed", owner: "local", reason: "model_timeout",
    });
    expect(state.last_diagnostic).toMatchObject({ status: "valid", label: "angry", accepted: false });
    expect(afterLatePending.evaluation).toBe("error");
    expect(afterLatePending.last_valid?.label).toBe("sad");
  });

  it("does not let producer sequence filtering swallow a closed fact or let it cross identities", () => {
    const send = makeStream("projection");
    let state = initial("session-a");
    const oldIdentity = identity(1, 1);
    const current = identity(2, 1);
    state = reduce(state, send({ type: "input_committed", identity: oldIdentity, visual_owner: "service" } as never));
    state = reduce(state, send({ type: "input_committed", identity: current, visual_owner: "service" } as never));
    state = reduce(state, {
      event_id: "stream-newer",
      session_id: "session-a",
      stream_id: "deadline",
      producer_seq: 5,
      payload: { type: "emotion", identity: oldIdentity, wordset_version: "v1", status: "error", source: "real" },
    } as never);
    state = reduce(state, {
      event_id: "close-stale-stream-seq",
      session_id: "session-a",
      stream_id: "deadline",
      producer_seq: 1,
      payload: {
        type: "visual_window", identity: current,
        fact: { status: "closed", owner: "service", decision_seq: 1, reason: "total_deadline" },
      },
    } as never);
    const reset = reduce(state, send({ type: "control", action: "reset" }));
    const reduced = reduce(reset, send({ type: "control", action: "reduced_motion", enabled: true }));
    const revised = { ...current, stimulus_revision: current.stimulus_revision + 1 };
    const afterRevision = reduce(reduced, send({ type: "input_committed", identity: revised, visual_owner: "service" } as never));

    expect(state.visual_windows?.[identityKey(current)]?.status).toBe("closed");
    expect(state.reason).toContain("总期限已到");
    expect(state.visual_windows?.[identityKey(oldIdentity)]?.status).toBe("open");
    expect(reduced.visual_windows?.[identityKey(current)]?.status).toBe("closed");
    expect(afterRevision.visual_windows?.[identityKey(revised)]?.status).toBe("open");
    expect(afterRevision.current).toEqual(revised);
    const currentReason = state.reason;
    const closeOld = reduce(state, {
      event_id: "close-old-identity", session_id: "session-a", stream_id: "old-deadline", producer_seq: 0,
      payload: { type: "visual_window", identity: oldIdentity,
        fact: { status: "closed", owner: "service", decision_seq: 1, reason: "model_timeout" } },
    } as never);
    expect(closeOld.reason).toBe(currentReason);
    const staleOrdinary = reduce(state, {
      event_id: "older-ordinary-after-close", session_id: "session-a", stream_id: "deadline", producer_seq: 4,
      payload: { type: "task", task: task("stale", "running") },
    });
    expect(staleOrdinary.tasks.stale).toBeUndefined();
  });

  it.each(["error", "pending"] as const)("首次收到 %s + closed 仍执行权威闭窗", (status) => {
    const send = makeStream("projection");
    const current = identity(1);
    let state = reduce(initial("session-a"), send({ type: "input_committed", identity: current }));
    state = reduce(state, { event_id: `first-${status}`, session_id: "session-a", stream_id: "late", producer_seq: 0,
      payload: { type: "emotion", identity: current, wordset_version: "v1", status, source: "real",
        visual_window: { status: "closed", owner: "local", decision_seq: 1, reason: "total_deadline" } } });
    expect(state.visual_windows[identityKey(current)]).toMatchObject({ status: "closed", decision_seq: 1 });
    expect(state.evaluation).toBe("error");
    expect(state.last_valid).toBeUndefined();
  });

  it("keeps accepted terminal against a late timeout and does not bind a new owner", () => {
    const send = makeStream("projection");
    let state = initial("session-a");
    const current = identity(1, 1);
    state = reduce(state, send({ type: "input_committed", identity: current, visual_owner: "local" } as never));
    state = reduce(state, send({
      type: "emotion", identity: current, wordset_version: "v1", status: "valid", label: "curious", source: "manual",
      visual_window: { status: "accepted", owner: "local", decision_seq: 1 },
    } as never));
    const afterLateClose = reduce(state, {
      event_id: "late-timer",
      session_id: "session-a",
      stream_id: "timeout",
      producer_seq: 0,
      payload: {
        type: "visual_window", identity: current,
        fact: { status: "closed", owner: "local", decision_seq: 2, reason: "model_timeout" },
      },
    } as never);
    const wrongOwner = reduce(state, {
      event_id: "other-owner",
      session_id: "session-a",
      stream_id: "service",
      producer_seq: 0,
      payload: {
        type: "emotion", identity: current, wordset_version: "v1", status: "valid", label: "sad", source: "real",
        visual_window: { status: "accepted", owner: "service", decision_seq: 99 },
      },
    } as never);

    expect(afterLateClose.visual_windows?.[identityKey(current)]?.status).toBe("accepted");
    expect(afterLateClose.last_valid?.label).toBe("curious");
    expect(wrongOwner.last_valid?.label).toBe("curious");
  });

  it("accepted同身份重复标签幂等，但不同业务结果不能替换首次裁定", () => {
    const send = makeStream("projection");
    const current = identity(1);
    let state = initial("session-a");
    state = reduce(state, send({ type: "input_committed", identity: current }));
    state = reduce(state, send({ type: "emotion", identity: current, wordset_version: "v1", status: "valid",
      label: "curious", source: "real", visual_window: { status: "accepted", owner: "local", decision_seq: 1 } }));
    const first = state.last_valid;
    state = reduce(state, send({ type: "emotion", identity: current, wordset_version: "v1", status: "valid",
      label: "curious", source: "real", visual_window: { status: "accepted", owner: "local", decision_seq: 1 } }));
    expect(state.last_valid).toEqual(first);
    state = reduce(state, send({ type: "emotion", identity: current, wordset_version: "v1", status: "valid",
      label: "sad", source: "real", visual_window: { status: "accepted", owner: "local", decision_seq: 1 } }));
    expect(state.last_valid).toEqual(first);
    expect(state.last_diagnostic).toMatchObject({ accepted: false, reason: "已裁定结果不可替换" });
  });

  it("accepted通知先到时只允许首个同裁定结果填入一次", () => {
    const send = makeStream("projection");
    const current = identity(1);
    let state = reduce(initial("session-a"), send({ type: "input_committed", identity: current }));
    state = reduce(state, send({ type: "visual_window", identity: current,
      fact: { status: "accepted", owner: "local", decision_seq: 1 } }));
    state = reduce(state, send({ type: "emotion", identity: current, wordset_version: "v1", status: "valid",
      label: "happy", source: "real", visual_window: { status: "accepted", owner: "local", decision_seq: 1 } }));
    const first = state.last_valid;
    state = reduce(state, send({ type: "emotion", identity: current, wordset_version: "v1", status: "valid",
      label: "sad", source: "real", visual_window: { status: "accepted", owner: "local", decision_seq: 1 } }));
    expect(state.last_valid).toEqual(first);
    expect(state.last_diagnostic).toMatchObject({ accepted: false, reason: "已裁定结果不可替换" });
  });

  it("embedded close sequence必须是open后的下一序号", () => {
    const send = makeStream("projection");
    const current = identity(1);
    let state = reduce(initial("session-a"), send({ type: "input_committed", identity: current }));
    const rejected = reduce(state, { event_id: "bad-seq-close", session_id: "session-a", stream_id: "result", producer_seq: 0,
      payload: { type: "emotion", identity: current, wordset_version: "v1", status: "error", source: "real",
        visual_window: { status: "closed", owner: "local", decision_seq: 9, reason: "model_timeout" } } });
    expect(rejected.visual_windows[identityKey(current)]?.status).toBe("open");
    state = reduce(state, { event_id: "good-seq-close", session_id: "session-a", stream_id: "result", producer_seq: 0,
      payload: { type: "emotion", identity: current, wordset_version: "v1", status: "error", source: "real",
        visual_window: { status: "closed", owner: "local", decision_seq: 1, reason: "model_timeout" } } });
    expect(state.visual_windows[identityKey(current)]?.status).toBe("closed");
  });
});
