"use client";

import { Radio, Square, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { VoiceRealtimeClient } from "@/lib/voice-realtime/client";
import type { RealtimeServerMessage } from "@/lib/voice-realtime/contracts";

type TalkPhase = "idle" | "connecting" | "listening" | "failed";

export function TalkMode({
  disabled,
  sessionId,
  onSessionId,
}: {
  disabled?: boolean;
  sessionId?: string | null;
  onSessionId?: (sessionId: string) => void;
}) {
  const [phase, setPhase] = useState<TalkPhase>("idle");
  const [error, setError] = useState<string | null>(null);
  const clientRef = useRef<VoiceRealtimeClient | null>(null);
  const activeRef = useRef(false);
  const mountedRef = useRef(true);
  const sessionIdRef = useRef(sessionId);

  const stopTalk = useCallback(async (message?: string) => {
    activeRef.current = false;
    await clientRef.current?.close();
    if (!mountedRef.current) return;
    setPhase("idle");
    setError(message ?? null);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      activeRef.current = false;
      void clientRef.current?.close();
    };
  }, []);

  useEffect(() => {
    if (
      activeRef.current &&
      sessionIdRef.current !== null &&
      sessionIdRef.current !== undefined &&
      sessionIdRef.current !== sessionId
    ) {
      void stopTalk("Talk stopped because the conversation changed. Press Talk to continue.");
    }
    sessionIdRef.current = sessionId;
  }, [sessionId, stopTalk]);

  useEffect(() => {
    const onVisibility = () => {
      if (document.hidden && activeRef.current) {
        void stopTalk("Talk stopped when this page left the foreground. Press Talk to continue.");
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [stopTalk]);

  const startTalk = useCallback(async () => {
    if (activeRef.current) return;
    setError(null);
    if (!window.isSecureContext) {
      setPhase("failed");
      setError("Talk needs HTTPS on another device. Run hexis tunnel start, then reopen the private URL.");
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      setPhase("failed");
      setError("This browser cannot provide a microphone for Talk.");
      return;
    }

    activeRef.current = true;
    setPhase("connecting");
    if (clientRef.current === null) {
      clientRef.current = new VoiceRealtimeClient({
        onMessage: (message: RealtimeServerMessage) => {
          if (!activeRef.current) return;
          if (message.type === "error") {
            setError(message.message);
            if (!message.recoverable) stopTalk(message.message);
          }
        },
        onStateChange: (state) => {
          if (!activeRef.current) return;
          if (state === "failed") setPhase("failed");
        },
      });
    }

    try {
      await clientRef.current.open(sessionIdRef.current ?? null, onSessionId);
      if (!activeRef.current) return;
      setPhase("listening");
    } catch (startError) {
      if (!activeRef.current) return;
      setPhase("failed");
      setError(startError instanceof Error ? startError.message : "Talk could not start.");
    }
  }, [onSessionId, stopTalk]);

  const phaseLabel: Record<TalkPhase, string> = {
    idle: "Talk off",
    connecting: "Connecting Talk",
    listening: "Listening",
    failed: "Talk needs attention",
  };

  return (
    <div className="relative flex-none">
      {phase === "idle" ? (
        <button
          type="button"
          disabled={disabled}
          onClick={() => void startTalk()}
          aria-label="Start Talk mode"
          title="Start realtime Talk"
          className="flex h-10 items-center gap-1.5 rounded-md px-2 text-xs font-medium text-[var(--ink-soft)] hover:bg-[var(--outline)] hover:text-[var(--foreground)] disabled:opacity-40"
        >
          <Radio size={16} /> Talk
        </button>
      ) : (
        <button
          type="button"
          onClick={() => void stopTalk()}
          aria-label="Stop Talk mode"
          aria-pressed="true"
          title="Stop realtime Talk"
          className="flex h-10 items-center gap-1.5 rounded-md bg-red-50 px-2 text-xs font-semibold text-red-700"
        >
          <Square size={14} /> Stop
        </button>
      )}
      {phase !== "idle" ? (
        <div className="absolute bottom-12 left-0 z-30 w-72 rounded-lg border border-[var(--outline)] bg-white p-3 shadow-xl">
          <p className="text-xs font-semibold">{phaseLabel[phase]}</p>
          <p className="mt-2 text-[11px] text-[var(--ink-soft)]">
            {phase === "listening"
              ? "The microphone stays open while Hexis listens."
              : "Talk is starting. Grant microphone permission if your browser asks."}
          </p>
        </div>
      ) : null}
      {error ? (
        <div
          role="alert"
          className="absolute bottom-12 left-0 z-50 w-80 rounded-lg border border-red-200 bg-white p-3 text-xs text-red-700 shadow-xl"
        >
          <button
            type="button"
            aria-label="Dismiss Talk mode error"
            onClick={() => setError(null)}
            className="float-right ml-2 rounded p-1 hover:bg-red-50"
          >
            <X size={14} />
          </button>
          <p>{error}</p>
          <button
            type="button"
            onClick={() => void startTalk()}
            className="mt-3 rounded-md bg-[var(--foreground)] px-3 py-2 text-xs font-semibold text-white"
          >
            Retry Talk
          </button>
        </div>
      ) : null}
    </div>
  );
}
