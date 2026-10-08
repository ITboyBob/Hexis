export type RealtimeClientMessage =
  | {
      schema_version: 1;
      type: "capture_open";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      capture_instance_id: string;
      sample_rate: 16000;
      channels: 1;
      sample_format: "s16le";
    }
  | {
      schema_version: 1;
      type: "audio_chunk";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      capture_instance_id: string;
      start_frame: number;
      end_frame: number;
      start_monotonic_ms: number;
      end_monotonic_ms: number;
      pcm_base64: string;
    }
  | {
      schema_version: 1;
      type: "utterance_end";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      capture_instance_id: string;
      start_frame: number;
      end_frame: number;
      start_monotonic_ms: number;
      end_monotonic_ms: number;
      audio_sha256: string;
    }
  | {
      schema_version: 1;
      type: "capture_suspend" | "capture_resume";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      capture_instance_id: string;
      control_id: string;
    }
  | {
      schema_version: 1;
      type: "control";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      action: "close" | "barge_in";
      reason: string;
    }
  | {
      schema_version: 1;
      type: "barge_in_confirmed";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      command_id: string;
      capture_instance_id: string;
      response_id: string;
      output_epoch: number;
      playback_id: string;
      local_playback_state: "started" | "not_started";
      receipt_prefix: PlaybackEventReceipt[];
    };

export type PlaybackEventReceipt = {
  action: "open" | "audible" | "progress" | "end" | "interrupt";
  session_id: string;
  response_id: string;
  playback_id: string;
  producer_seq: number;
  output_epoch: number;
  media_session_id: string;
  turn_id?: string | null;
  start_ms?: number | null;
  audible_ms?: number | null;
  played_ms?: number | null;
  interval_ms?: number | null;
  phase?: string | null;
  scope?: "complete" | "partial" | "unplayed" | "unknown" | null;
  reason?: string | null;
};

export type VoiceBehaviorEvent = {
  event_id: string;
  schema_version: 1;
  event_type: string;
  stream_id: string;
  seq: number;
  session_id: string;
  source: "server";
  producer_id: string;
  producer_seq: number;
  channel: string;
  payload: Record<string, unknown>;
  request_id?: string | null;
  stimulus_revision?: number | null;
  input_turn_id?: string | null;
  response_id?: string | null;
  media_session_id?: string | null;
  output_epoch?: number | null;
  turn_id?: string | null;
  task_id?: string | null;
  call_id?: string | null;
  ts?: string | null;
};

export type BargeInCapability = {
  supported: boolean;
  configuration_id: string;
  maximum_receipt_prefix_items: number;
};

export type RealtimeSessionResponse = {
  session_id: string;
  media_session_id: string;
  websocket_url: string;
  barge_in_capability: BargeInCapability;
};

export type RealtimeServerMessage =
  | {
      schema_version: 1;
      type: "ready";
      session_id: string;
      media_session_id: string;
      producer_seq: 0;
      cloud_session_id: string;
      instructions_hash: string;
      barge_in_capability: BargeInCapability;
    }
  | {
      schema_version: 1;
      type: "m5_selection_opened";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      selection: {
        selection_id: string;
        version: number;
        message: string;
        state: "open";
        origin_session_id: string;
      };
    }
  | {
      schema_version: 1;
      type: "capture_suspended";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      capture_instance_id: string;
      control_id: string;
      discarded_range: { start_frame: number; end_frame: number } | null;
    }
  | {
      schema_version: 1;
      type: "capture_resumed";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      capture_instance_id: string;
      control_id: string;
    }
  | {
      schema_version: 1;
      type: "barge_in_result";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      command_id: string;
      capture_instance_id: string;
      response_id: string;
      output_epoch: number;
      playback_id: string;
      status: "accepted" | "playback_only" | "stale_target" | "unknown";
    }
  | {
      schema_version: 1;
      type: "audio_chunk";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      response_id: string;
      playback_id: string;
      turn_id: string | null;
      output_epoch: number;
      segment_id: string;
      segment_index: number;
      chunk_seq: number;
      pcm_base64: string;
    }
  | {
      schema_version: 1;
      type: "generation";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      response_id: string;
      playback_id: string;
      turn_id: string | null;
      output_epoch: number;
      kind: "text_delta" | "audio_transcript_delta";
      text: string | null;
    }
  | {
      schema_version: 1;
      type: "generation";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      response_id: string;
      playback_id: string;
      turn_id: string | null;
      output_epoch: number;
      kind: "done" | "failed";
      reason: string | null;
    }
  | {
      schema_version: 1;
      type: "event";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      event: VoiceBehaviorEvent;
    }
  | {
      schema_version: 1;
      type: "error";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      stage: string;
      code: string;
      message: string;
      recoverable: boolean;
    };

