"use client";

import { Radio, Square, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { AudioCaptureController } from "@/lib/voice-realtime/capture";
import { VoiceRealtimeClient } from "@/lib/voice-realtime/client";
import type { RealtimeServerMessage } from "@/lib/voice-realtime/contracts";
import { NativeOutputQueue } from "@/lib/voice-realtime/output-queue";
import { decodeBase64ToPcm } from "@/lib/voice-playback/native-output-source";
import { NativeOutputPlayer } from "@/lib/voice-playback/native-player";
import type { NativePcmSource } from "@/lib/voice-playback/native-output-source";
import type { NativePlaybackEventRequest } from "@/lib/voice-playback/native-player";

type TalkPhase = "idle" | "connecting" | "listening" | "failed";

async function postPlaybackReceipt(
  endpoint: string,
  event: NativePlaybackEventRequest,
): Promise<void> {
  if (!endpoint) throw new Error("Playback service address is unavailable; playback status remains unconfirmed.");
  let lastError: Error | null = null;
  for (const delayMs of [0, 250, 750]) {
    if (delayMs > 0) await new Promise((resolve) => setTimeout(resolve, delayMs));
    let response: Response;
    try {
      response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(event),
      });
    } catch (error) {
      lastError = error instanceof Error ? error : new Error("Playback receipt request failed.");
      continue;
    }
    if (response.ok) return;
    if (response.status < 500) throw new Error(`Playback receipt was rejected (${response.status}).`);
    lastError = new Error(`Playback receipt could not be saved (${response.status}).`);
  }
  throw new Error(`Playback status is unconfirmed. ${lastError?.message ?? "Retry Talk to continue."}`);
}

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
  const [retryAvailable, setRetryAvailable] = useState(false);
  const clientRef = useRef<VoiceRealtimeClient | null>(null);
  const captureRef = useRef<AudioCaptureController | null>(null);
  const outputsRef = useRef(new Map<string, { queue: NativeOutputQueue; player: NativeOutputPlayer }>());
  const invalidatedResponsesRef = useRef(new Set<string>());
  const activeRef = useRef(false);
  const mountedRef = useRef(true);
  const sessionIdRef = useRef(sessionId);

  const stopOutputs = useCallback(async () => {
    const outputs = [...outputsRef.current.values()];
    outputsRef.current.clear();
    for (const output of outputs) {
      output.player.stop();
      output.queue.close();
      await output.player.destroy().catch(() => undefined);
    }
  }, []);

  const stopOutputsForBargeIn = useCallback(() => {
    const outputs = [...outputsRef.current.entries()];
    outputsRef.current.clear();
    for (const [responseId, output] of outputs) {
      if (invalidatedResponsesRef.current.size >= 256) {
        const oldest = invalidatedResponsesRef.current.values().next().value;
        if (oldest) invalidatedResponsesRef.current.delete(oldest);
      }
      invalidatedResponsesRef.current.add(responseId);
      output.player.stop("barge_in");
      output.queue.close();
      void output.player.destroy().catch(() => undefined);
    }
  }, []);

  const stopTalk = useCallback(async (message?: string) => {
    activeRef.current = false;
    const capture = captureRef.current;
    captureRef.current = null;
    await capture?.close();
    await stopOutputs();
    await clientRef.current?.close();
    if (!mountedRef.current) return;
    setPhase("idle");
    setError(message ?? null);
    setRetryAvailable(Boolean(message));
  }, [stopOutputs]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      activeRef.current = false;
      void captureRef.current?.close();
      void stopOutputs();
      void clientRef.current?.close();
    };
  }, [stopOutputs]);

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
    invalidatedResponsesRef.current.clear();
    setError(null);
    setRetryAvailable(false);
    if (!window.isSecureContext) {
      setPhase("failed");
      setError("Talk needs HTTPS on another device. Run hexis tunnel start, then reopen the private URL.");
      setRetryAvailable(true);
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      setPhase("failed");
      setError("This browser cannot provide a microphone for Talk.");
      setRetryAvailable(true);
      return;
    }

    activeRef.current = true;
    setPhase("connecting");
    if (clientRef.current === null) {
      clientRef.current = new VoiceRealtimeClient({
        onMessage: (message: RealtimeServerMessage) => {
          if (!activeRef.current) return;
          if (message.type === "audio_chunk") {
            if (invalidatedResponsesRef.current.has(message.response_id)) return;
            setError(null);
            setRetryAvailable(false);
            let output = outputsRef.current.get(message.response_id);
            if (!output) {
              const queue = new NativeOutputQueue(16);
              const player = new NativeOutputPlayer({
                identity: {
                  playback_id: message.playback_id,
                  output_epoch: message.output_epoch,
                  response_id: message.response_id,
                  media_session_id: message.media_session_id,
                  turn_id: message.turn_id,
                },
                session_id: message.session_id,
                emit: (event: NativePlaybackEventRequest) => {
                  const endpoint = clientRef.current?.activePlaybackEndpoint ?? "";
                  void postPlaybackReceipt(endpoint, event).then(() => {
                    if (event.action === "end" || event.action === "interrupt") {
                      outputsRef.current.delete(message.response_id);
                    }
                  }).catch((receiptError: unknown) => {
                if (mountedRef.current) {
                  setError(receiptError instanceof Error ? receiptError.message : "Playback receipt could not be saved.");
                  setRetryAvailable(true);
                }
                  });
                },
              });
              player.attach({ epoch: message.output_epoch, source: "real" });
              output = { queue, player };
              outputsRef.current.set(message.response_id, output);
              const source: NativePcmSource = { sample_rate: 24_000, channels: 1, chunks: queue };
              void player.start(source, Math.round(performance.now())).catch((playbackError: unknown) => {
                if (mountedRef.current) {
                  setError(playbackError instanceof Error ? playbackError.message : "Voice output failed.");
                  setRetryAvailable(true);
                }
                queue.fail(playbackError instanceof Error ? playbackError : new Error("Voice output failed."));
              });
            }
            try {
              output.queue.push({
                data: decodeBase64ToPcm(message.pcm_base64),
                segment_id: message.segment_id,
                chunk_seq: message.chunk_seq,
              });
            } catch (outputError) {
              output.queue.fail(outputError instanceof Error ? outputError : new Error("Voice output queue failed."));
              setError(outputError instanceof Error ? outputError.message : "Voice output queue failed.");
              setRetryAvailable(true);
            }
            return;
          }
          if (message.type === "generation") {
            if (invalidatedResponsesRef.current.has(message.response_id)) return;
            const output = outputsRef.current.get(message.response_id);
            if (!output) return;
            if (message.kind === "done") output.queue.close();
            if (message.kind === "failed") {
              output.queue.fail(new Error(message.reason || "Voice generation failed."));
            }
            return;
          }
          if (message.type === "error") {
            setError(message.message);
            setRetryAvailable(message.code !== "speech_not_recognized");
            if (!message.recoverable || message.stage === "client") {
              void stopTalk(message.message);
            }
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
      const stream = clientRef.current.mediaStream;
      if (!stream) throw new Error("The microphone stream ended before capture started.");
      const capture = new AudioCaptureController(
        clientRef.current,
        crypto.randomUUID(),
        (message) => void stopTalk(message),
        stopOutputsForBargeIn,
      );
      captureRef.current = capture;
      await capture.start(stream);
      if (!activeRef.current) return;
      setPhase("listening");
    } catch (startError) {
      if (!activeRef.current) return;
      await captureRef.current?.close();
      captureRef.current = null;
      await clientRef.current?.close();
      setPhase("failed");
      setError(startError instanceof Error ? startError.message : "Talk could not start.");
      setRetryAvailable(true);
    }
  }, [onSessionId, stopOutputs, stopOutputsForBargeIn, stopTalk]);

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
            onClick={() => {
              setError(null);
              setRetryAvailable(false);
            }}
            className="float-right ml-2 rounded p-1 hover:bg-red-50"
          >
            <X size={14} />
          </button>
          <p>{error}</p>
          {retryAvailable ? <button
            type="button"
            onClick={() => void (async () => {
              await stopTalk();
              await startTalk();
            })()}
            className="mt-3 rounded-md bg-[var(--foreground)] px-3 py-2 text-xs font-semibold text-white"
          >
            Retry Talk
          </button> : null}
        </div>
      ) : null}
    </div>
  );
}
