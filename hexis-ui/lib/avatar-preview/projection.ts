import { isEmotion, WORDSET_VERSION } from "./contracts";
import type { Emotion, Event, Identity, Playback, Source, Task } from "./contracts";

export type State = {
  session_id: string;
  current?: Identity;
  last_valid?: { label: Emotion; identity: Identity; source: Source };
  evaluation: "unassessed" | "pending" | "valid" | "error";
  tasks: Record<string, Task>;
  inputs: Record<string, Source>;
  playback?: Playback;
  playback_closed: boolean;
  seen: Record<string, true>;
  seq: Record<string, number>;
  dead: boolean;
  reduced_motion: boolean;
  visual_generation: number;
  reason: string;
};

const terminalTaskPhases = new Set<Task["phase"]>(["completed", "failed", "cancelled"]);
const terminalPlaybackPhases = new Set<Playback["phase"]>(["drained", "cancel", "error"]);

export function initial(session_id: string): State {
  return {
    session_id,
    evaluation: "unassessed",
    tasks: {},
    inputs: {},
    playback_closed: true,
    seen: {},
    seq: {},
    dead: false,
    reduced_motion: false,
    visual_generation: 0,
    reason: "尚未评估",
  };
}

function sameIdentity(a: Identity | undefined, b: Identity): boolean {
  return !!a
    && a.session_id === b.session_id
    && a.input_turn_id === b.input_turn_id
    && a.input_order === b.input_order
    && a.stimulus_revision === b.stimulus_revision;
}

export function reduce(state: State, event: Event): State {
  if (state.dead || event.session_id !== state.session_id) return state;
  if (state.seen[event.event_id] || event.producer_seq <= (state.seq[event.stream_id] ?? -1)) {
    return state;
  }

  const next: State = {
    ...state,
    seen: { ...state.seen, [event.event_id]: true },
    seq: { ...state.seq, [event.stream_id]: event.producer_seq },
  };
  const payload = event.payload;

  if (payload.type === "input_committed") {
    const current = state.current;
    const incoming = payload.identity;
    const isOlder = current && (
      incoming.input_order < current.input_order
      || (incoming.input_order === current.input_order && (
        incoming.input_turn_id !== current.input_turn_id
        || incoming.stimulus_revision <= current.stimulus_revision
      ))
    );
    if (incoming.session_id !== state.session_id || isOlder) {
      return { ...next, reason: "拒绝旧输入或相同版本重复提交" };
    }
    return { ...next, current: incoming, evaluation: "pending", reason: "已接纳新输入" };
  }

  if (payload.type === "emotion") {
    if (!sameIdentity(state.current, payload.identity) || payload.wordset_version !== WORDSET_VERSION) {
      return { ...next, reason: "拒绝情绪归属或词表版本不匹配" };
    }
    if (payload.status === "valid" && isEmotion(payload.label)) {
      return {
        ...next,
        last_valid: { label: payload.label, identity: payload.identity, source: payload.source },
        evaluation: "valid",
        reason: "已接纳合法情绪",
      };
    }
    return {
      ...next,
      evaluation: payload.status === "pending" ? "pending" : "error",
      reason: payload.reason ?? "本次评估失败；保留最近有效情绪",
    };
  }

  if (payload.type === "input_activity") {
    const inputs = { ...state.inputs };
    if (payload.speaking) inputs[payload.activity_id] = payload.source;
    else delete inputs[payload.activity_id];
    return { ...next, inputs };
  }

  if (payload.type === "task") {
    const existing = state.tasks[payload.task.activity_id];
    if (existing && terminalTaskPhases.has(existing.phase)) {
      return { ...next, reason: "活动实例已终止" };
    }
    return { ...next, tasks: { ...state.tasks, [payload.task.activity_id]: payload.task } };
  }

  if (payload.type === "playback_open") {
    if (state.playback && payload.output_epoch <= state.playback.output_epoch) {
      return { ...next, reason: "播放窗口须推进 epoch" };
    }
    return {
      ...next,
      playback_closed: false,
      playback: {
        playback_id: payload.playback_id,
        output_epoch: payload.output_epoch,
        phase: "buffering",
        progress: 0,
        amplitude: 0,
        source: payload.source,
      },
    };
  }

  if (payload.type === "playback") {
    const active = state.playback;
    const feedback = payload.feedback;
    if (
      !active
      || state.playback_closed
      || active.playback_id !== feedback.playback_id
      || active.output_epoch !== feedback.output_epoch
    ) {
      return { ...next, reason: "拒绝旧播放反馈" };
    }
    return {
      ...next,
      playback_closed: terminalPlaybackPhases.has(feedback.phase),
      playback: {
        ...feedback,
        amplitude: feedback.phase === "playing"
          ? Math.max(0, Math.min(1, feedback.amplitude))
          : 0,
      },
    };
  }

  if (payload.type === "control") {
    if (payload.action === "destroy") return { ...next, dead: true };
    if (payload.action === "reset") {
      return { ...next, visual_generation: state.visual_generation + 1 };
    }
    if (payload.action === "reduced_motion") {
      return { ...next, reduced_motion: Boolean(payload.enabled) };
    }
    return next;
  }

  return next;
}

export function attention(state: State, previous?: string): string {
  const listening = Object.keys(state.inputs).sort();
  if (listening.length) return `listening:${listening[0]}`;

  const foreground = Object.values(state.tasks)
    .filter((task) => task.foreground && !terminalTaskPhases.has(task.phase))
    .map((task) => task.activity_id)
    .sort();
  return previous && foreground.includes(previous) ? previous : foreground[0] ?? "idle";
}

export function activeTasks(state: State): Task[] {
  return Object.values(state.tasks).filter((task) => !terminalTaskPhases.has(task.phase));
}
