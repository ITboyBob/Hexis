import { compose } from "../avatar-preview/composition";
import { isEmotion, WORDSET_VERSION } from "../avatar-preview/contracts";
import type {
  Emotion,
  Event,
  Identity,
  Playback,
  VisualWindowFact,
} from "../avatar-preview/contracts";
import type { VoiceBehaviorEvent } from "./contracts";
import { initial, reduce } from "../avatar-preview/projection";
import type { State as PreviewState } from "../avatar-preview/projection";
import type { Composition } from "../avatar-preview/engine";
import type { NativePlaybackIdentity } from "../voice-playback/native-player";

export type { VoiceBehaviorEvent } from "./contracts";

type Activity = { identity: Identity; activity_id: string };
export type LocalPlayback = {
  identity: NativePlaybackIdentity;
  feedback: Playback;
};

export type VoiceAvatarState = {
  session_id: string;
  media_session_id: string | null;
  preview: PreviewState;
  modelActivities: Record<string, Activity>;
  recallActivities: Record<string, Activity>;
  localPlayback: LocalPlayback | null;
};

export function initialVoiceAvatarState(sessionId: string, mediaSessionId: string | null = null): VoiceAvatarState {
  return {
    session_id: sessionId,
    media_session_id: mediaSessionId,
    preview: initial(sessionId),
    modelActivities: {},
    recallActivities: {},
    localPlayback: null,
  };
}

export function setVoiceAvatarMediaSession(state: VoiceAvatarState, mediaSessionId: string): VoiceAvatarState {
  if (!mediaSessionId || state.media_session_id === mediaSessionId) return state;
  return initialVoiceAvatarState(state.session_id, mediaSessionId);
}

function identityFrom(value: unknown): Identity | undefined {
  if (!value || typeof value !== "object") return undefined;
  const identity = value as Partial<Identity>;
  return typeof identity.session_id === "string"
    && typeof identity.input_turn_id === "string"
    && Number.isInteger(identity.input_order)
    && Number.isInteger(identity.stimulus_revision)
    ? identity as Identity
    : undefined;
}

function sameIdentity(left: Identity | undefined, right: Identity | undefined): boolean {
  return !!left && !!right
    && left.session_id === right.session_id
    && left.input_turn_id === right.input_turn_id
    && left.input_order === right.input_order
    && left.stimulus_revision === right.stimulus_revision;
}

function previewEvent(
  event: VoiceBehaviorEvent,
  payload: Event["payload"],
  state: VoiceAvatarState,
): PreviewState {
  return reduce(state.preview, {
    event_id: event.event_id,
    session_id: event.session_id,
    stream_id: event.stream_id,
    producer_seq: event.seq,
    payload,
  });
}

