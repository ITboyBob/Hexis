import { eyeKey } from "./contracts";
import { attention } from "./projection";
import type { State } from "./projection";
import type { Composition } from "./engine";

export function compose(state: State, previous: string | undefined, limit: number) {
  if (!Number.isFinite(limit) || limit < 0) throw new Error("播放微动限幅无效");
  const focus = attention(state, previous);
  const task = state.tasks[focus];
  const pose: Composition["pose"] = focus.startsWith("listening:")
    ? "listening"
    : task?.phase === "running"
      ? (["emotion", "appraisal", "reply"].includes(task.kind) ? "thinking" : "working")
      : "idle";
  const playing = state.playback?.phase === "playing" && !state.playback_closed;
  const value: Composition = {
    eye: state.last_valid ? eyeKey(state.last_valid.label) : "idle",
    pose,
    playing,
    amplitude: playing ? state.playback!.amplitude : 0,
    motion_limit_px: limit,
    reduced_motion: state.reduced_motion,
  };
  return { focus, value };
}
