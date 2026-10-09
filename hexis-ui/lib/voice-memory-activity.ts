import type { RealtimeServerMessage } from "./voice-realtime/contracts";

export type VoiceMemoryRecallMessage = Extract<RealtimeServerMessage, { type: "memory_recall" }>;

export function voiceMemoryRecallLog(message: VoiceMemoryRecallMessage, ts = Date.now()) {
  return {
    id: `voice-memory:${message.input_turn_id}:${message.stimulus_revision}`,
    category: "memory" as const,
    title: "Memory Recall",
    detail: message.recall_performed
      ? `Retrieved ${message.count} relevant memories`
      : "Memory recall was not performed by the selected policy",
    raw: {
      kind: "memory_recall",
      count: message.count,
      memories: message.memories,
      hydrate_ms: message.hydrate_ms,
      recall_performed: message.recall_performed,
    },
    ts,
    sessionId: message.session_id,
  };
}
