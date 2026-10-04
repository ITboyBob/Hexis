export const EMOTIONS = [
  "neutral",
  "happy",
  "excited",
  "curious",
  "surprised",
  "confused",
  "suspicious",
  "angry",
  "sad",
  "scared",
  "proud",
  "shy",
  "bored",
  "playful",
] as const;

export type Emotion = (typeof EMOTIONS)[number];
export const WORDSET_VERSION = "v1";

const emotionSet: ReadonlySet<string> = new Set(EMOTIONS);

export function isEmotion(value: unknown): value is Emotion {
  return typeof value === "string" && emotionSet.has(value);
}

export function eyeKey(value: Emotion): string {
  return value === "neutral" ? "idle" : value;
}

export type Identity = {
  session_id: string;
  input_turn_id: string;
  input_order: number;
  stimulus_revision: number;
};

export type VisualWindowOwner = "local" | "service";
export type VisualWindowStatus = "open" | "accepted" | "closed";
export type VisualWindowReason = "model_timeout" | "total_deadline";
export type VisualWindowFact = {
  status: VisualWindowStatus;
  owner: VisualWindowOwner;
  decision_seq: number;
  reason?: VisualWindowReason;
};

export type Source = "manual" | "simulated" | "real";
export type TaskPhase =
  | "queued"
  | "running"
  | "waiting_user"
  | "cancelling"
  | "unknown"
  | "completed"
  | "failed"
  | "cancelled";

export type Task = {
  activity_id: string;
  task_id: string | null;
  kind: string;
  foreground: boolean;
  phase: TaskPhase;
  source: Source;
};

export type PlaybackPhase = "playing" | "buffering" | "drained" | "cancel" | "error";
export type Playback = {
  playback_id: string;
  output_epoch: number;
  response_id?: string;
  media_session_id?: string;
  phase: PlaybackPhase;
  progress: number;
  played_ms: number;
  interval_ms: number;
  amplitude: number;
  source: Source;
};

export type Payload =
  | { type: "input_committed"; identity: Identity; visual_owner?: VisualWindowOwner }
  | {
      type: "emotion";
      identity: Identity;
      wordset_version: string;
      status: "pending" | "valid" | "error";
      label?: Emotion;
      source: Source;
      reason?: string;
      visual_window?: VisualWindowFact;
    }
  | { type: "visual_window"; identity: Identity; fact: VisualWindowFact }
  | { type: "input_activity"; activity_id: string; speaking: boolean; source: Source }
  | { type: "task"; task: Task }
  | { type: "playback_open"; playback_id: string; output_epoch: number; source: Source }
  | { type: "playback"; feedback: Playback }
  | {
      type: "control";
      action: "reset" | "reduced_motion" | "destroy" | "stop_effect";
      effect_id?: string;
      enabled?: boolean;
    };

export type Event = {
  event_id: string;
  session_id: string;
  stream_id: string;
  producer_seq: number;
  payload: Payload;
};
