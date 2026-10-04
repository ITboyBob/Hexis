import { describe, expect, it } from "vitest";
import { activeTasks, attention, initial, reduce } from "./projection";
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
    payload,
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
    expect(state.evaluation).toBe("pending");
    expect(state.last_valid?.label).toBe("happy");
    state = reduce(state, send({
      type: "emotion", identity: current, wordset_version: "v1", status: "valid",
      label: "joy" as never, source: "real",
    }));
    expect(state.evaluation).toBe("error");
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
});
