import type { M5TaskSelection } from "../m5-tasks";
import { isEmotion, WORDSET_VERSION, type Emotion } from "../avatar-preview/contracts";

export type RtcDescriptor = {
  schema_version: 2; transport: "webrtc"; session_id: string; media_session_id: string;
  control_url: string; offer_url: string; close_url: string; timeout_ms: number;
  context_version: string; model: string; session_config: Record<string, unknown>;
};
type Envelope = { schema_version: 2; session_id: string; media_session_id: string; producer_seq: number };
type InputIdentity = { input_turn_id: string; input_order: number; turn_id: string };
export type RtcServerMessage = Envelope & (
  | { type: "control_ready" }
  | { type: "ready"; cloud_session_id: string }
  | (InputIdentity & { type: "input_committed" })
  | (InputIdentity & { type: "user_text"; text: string })
  | (InputIdentity & { type: "user_text_saved"; text: string; message_id: string })
  | (InputIdentity & { type: "emotion_result"; label: Emotion; wordset_version: string })
  | { type: "side_task_status"; branch: "transcription" | "transcript" | "emotion"; status: "failed"; code: string }
  | { type: "provider_command"; command_id: string; event: Record<string, unknown> }
  | { type: "assistant_text"; response_id: string; turn_id: string; text: string; input_turn_id?: string; input_order?: number }
  | { type: "response_saved"; response_id: string; turn_id: string; text: string; status: string; playback_scope: "unknown"; input_turn_id?: string; input_order?: number }
  | { type: "m5_selection_opened"; selection: M5TaskSelection }
  | { type: "recall_result"; input_turn_id: string; result: Record<string, unknown> }
  | { type: "error"; code: string; message: string }
);

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("RTC control payload is not an object.");
  return value as Record<string, unknown>;
}
function text(value: unknown): string {
  if (typeof value !== "string" || !value.trim() || value.length > 4096) throw new Error("RTC identity or address is missing.");
  return value;
}

export function parseRtcDescriptor(value: unknown): RtcDescriptor {
  const data = record(value);
  if (data.schema_version !== 2 || data.transport !== "webrtc") throw new Error("RTC session protocol is unsupported.");
  for (const key of ["session_id", "media_session_id", "control_url", "offer_url", "close_url", "context_version", "model"]) text(data[key]);
  const url = new URL(data.control_url as string);
  if (!["ws:", "wss:"].includes(url.protocol) || url.username || url.password) throw new Error("RTC control address is invalid.");
  for (const key of ["offer_url", "close_url"]) {
    const path = data[key] as string;
    if (!path.startsWith("/api/") || path.startsWith("//") || path.includes("\\")) throw new Error("RTC signaling path must be local.");
  }
  if (!/^[0-9a-f]{64}$/.test(data.context_version as string)) throw new Error("RTC prepared context version is invalid.");
  if (typeof data.timeout_ms !== "number" || !Number.isFinite(data.timeout_ms) || data.timeout_ms <= 0) throw new Error("RTC timeout is invalid.");
  const config = record(data.session_config);
  if (record(config.input_audio_transcription).model !== "qwen3-asr-flash-realtime" || record(config.turn_detection).type !== "semantic_vad"
      || typeof config.instructions !== "string" || !config.instructions.trim()
      || !Array.isArray(config.modalities) || !config.modalities.includes("audio") || !config.modalities.includes("text")) {
    throw new Error("RTC session must use prepared context, cloud VAD, and asynchronous input transcription.");
  }
  return data as RtcDescriptor;
}

export function parseRtcMessage(value: unknown): RtcServerMessage {
  const data = record(value);
  if (data.schema_version !== 2 || !Number.isSafeInteger(data.producer_seq) || (data.producer_seq as number) < 0) throw new Error("RTC control envelope is invalid.");
  text(data.session_id); text(data.media_session_id);
  switch (data.type) {
    case "input_committed":
    case "user_text":
    case "user_text_saved":
    case "emotion_result": {
      text(data.input_turn_id); text(data.turn_id);
      if (!Number.isSafeInteger(data.input_order) || (data.input_order as number) < 0) throw new Error("RTC input ordering is invalid.");
      if (data.type === "user_text" || data.type === "user_text_saved") {
        if (typeof data.text !== "string" || !data.text.trim() || data.text.length > 32768) throw new Error("RTC user text is invalid.");
        if (data.type === "user_text_saved") text(data.message_id);
      }
      if (data.type === "emotion_result" && (!isEmotion(data.label) || data.wordset_version !== WORDSET_VERSION)) throw new Error("RTC emotion is invalid.");
      break;
    }
    case "side_task_status":
      if (!["transcription", "transcript", "emotion"].includes(String(data.branch)) || data.status !== "failed") throw new Error("RTC side task status is invalid.");
      text(data.code); break;
    case "control_ready": break;
    case "ready": text(data.cloud_session_id); break;
    case "provider_command": text(data.command_id); record(data.event); break;
    case "assistant_text":
    case "response_saved":
      text(data.response_id); text(data.turn_id);
      if (data.input_turn_id !== undefined) text(data.input_turn_id);
      if (data.input_order !== undefined && (!Number.isSafeInteger(data.input_order) || (data.input_order as number) < 0)) throw new Error("RTC output input order is invalid.");
      if (typeof data.text !== "string") throw new Error("RTC output text is invalid.");
      if (data.type === "response_saved" && (data.playback_scope !== "unknown" || typeof data.status !== "string")) throw new Error("RTC output receipt is invalid.");
      break;
    case "m5_selection_opened": {
      const selection = record(data.selection);
      text(selection.selection_id); text(selection.origin_session_id);
      if (selection.origin_session_id !== data.session_id || selection.state !== "open"
          || !Number.isSafeInteger(selection.version) || (selection.version as number) <= 0
          || typeof selection.message !== "string") throw new Error("RTC task selection is invalid.");
      break;
    }
    case "recall_result": text(data.input_turn_id); record(data.result); break;
    case "error": text(data.code); text(data.message); break;
    default: throw new Error("Unsupported RTC control message.");
  }
  return data as RtcServerMessage;
}