export function applyVoiceBehaviorEvent(
  state: VoiceAvatarState,
  event: VoiceBehaviorEvent,
): VoiceAvatarState {
  if (
    !event.event_id
    || event.schema_version !== 1
    || !event.stream_id
    || !Number.isInteger(event.seq)
    || event.seq <= 0
    || !Number.isInteger(event.producer_seq)
    || event.producer_seq < 0
    || !event.payload
    || typeof event.payload !== "object"
    || event.session_id !== state.session_id
    || (state.media_session_id !== null && event.media_session_id !== state.media_session_id)
  ) return state;

  const payload = event.payload;
  const identity = identityFrom(payload.identity);
  if (event.event_type === "voice_input_committed" && identity) {
    const preview = previewEvent(event, { type: "input_committed", identity, visual_owner: "service" }, state);
    return {
      ...state,
      preview,
      ...(sameIdentity(preview.current, identity) ? { modelActivities: {}, recallActivities: {} } : {}),
    };
  }

  if (event.event_type === "voice_visual_window" && identity && sameIdentity(state.preview.current, identity)) {
    const fact = payload.fact as VisualWindowFact | undefined;
    if (!fact || fact.owner !== "service") return state;
    let preview = state.preview;
    if (fact.status === "accepted" && isEmotion(payload.label)) {
      preview = previewEvent(event, {
        type: "emotion",
        identity,
        wordset_version: typeof payload.wordset_version === "string" ? payload.wordset_version : "",
        status: "valid",
        label: payload.label as Emotion,
        source: "real",
        visual_window: fact,
      }, state);
    } else if (fact.status === "accepted" || fact.status === "closed") {
      preview = previewEvent(event, { type: "visual_window", identity, fact }, state);
    }
    return { ...state, preview };
  }

  if (event.event_type === "voice_speech_start" || event.event_type === "voice_speech_end") {
    // 语音起止由云端负责，不再投影为前台姿态。
    return state;
  }

  if (
    (event.event_type === "voice_model_call_started" || event.event_type === "voice_model_call_ended"
      || event.event_type === "voice_recall_started" || event.event_type === "voice_recall_ended")
    && identity && sameIdentity(state.preview.current, identity)
    && typeof payload.activity_id === "string"
  ) {
    const isRecall = event.event_type.startsWith("voice_recall_");
    const activities = isRecall ? state.recallActivities : state.modelActivities;
    const activity_id = payload.activity_id;
    const nextActivities = { ...activities };
    if (event.event_type.endsWith("_started")) nextActivities[activity_id] = { identity, activity_id };
    else delete nextActivities[activity_id];
    return isRecall
      ? { ...state, recallActivities: nextActivities }
      : { ...state, modelActivities: nextActivities };
  }

  return state;
}

export function replayVoiceBehaviorEvents(
  state: VoiceAvatarState,
  events: readonly VoiceBehaviorEvent[],
): VoiceAvatarState {
  const ordered = [...events].sort((left, right) =>
    left.stream_id.localeCompare(right.stream_id)
    || left.seq - right.seq
    || left.event_id.localeCompare(right.event_id));
  return ordered.reduce(applyVoiceBehaviorEvent, state);
}

export function applyLocalPlaybackFeedback(
  state: VoiceAvatarState,
  value: LocalPlayback,
): VoiceAvatarState {
  const { identity, feedback } = value;
  if (
    state.media_session_id === null
    || identity.media_session_id !== state.media_session_id
    || feedback.source !== "real"
  ) return state;

  const current = state.localPlayback;
  if (current) {
    if (identity.output_epoch < current.identity.output_epoch) return state;
    if (identity.output_epoch === current.identity.output_epoch
      && (identity.playback_id !== current.identity.playback_id
        || identity.response_id !== current.identity.response_id)) return state;
  }
  if (current && identity.playback_id === current.identity.playback_id
    && feedback.phase === "playing" && current.feedback.phase !== "playing") return state;

  return { ...state, localPlayback: { identity: { ...identity }, feedback: { ...feedback } } };
}

export function projectVoiceAvatar(state: VoiceAvatarState): {
  composition: Composition;
  bubble?: "thinking" | "recalling";
  emotionLabel?: Emotion;
} {
  const focus = compose(state.preview, undefined, 8).value;
  const identity = state.preview.current;
  const hasCurrentRecall = identity && Object.values(state.recallActivities).some((item) => sameIdentity(item.identity, identity));
  const hasCurrentModelCall = identity && Object.values(state.modelActivities).some((item) => sameIdentity(item.identity, identity));
  const emotionLabel = identity && state.preview.last_valid
    && sameIdentity(state.preview.last_valid.identity, identity)
    ? state.preview.last_valid.label
    : undefined;
  const composition: Composition = {
    ...focus,
    pose: hasCurrentModelCall || hasCurrentRecall ? "thinking" : "idle",
    playing: false,
    amplitude: 0,
  };
  return {
    composition,
    ...(emotionLabel ? { emotionLabel } : {}),
    ...(hasCurrentRecall ? { bubble: "recalling" as const }
      : hasCurrentModelCall ? { bubble: "thinking" as const } : {}),
  };
}