type MessageRecord = Record<string, unknown>;

function isMessageRecord(value: unknown): value is MessageRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireExactFields(
  value: MessageRecord,
  required: readonly string[],
  optional: readonly string[] = [],
): void {
  const allowed = new Set([...required, ...optional]);
  for (const key of required) {
    if (!Object.prototype.hasOwnProperty.call(value, key)) {
      throw new Error(`The voice service message is missing ${key}.`);
    }
  }
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) throw new Error(`The voice service message contains unsupported ${key}.`);
  }
}

function requireString(value: MessageRecord, key: string, nullable = false): void {
  const field = value[key];
  if (nullable && field === null) return;
  if (typeof field !== "string" || field.length === 0) {
    throw new Error(`The voice service message has an invalid ${key}.`);
  }
}

function requireInteger(value: MessageRecord, key: string, minimum = 0): void {
  const field = value[key];
  if (typeof field !== "number" || !Number.isSafeInteger(field) || field < minimum) {
    throw new Error(`The voice service message has an invalid ${key}.`);
  }
}

function validateEnvelope(value: MessageRecord, type: string): void {
  if (value.schema_version !== 1 || value.type !== type) {
    throw new Error("The voice service message has an unsupported schema or type.");
  }
  requireString(value, "session_id");
  requireString(value, "media_session_id");
  requireInteger(value, "producer_seq");
}

function parseBargeInCapability(value: unknown): BargeInCapability {
  if (!isMessageRecord(value)) throw new Error("The voice service has an invalid barge-in capability.");
  requireExactFields(value, ["supported", "configuration_id", "maximum_receipt_prefix_items"]);
  if (typeof value.supported !== "boolean") throw new Error("The voice service capability has an invalid supported flag.");
  requireString(value, "configuration_id");
  requireInteger(value, "maximum_receipt_prefix_items", 1);
  return value as unknown as BargeInCapability;
}

export function parseRealtimeSessionResponse(value: unknown): RealtimeSessionResponse {
  if (!isMessageRecord(value)) throw new Error("The voice service returned an invalid session description.");
  requireExactFields(value, [
    "session_id", "media_session_id", "websocket_url", "barge_in_capability",
  ]);
  requireString(value, "session_id");
  requireString(value, "media_session_id");
  requireString(value, "websocket_url");
  let url: URL;
  try {
    url = new URL(value.websocket_url as string);
  } catch {
    throw new Error("The voice service returned an invalid WebSocket URL.");
  }
  if (url.protocol !== "ws:" && url.protocol !== "wss:") {
    throw new Error("The voice service returned an invalid WebSocket URL.");
  }
  parseBargeInCapability(value.barge_in_capability);
  return value as unknown as RealtimeSessionResponse;
}

