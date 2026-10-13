import type { M5TaskSelection } from "../m5-tasks";

export type RtcDescriptor = {
  schema_version: 2; transport: "webrtc"; session_id: string; media_session_id: string;
  control_url: string; offer_url: string; close_url: string; timeout_ms: number;
  context_version: string; model: string; session_config: Record<string, unknown>;
};
type Envelope = { schema_version: 2; session_id: string; media_session_id: string; producer_seq: number };
export type RtcServerMessage = Envelope & (
  | { type: "control_ready" }
  | { type: "ready"; cloud_session_id: string }
  | { type: "provider_command"; command_id: string; event: Record<string, unknown> }
  | { type: "assistant_text"; response_id: string; turn_id: string; text: string }
  | { type: "response_saved"; response_id: string; turn_id: string; text: string; status: string; playback_scope: "unknown" }
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
  if (config.input_audio_transcription !== null || record(config.turn_detection).type !== "semantic_vad"
      || typeof config.instructions !== "string" || !config.instructions.trim()
      || !Array.isArray(config.modalities) || !config.modalities.includes("audio") || !config.modalities.includes("text")) {
    throw new Error("RTC session must use prepared context, cloud VAD, and disabled input transcription.");
  }
  return data as RtcDescriptor;
}

export function parseRtcMessage(value: unknown): RtcServerMessage {
  const data = record(value);
  if (data.schema_version !== 2 || !Number.isSafeInteger(data.producer_seq) || (data.producer_seq as number) < 0) throw new Error("RTC control envelope is invalid.");
  text(data.session_id); text(data.media_session_id);
  switch (data.type) {
    case "control_ready": break;
    case "ready": text(data.cloud_session_id); break;
    case "provider_command": text(data.command_id); record(data.event); break;
    case "assistant_text":
    case "response_saved":
      text(data.response_id); text(data.turn_id);
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
