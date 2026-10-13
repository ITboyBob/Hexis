"use client";

import { Radio, Square } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { VoiceRealtimeClient, type VoiceRealtimeState } from "@/lib/voice-realtime/client";
import type { RtcServerMessage } from "@/lib/voice-realtime/rtc-contracts";
import type { RealtimeServerMessage, VoiceMemoryPreview } from "@/lib/voice-realtime/contracts";
import type { M5TaskSelection } from "@/lib/m5-tasks";
import { initialVoiceAvatarState, projectVoiceAvatar } from "@/lib/voice-realtime/avatar-projection";
import { VoiceAvatar } from "./voice-avatar";

// user 变体仅供旧历史消费者的类型兼容；RTC 不生产用户文字更新。
export type VoiceChatText =
  | { sessionId: string; kind: "user"; inputTurnId: string; text: string }
  | { sessionId: string; kind: "assistant"; turnId: string; responseId: string; text: string; final?: boolean };

type Props = {
  disabled?: boolean; sessionId?: string | null; onSessionId?: (sessionId: string) => void;
  selectionOpen?: boolean; onSelectionOpened?: (selection: M5TaskSelection) => void;
  onMemoryRecall?: (message: Extract<RealtimeServerMessage, { type: "memory_recall" }>) => void;
  onChatText?: (message: VoiceChatText) => void;
  onVoiceTurnReady?: (turn: { sessionId: string; turnId: string }) => void;
};

export function TalkMode(props: Props) {
  const [state, setState] = useState<VoiceRealtimeState>("idle");
  const [error, setError] = useState<string | null>(null);
  const clientRef = useRef<VoiceRealtimeClient | null>(null);
  const propsRef = useRef(props);
  useLayoutEffect(() => { propsRef.current = props; }, [props]);
  const mounted = useRef(true);
  const activeSession = useRef(props.sessionId ?? null);
  const active = state === "connecting" || state === "ready";
  const avatar = projectVoiceAvatar(initialVoiceAvatarState(props.sessionId ?? ""));

  const stop = useCallback(async () => {
    const client = clientRef.current;
    clientRef.current = null;
    if (mounted.current) setState("closed");
    if (client) await client.close();
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; void stop(); };
  }, [stop]);
  useEffect(() => {
    if (clientRef.current && (props.sessionId ?? null) !== activeSession.current) void stop();
  }, [props.sessionId, stop]);
  useEffect(() => {
    clientRef.current?.setCaptureEnabled(!props.selectionOpen);
  }, [props.selectionOpen]);

  const start = async () => {
    if (clientRef.current || propsRef.current.selectionOpen) return;
    setError(null);
    const handleMessage = (message: RtcServerMessage) => {
      const client = clientRef.current;
      if (!client || message.session_id !== client.activeSessionId || message.media_session_id !== client.activeMediaSessionId) return;
      const callbacks = propsRef.current;
      if (message.type === "assistant_text" || message.type === "response_saved") {
        if (message.text) callbacks.onChatText?.({ sessionId: message.session_id, kind: "assistant", turnId: message.turn_id,
          responseId: message.response_id, text: message.text, final: message.type === "response_saved" });
        if (message.type === "response_saved") callbacks.onVoiceTurnReady?.({ sessionId: message.session_id, turnId: message.turn_id });
      } else if (message.type === "m5_selection_opened") {
        client.setCaptureEnabled(false);
        callbacks.onSelectionOpened?.(message.selection);
      } else if (message.type === "recall_result") {
        const value = message.result;
        if (Array.isArray(value.memories) && typeof value.elapsed_ms === "number") {
          const memories = value.memories.filter((m): m is VoiceMemoryPreview => !!m && typeof m === "object"
            && typeof m.id === "string" && typeof m.content === "string" && typeof m.type === "string");
          callbacks.onMemoryRecall?.({ schema_version: 1, type: "memory_recall", session_id: message.session_id,
            media_session_id: message.media_session_id, producer_seq: message.producer_seq,
            input_turn_id: message.input_turn_id, stimulus_revision: 0, count: memories.length,
            memories, hydrate_ms: value.elapsed_ms, recall_performed: true });
        }
      }
    };
    const client = new VoiceRealtimeClient({ toolSurface: "m5", onMessage: handleMessage,
      onStateChange: (next) => { if (mounted.current && clientRef.current === client) setState(next); },
      onError: (cause) => { if (mounted.current && (!clientRef.current || clientRef.current === client)) setError(cause.message); },
    });
    clientRef.current = client;
    activeSession.current = propsRef.current.sessionId ?? null;
    try {
      await client.open(propsRef.current.sessionId ?? null, (sessionId) => {
        if (clientRef.current !== client) return;
        activeSession.current = sessionId;
        propsRef.current.onSessionId?.(sessionId);
      });
      client.setCaptureEnabled(!propsRef.current.selectionOpen);
    } catch (cause) {
      if (clientRef.current !== client) return;
      if (clientRef.current === client) clientRef.current = null;
      if (mounted.current) {
        setState("failed");
        setError(cause instanceof Error ? cause.message : "RTC Talk could not start.");
      }
    }
  };

  return <div className="relative flex-none">
    {active ? <VoiceAvatar composition={avatar.composition} generation={0} bottomOffsetPx={80} /> : null}
    {active ? <button type="button" aria-label="Stop Talk mode" aria-pressed="true"
      onClick={() => void stop()} className="flex h-10 items-center gap-1.5 rounded-md bg-red-50 px-2 text-xs font-semibold text-red-700">
      <Square size={14} /> Stop
    </button> : <button type="button" aria-label="Start Talk mode" disabled={props.disabled || props.selectionOpen}
      onClick={() => void start()} className="flex h-10 items-center gap-1.5 rounded-md px-2 text-xs font-medium disabled:opacity-40">
      <Radio size={16} /> Talk
    </button>}
    {active ? <div data-testid="voice-status-card" className="absolute bottom-12 left-0 z-30 w-72 rounded-lg border bg-white p-3 shadow-xl">
      <p className="text-xs font-semibold">{state === "connecting" ? "Connecting Talk" : props.selectionOpen ? "Talk paused for task selection" : "Talk connected"}</p>
      <p className="mt-2 text-[11px]">{state === "connecting" ? "Grant microphone permission to start the connection."
        : props.selectionOpen ? "Choose a task or cancel to resume the microphone." : "The microphone stays open until you stop Talk."}</p>
    </div> : null}
    {error ? <div role="alert" className="absolute bottom-12 left-0 z-50 w-80 rounded-lg border border-red-200 bg-white p-3 text-xs text-red-700 shadow-xl">
      <p>{error}</p>
      <button type="button" onClick={() => { void stop().then(start); }} className="mt-3 rounded border px-3 py-2">Retry Talk</button>
    </div> : null}
  </div>;
}
