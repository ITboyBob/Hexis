import { isEmotion, WORDSET_VERSION } from "./contracts";
import type { Emotion, Event, Identity, Playback, Source, Task, VisualWindowFact } from "./contracts";

export type State = {
  session_id: string;
  current?: Identity;
  last_valid?: { label: Emotion; identity: Identity; source: Source };
  visual_windows: Record<string, VisualWindowFact>;
  accepted_results: Record<string, { label: Emotion; source: Source }>;
  last_diagnostic?: { identity: Identity; status: "pending" | "valid" | "error"; label?: Emotion; accepted: boolean; reason?: string };
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
    visual_windows: {},
    accepted_results: {},
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

export function identityKey(identity: Identity): string {
  return JSON.stringify([identity.session_id, identity.input_turn_id, identity.input_order, identity.stimulus_revision]);
}

export type ReceiptCode = "duplicate" | "session_mismatch" | "identity_mismatch" | "wordset_mismatch" | "closed" | "missing_window" | "terminal" | "rejected";
export type EventReceipt = { accepted: boolean; reason: string; fact_applied?: boolean; code?: ReceiptCode };

/** Authoritative event receipt for diagnostics; no UI caller infers acceptance from object identity. */
export function eventReceipt(state: State, event: Event): EventReceipt {
  if (state.dead) return { accepted: false, reason: "预览已销毁", code: "terminal" };
  if (event.session_id !== state.session_id) return { accepted: false, reason: "事件会话与当前预览不匹配", code: "session_mismatch" };
  const payload = event.payload;
  const closeFact = payload.type === "visual_window" && payload.fact.status === "closed"
    ? { identity: payload.identity, fact: payload.fact, carriesEmotion: false }
    : payload.type === "emotion" && payload.visual_window?.status === "closed"
      ? { identity: payload.identity, fact: payload.visual_window, carriesEmotion: true }
      : undefined;
  if (!closeFact && (state.seen[event.event_id] || event.producer_seq <= (state.seq[event.stream_id] ?? -1))) {
    return { accepted: false, reason: "重复事件ID或stream序号过旧", code: "duplicate" };
  }
  if (closeFact) {
    const current = state.visual_windows[identityKey(closeFact.identity)];
    const valid = closeFact.identity.session_id === state.session_id && current
      && current.owner === closeFact.fact.owner && current.status === "open"
      && closeFact.fact.decision_seq === current.decision_seq + 1;
    return valid
      ? { accepted: !closeFact.carriesEmotion, fact_applied: true, reason: closeFact.fact.reason ?? "视觉资格已关闭" }
      : { accepted: false, reason: "视觉资格关闭事实与当前开窗不匹配", code: "rejected" };
  }
  if (payload.type === "input_committed") {
    const current = state.current;
    const identity = payload.identity;
    const older = current && (identity.input_order < current.input_order
      || (identity.input_order === current.input_order
        && (identity.input_turn_id !== current.input_turn_id || identity.stimulus_revision <= current.stimulus_revision)));
    return identity.session_id === state.session_id && !older
      ? { accepted: true, reason: "输入已接纳" }
      : { accepted: false, reason: "拒绝旧输入或相同版本重复提交", code: "identity_mismatch" };
  }
  if (payload.type === "emotion") {
    const key = identityKey(payload.identity);
    const fact = state.visual_windows[key];
    if (!sameIdentity(state.current, payload.identity) || payload.wordset_version !== WORDSET_VERSION) {
      return { accepted: false, reason: "拒绝情绪归属或词表版本不匹配", code: sameIdentity(state.current, payload.identity) ? "wordset_mismatch" : "identity_mismatch" };
    }
    if (payload.visual_window?.status === "closed") return { accepted: false, reason: payload.visual_window.reason ?? "视觉资格已关闭", code: "closed" };
    if (fact?.status === "closed") return { accepted: false, reason: fact.reason ?? "视觉资格已关闭", code: "closed" };
    if (!fact) return { accepted: false, reason: "输入尚未开视觉资格窗口", code: "missing_window" };
    if (fact.status === "accepted" && payload.status !== "valid") return { accepted: false, reason: "视觉资格已终结", code: "terminal" };
    if (payload.status !== "valid") return { accepted: true, reason: payload.reason ?? `已记录${payload.status}` };
    const supplied = payload.visual_window;
    const firstAcceptedResult = fact?.status === "accepted" && supplied?.status === "accepted"
      && supplied.owner === fact.owner && supplied.decision_seq === fact.decision_seq
      && !state.accepted_results[key];
    const openWindowResult = fact?.status === "open" && supplied?.status === "accepted"
      && supplied.owner === fact.owner && supplied.decision_seq === fact.decision_seq + 1;
    if (!isEmotion(payload.label) || (!firstAcceptedResult && !openWindowResult)) {
      return { accepted: false, reason: "缺少匹配的accepted视觉资格" };
    }
    return { accepted: true, reason: "合法情绪与视觉资格已接纳" };
  }
  if (payload.type === "visual_window") {
    const current = state.visual_windows[identityKey(payload.identity)];
    const valid = current && current.owner === payload.fact.owner && current.status === "open"
      && payload.fact.decision_seq === current.decision_seq + 1
      && (payload.fact.status === "accepted" || payload.fact.status === "closed");
    return valid ? { accepted: true, reason: payload.fact.status === "closed" ? payload.fact.reason ?? "视觉资格已关闭" : "视觉资格已接纳" }
      : { accepted: false, reason: "视觉资格事实与当前开窗不匹配", code: "rejected" };
  }
  if (payload.type === "task") {
    const current = state.tasks[payload.task.activity_id];
    return current && terminalTaskPhases.has(current.phase)
      ? { accepted: false, reason: "活动实例已终止" }
      : { accepted: true, reason: "活动状态已记录" };
  }
  if (payload.type === "playback_open") {
    return state.playback && payload.output_epoch <= state.playback.output_epoch
      ? { accepted: false, reason: "播放窗口须推进epoch" }
      : { accepted: true, reason: "播放epoch已打开" };
  }
  if (payload.type === "playback") {
    const current = state.playback;
    const feedback = payload.feedback;
    return current && !state.playback_closed && current.playback_id === feedback.playback_id
      && current.output_epoch === feedback.output_epoch
      && current.source === feedback.source
      ? { accepted: true, reason: "播放反馈已记录" }
      : { accepted: false, reason: "拒绝旧播放反馈" };
  }
  return { accepted: true, reason: payload.type === "control" ? `已执行${payload.action}` : "输入活动已记录" };
}

export function applyEvent(state: State, event: Event): { state: State; receipt: EventReceipt } {
  return { state: reduce(state, event), receipt: eventReceipt(state, event) };
}

export function reduce(state: State, event: Event): State {
  if (state.dead || event.session_id !== state.session_id) return state;
  const payload = event.payload;
  const closure = payload.type === "visual_window" && payload.fact.status === "closed"
    ? { identity: payload.identity, fact: payload.fact, status: "error" as const }
    : payload.type === "emotion" && payload.visual_window?.status === "closed"
      ? { identity: payload.identity, fact: payload.visual_window, status: payload.status, label: payload.label }
      : undefined;
  const authoritativeClose = !!closure;
  if (!authoritativeClose && (state.seen[event.event_id] || event.producer_seq <= (state.seq[event.stream_id] ?? -1))) {
    return state;
  }

  const next: State = {
    ...state,
    seen: { ...state.seen, [event.event_id]: true },
    seq: { ...state.seq, [event.stream_id]: event.producer_seq },
  };

  if (authoritativeClose) {
    if (!closure || closure.identity.session_id !== state.session_id) return state;
    const { identity, fact } = closure;
    const key = identityKey(identity);
    const currentFact = state.visual_windows[key];
    if (!currentFact || currentFact.owner !== fact.owner || currentFact.status !== "open"
      || fact.decision_seq !== currentFact.decision_seq + 1) return state;
    const visual_windows = { ...state.visual_windows, [key]: fact };
    const isCurrent = sameIdentity(state.current, identity);
    const seq = Math.max(state.seq[event.stream_id] ?? -1, event.producer_seq);
    const diagnosticStatus = closure.status;
    const diagnosticLabel = "label" in closure ? closure.label : undefined;
    return { ...next, seq: { ...next.seq, [event.stream_id]: seq }, visual_windows,
      evaluation: isCurrent ? "error" : state.evaluation,
      last_diagnostic: { identity, status: diagnosticStatus, label: diagnosticLabel, accepted: false, reason: fact.reason },
      reason: isCurrent
        ? fact.reason === "model_timeout"
          ? "情绪评估期限已到；最近有效情绪保留，可检查设置后显式重试"
          : "情绪总期限已到；最近有效情绪保留，可检查上下文与设置后显式重试"
        : state.reason };
  }

  if (payload.type === "visual_window") {
    const key = identityKey(payload.identity);
    const existing = state.visual_windows[key];
    if (!existing || existing.owner !== payload.fact.owner || existing.status !== "open"
      || payload.fact.status !== "accepted" || payload.fact.decision_seq !== existing.decision_seq + 1) return state;
    return { ...next, visual_windows: { ...state.visual_windows, [key]: payload.fact } };
  }

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
    const key = identityKey(incoming);
    const owner = payload.visual_owner ?? "local";
    return { ...next, current: incoming, evaluation: "pending",
      visual_windows: { ...state.visual_windows, [key]: state.visual_windows[key] ?? { status: "open", owner, decision_seq: 0 } },
      reason: "已接纳新输入" };
  }

  if (payload.type === "emotion") {
    const key = identityKey(payload.identity);
    const fact = payload.visual_window;
    const currentFact = state.visual_windows[key];
    if (!sameIdentity(state.current, payload.identity) || payload.wordset_version !== WORDSET_VERSION) {
      return { ...next, reason: "拒绝情绪归属或词表版本不匹配" };
    }
    if (payload.status === "valid" && isEmotion(payload.label) && fact?.status === "accepted"
      && currentFact !== undefined && currentFact.owner === fact.owner
      && ((currentFact.status === "open" && fact.decision_seq === currentFact.decision_seq + 1)
        || (currentFact.status === "accepted" && fact.decision_seq === currentFact.decision_seq))) {
      const previouslyAccepted = state.accepted_results[key];
      if (currentFact.status === "accepted" && previouslyAccepted) {
        const sameResult = previouslyAccepted.label === payload.label && previouslyAccepted.source === payload.source;
        return { ...next, last_diagnostic: { identity: payload.identity, status: "valid", label: payload.label,
          accepted: false, reason: sameResult ? "重复结果" : "已裁定结果不可替换" } };
      }
      return {
        ...next,
        visual_windows: { ...state.visual_windows, [key]: fact },
        accepted_results: { ...state.accepted_results, [key]: { label: payload.label, source: payload.source } },
        last_valid: { label: payload.label, identity: payload.identity, source: payload.source },
        evaluation: "valid",
        last_diagnostic: { identity: payload.identity, status: "valid", label: payload.label, accepted: true },
        reason: "已接纳合法情绪",
      };
    }
    if (currentFact?.status === "accepted") return { ...next, last_diagnostic: { identity: payload.identity,
      status: payload.status, accepted: false, reason: "视觉资格已终结" } };
    if (currentFact?.status === "closed") return { ...next, last_diagnostic: { identity: payload.identity, status: payload.status, accepted: false, reason: payload.reason } };
    return {
      ...next,
      evaluation: payload.status === "pending" ? "pending" : "error",
      last_diagnostic: { identity: payload.identity, status: payload.status, accepted: false, reason: payload.reason },
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
        played_ms: 0,
        interval_ms: 0,
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
      || active.source !== feedback.source
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
