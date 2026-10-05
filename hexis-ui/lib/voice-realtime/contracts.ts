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
      type: "control";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      action: "close" | "barge_in";
      reason: string;
    };

export type RealtimeServerMessage =
  | {
      schema_version: 1;
      type: "ready";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      cloud_session_id: string;
      instructions_hash: string;
    }
  | {
      schema_version: 1;
      type: "audio_chunk";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      response_id: string;
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
      output_epoch: number;
      kind: "text_delta" | "audio_transcript_delta" | "done" | "failed";
      text?: string;
      reason?: string;
    }
  | {
      schema_version: 1;
      type: "event";
      session_id: string;
      media_session_id: string;
      producer_seq: number;
      event: Record<string, unknown>;
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

export type RealtimeSessionResponse = {
  session_id: string;
  media_session_id: string;
  websocket_url: string;
};