export function parseRealtimeServerMessage(value: unknown): RealtimeServerMessage {
  if (!isMessageRecord(value) || typeof value.type !== "string") {
    throw new Error("The voice service sent an invalid message.");
  }
  const type = value.type;

  if (type === "ready") {
    requireExactFields(value, [
      "schema_version", "type", "session_id", "media_session_id", "producer_seq",
      "cloud_session_id", "instructions_hash", "barge_in_capability",
    ]);
    validateEnvelope(value, type);
    if (value.producer_seq !== 0) throw new Error("The voice service ready message must use producer_seq 0.");
    requireString(value, "cloud_session_id");
    requireString(value, "instructions_hash");
    if (!/^[0-9a-f]{64}$/.test(value.instructions_hash as string)) {
      throw new Error("The voice service message has an invalid instructions_hash.");
    }
    parseBargeInCapability(value.barge_in_capability);
  } else if (type === "audio_chunk") {
    requireExactFields(value, [
      "schema_version", "type", "session_id", "media_session_id", "producer_seq",
      "response_id", "playback_id", "turn_id", "output_epoch", "segment_id",
      "segment_index", "chunk_seq", "pcm_base64",
    ]);
    validateEnvelope(value, type);
    for (const key of ["response_id", "playback_id", "segment_id", "pcm_base64"]) requireString(value, key);
    requireString(value, "turn_id", true);
    requireInteger(value, "output_epoch");
    requireInteger(value, "segment_index");
    requireInteger(value, "chunk_seq", 1);
  } else if (type === "generation") {
    const common = [
      "schema_version", "type", "session_id", "media_session_id", "producer_seq",
      "response_id", "playback_id", "turn_id", "output_epoch", "kind",
    ];
    requireExactFields(
      value,
      [...common, value.kind === "text_delta" || value.kind === "audio_transcript_delta" ? "text" : "reason"],
    );
    validateEnvelope(value, type);
    requireString(value, "response_id");
    requireString(value, "playback_id");
    requireString(value, "turn_id", true);
    requireInteger(value, "output_epoch");
    if (value.kind === "text_delta" || value.kind === "audio_transcript_delta") {
      requireString(value, "text", true);
    } else if (value.kind === "done" || value.kind === "failed") {
      requireString(value, "reason", true);
    } else {
      throw new Error("The voice service message has an unsupported generation kind.");
    }
  } else if (type === "m5_selection_opened") {
    requireExactFields(value, [
      "schema_version", "type", "session_id", "media_session_id", "producer_seq", "selection",
    ]);
    validateEnvelope(value, type);
    if (!isMessageRecord(value.selection)) throw new Error("The voice service message has an invalid selection.");
    requireExactFields(value.selection, [
      "selection_id", "version", "message", "state", "origin_session_id",
    ]);
    requireString(value.selection, "selection_id");
    requireInteger(value.selection, "version", 1);
    requireString(value.selection, "message");
    requireString(value.selection, "origin_session_id");
    if (value.selection.state !== "open") throw new Error("The voice service message has an invalid selection state.");
  } else if (type === "capture_suspended") {
    requireExactFields(value, [
      "schema_version", "type", "session_id", "media_session_id", "producer_seq",
      "capture_instance_id", "control_id", "discarded_range",
    ]);
    validateEnvelope(value, type);
    requireString(value, "capture_instance_id");
    requireString(value, "control_id");
    if (value.discarded_range !== null) {
      if (!isMessageRecord(value.discarded_range)) throw new Error("The voice service message has an invalid discarded_range.");
      requireExactFields(value.discarded_range, ["start_frame", "end_frame"]);
      requireInteger(value.discarded_range, "start_frame");
      requireInteger(value.discarded_range, "end_frame");
    }
  } else if (type === "capture_resumed") {
    requireExactFields(value, [
      "schema_version", "type", "session_id", "media_session_id", "producer_seq",
      "capture_instance_id", "control_id",
    ]);
    validateEnvelope(value, type);
    requireString(value, "capture_instance_id");
    requireString(value, "control_id");
  } else if (type === "barge_in_result") {
    requireExactFields(value, [
      "schema_version", "type", "session_id", "media_session_id", "producer_seq",
      "command_id", "capture_instance_id", "response_id", "output_epoch",
      "playback_id", "status",
    ]);
    validateEnvelope(value, type);
    for (const key of ["command_id", "capture_instance_id", "response_id", "playback_id"]) {
      requireString(value, key);
    }
    requireInteger(value, "output_epoch");
    if (!["accepted", "playback_only", "stale_target", "unknown"].includes(String(value.status))) {
      throw new Error("The voice service message has an unsupported barge-in result status.");
    }
  } else if (type === "event") {
    requireExactFields(value, [
      "schema_version", "type", "session_id", "media_session_id", "producer_seq", "event",
    ]);
    validateEnvelope(value, type);
    if (!isMessageRecord(value.event)) throw new Error("The voice service message has an invalid event.");
  } else if (type === "error") {
    requireExactFields(value, [
      "schema_version", "type", "session_id", "media_session_id", "producer_seq",
      "stage", "code", "message", "recoverable",
    ]);
    validateEnvelope(value, type);
    for (const key of ["stage", "code", "message"]) requireString(value, key);
    if (typeof value.recoverable !== "boolean") throw new Error("The voice service message has an invalid recoverable flag.");
  } else {
    throw new Error("The voice service sent an unsupported message type.");
  }

  return value as unknown as RealtimeServerMessage;
}
