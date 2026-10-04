import { describe, expect, it } from "vitest";
import type { Event, Payload, PlaybackPhase, Task } from "./contracts";
import { initial, reduce } from "./projection";
import { compose } from "./composition";

const makeStream = () => {
  let seq = 0;
  return (payload: Payload): Event => ({
    event_id: `compose-${++seq}`,
    session_id: "session-a",
    stream_id: "compose",
    producer_seq: seq,
    payload,
  });
};

const task = (activity_id: string, kind: string, foreground: boolean): Task => ({
  activity_id,
  task_id: activity_id,
  kind,
  foreground,
  phase: "running",
  source: "real",
});

describe("合成眼形、活动姿态和播放微动", () => {
  it("starts idle without stealing attention from background work", () => {
    const send = makeStream();
    let state = initial("session-a");
    state = reduce(state, send({ type: "task", task: task("tool-a", "tool", false) }));
    state = reduce(state, send({ type: "task", task: task("tool-b", "tool", false) }));

    const result = compose(state, undefined, 12);
    expect(result.focus).toBe("idle");
    expect(result.value).toMatchObject({ eye: "idle", pose: "idle", playing: false, amplitude: 0 });
  });

  it("keeps the valid emotional eye while listening remains the foreground pose", () => {
    const send = makeStream();
    let state = initial("session-a");
    const identity = {
      session_id: "session-a", input_turn_id: "turn-a", input_order: 1, stimulus_revision: 1,
    };
    state = reduce(state, send({ type: "input_committed", identity }));
    state = reduce(state, send({
      type: "emotion", identity, wordset_version: "v1", status: "valid", label: "happy", source: "real",
      visual_window: { status: "accepted", owner: "local", decision_seq: 1 },
    }));
    state = reduce(state, send({ type: "input_activity", activity_id: "user-voice", speaking: true, source: "real" }));
    state = reduce(state, send({ type: "task", task: task("tool-a", "tool", false) }));
    state = reduce(state, send({ type: "task", task: task("tool-b", "tool", false) }));
    state = reduce(state, send({ type: "playback_open", playback_id: "play-1", output_epoch: 1, source: "real" }));
    state = reduce(state, send({
      type: "playback", feedback: {
        playback_id: "play-1", output_epoch: 1, phase: "playing", progress: 0.5,
        played_ms: 500, interval_ms: 0, amplitude: 0.7, source: "real",
      },
    }));

    const result = compose(state, undefined, 12);
    expect(result.focus).toBe("listening:user-voice");
    expect(result.value).toMatchObject({
      eye: "happy", pose: "listening", playing: true, amplitude: 0.7, motion_limit_px: 12,
    });
  });

  it("uses the focused foreground task to choose thinking or working pose", () => {
    const send = makeStream();
    let state = initial("session-a");
    state = reduce(state, send({ type: "task", task: task("analysis", "reply", true) }));
    expect(compose(state, undefined, 8).value.pose).toBe("thinking");
    state = reduce(state, send({ type: "task", task: task("analysis", "tool", true) }));
    expect(compose(state, undefined, 8).value.pose).toBe("working");
  });

  it.each(["buffering", "cancel", "drained", "error"] as const)(
    "zeros playback contribution in %s and carries reduced motion",
    (phase: PlaybackPhase) => {
    const send = makeStream();
    let state = initial("session-a");
    state = reduce(state, send({ type: "playback_open", playback_id: "p", output_epoch: 1, source: "simulated" }));
    state = reduce(state, send({
      type: "playback", feedback: {
        playback_id: "p", output_epoch: 1, phase, progress: 0,
        played_ms: 0, interval_ms: 0, amplitude: 0.8, source: "simulated",
      },
    }));
    state = reduce(state, send({ type: "control", action: "reduced_motion", enabled: true }));

    expect(compose(state, undefined, 5).value).toMatchObject({
      playing: false, amplitude: 0, reduced_motion: true,
    });
    expect(() => compose(state, undefined, Number.NaN)).toThrow("限幅无效");
    },
  );
});
