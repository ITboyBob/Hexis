"use client";

import { Radio, Square, X } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { AudioCaptureController, type BargeInTarget, type CapturePauseToken } from "@/lib/voice-realtime/capture";
import { VoiceRealtimeClient } from "@/lib/voice-realtime/client";
import type { RealtimeServerMessage } from "@/lib/voice-realtime/contracts";
import type { M5TaskSelection } from "@/lib/m5-tasks";
import { NativeOutputQueue, REALTIME_OUTPUT_SAMPLE_RATE } from "@/lib/voice-realtime/output-queue";
import { decodeBase64ToPcm } from "@/lib/voice-playback/native-output-source";
import { NativeOutputPlayer } from "@/lib/voice-playback/native-player";
import type { NativePcmSource } from "@/lib/voice-playback/native-output-source";
import type { NativePlaybackEventRequest } from "@/lib/voice-playback/native-player";
import {
  applyLocalPlaybackFeedback,
  applyVoiceBehaviorEvent,
  initialVoiceAvatarState,
  projectVoiceAvatar,
  setVoiceAvatarMediaSession,
} from "@/lib/voice-realtime/avatar-projection";
import { VoiceAvatar } from "./voice-avatar";

type TalkPhase = "idle" | "connecting" | "listening" | "suspended" | "failed" | "closing";
type TalkOutput = {
  queue: NativeOutputQueue;
  player: NativeOutputPlayer;
  failed: boolean;
  speaking: boolean;
  identity: { playback_id: string; output_epoch: number; response_id: string; media_session_id: string };
};
type SelectionPause = {
  client: VoiceRealtimeClient;
  capture: AudioCaptureController;
  token: CapturePauseToken;
  mediaSessionId: string;
};

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
  selectionOpen = false,
  onSelectionOpened,
  onMemoryRecall,
}: {
  disabled?: boolean;
  sessionId?: string | null;
  onSessionId?: (sessionId: string) => void;
  selectionOpen?: boolean;
  onSelectionOpened?: (selection: M5TaskSelection) => void;
  onMemoryRecall?: (message: Extract<RealtimeServerMessage, { type: "memory_recall" }>) => void;
}) {
  const [phase, setPhase] = useState<TalkPhase>("idle");
  const [speaking, setSpeaking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retryAvailable, setRetryAvailable] = useState(false);
  const [avatarState, setAvatarState] = useState(() => initialVoiceAvatarState(sessionId ?? ""));
  const [statusCardHeight, setStatusCardHeight] = useState(0);
  const clientRef = useRef<VoiceRealtimeClient | null>(null);
  const captureRef = useRef<AudioCaptureController | null>(null);
  const outputsRef = useRef(new Map<string, TalkOutput>());
  const invalidatedResponsesRef = useRef(new Set<string>());
  const activeBargeTargetRef = useRef<BargeInTarget | null>(null);
  const activeRef = useRef(false);
  const mountedRef = useRef(true);
  const sessionIdRef = useRef(sessionId);
  const onMemoryRecallRef = useRef(onMemoryRecall);
  useLayoutEffect(() => {
    onMemoryRecallRef.current = onMemoryRecall;
  }, [onMemoryRecall]);
  const selectionPauseRef = useRef<SelectionPause | null>(null);
  const statusCardRef = useRef<HTMLDivElement>(null);
  const avatar = projectVoiceAvatar(avatarState);

  const refreshSpeaking = useCallback(() => {
    if (mountedRef.current) {
      setSpeaking(activeRef.current && [...outputsRef.current.values()].some((output) => output.speaking));
    }
  }, []);

  const invalidateResponse = useCallback((responseId: string) => {
    if (invalidatedResponsesRef.current.size >= 256) {
      const oldest = invalidatedResponsesRef.current.values().next().value;
      if (oldest) invalidatedResponsesRef.current.delete(oldest);
    }
    invalidatedResponsesRef.current.add(responseId);
  }, []);

  const failOutput = useCallback((responseId: string, output: TalkOutput, cause: unknown) => {
    if (output.failed) return;
    output.failed = true;
    const failure = cause instanceof Error ? cause : new Error("Voice output failed. Retry Talk to continue.");
    invalidateResponse(responseId);
    outputsRef.current.delete(responseId);
    if (activeBargeTargetRef.current?.response_id === responseId) activeBargeTargetRef.current = null;
    refreshSpeaking();
    output.queue.fail(failure);
    output.player.stop("error");
    void output.player.destroy().catch(() => undefined);
    if (mountedRef.current) {
      setError(failure.message);
      setRetryAvailable(true);
    }
  }, [invalidateResponse, refreshSpeaking]);

  const stopOutputs = useCallback(async () => {
    const outputs = [...outputsRef.current.values()];
    outputsRef.current.clear();
    activeBargeTargetRef.current = null;
    refreshSpeaking();
    for (const output of outputs) {
      output.player.stop();
      await output.queue[Symbol.asyncIterator]().return?.();
      await output.player.destroy().catch(() => undefined);
    }
  }, [refreshSpeaking]);

  const stopTalk = useCallback(async (message?: string) => {
    activeRef.current = false;
    selectionPauseRef.current = null;
    activeBargeTargetRef.current = null;
    if (mountedRef.current) setPhase("closing");
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

  const stopOutputsForBargeIn = useCallback((target?: BargeInTarget) => {
    if (target) {
      const client = clientRef.current;
      const capture = captureRef.current;
      const capability = client?.activeBargeInCapability;
      const output = outputsRef.current.get(target.response_id);
      if (invalidatedResponsesRef.current.has(target.response_id)) return;
      if (
        output &&
        (output.identity.playback_id !== target.playback_id || output.identity.output_epoch !== target.output_epoch)
      ) return;
      if (!client || !capture || !capability?.supported) {
        void stopTalk("Confirmed voice interruption is unavailable. Stop Talk, then retry after the realtime service is ready.");
        return;
      }

      const localPlaybackState = output?.player.hasStarted ? "started" : "not_started";
      invalidateResponse(target.response_id);
      activeBargeTargetRef.current = null;
      outputsRef.current.delete(target.response_id);
      refreshSpeaking();
      if (output) {
        output.player.stop("barge_in");
        void output.queue[Symbol.asyncIterator]().return?.();
        void output.player.destroy().catch(() => undefined);
      }
      try {
        const receiptPrefix = localPlaybackState === "started"
          ? output?.player.unacknowledgedReceipts(capability.maximum_receipt_prefix_items) ?? []
          : [];
        client.send({
          schema_version: 1,
          type: "barge_in_confirmed",
          session_id: "",
          media_session_id: "",
          producer_seq: 0,
          command_id: crypto.randomUUID(),
          capture_instance_id: capture.id,
          response_id: target.response_id,
          output_epoch: target.output_epoch,
          playback_id: target.playback_id,
          local_playback_state: localPlaybackState,
          receipt_prefix: receiptPrefix,
        });
      } catch (confirmationError) {
        const message = confirmationError instanceof Error
          ? confirmationError.message
          : "Talk could not confirm the interrupted response.";
        void stopTalk(`Talk stopped because the interruption could not be confirmed: ${message}`);
      }
      return;
    }

    const outputs = [...outputsRef.current.entries()];
    outputsRef.current.clear();
    refreshSpeaking();
    for (const [responseId, output] of outputs) {
      invalidateResponse(responseId);
      if (activeBargeTargetRef.current?.response_id === responseId) activeBargeTargetRef.current = null;
      output.player.stop("barge_in");
      void output.queue[Symbol.asyncIterator]().return?.();
      void output.player.destroy().catch(() => undefined);
    }
  }, [invalidateResponse, refreshSpeaking, stopTalk]);

  const pauseForSelection = useCallback(() => {
    if (selectionPauseRef.current) return;
    const client = clientRef.current;
    const capture = captureRef.current;
    const mediaSessionId = client?.activeMediaSessionId;
    if (!activeRef.current || !client || !capture || !mediaSessionId) return;
    const token = capture.pause();
    if (!token) return;
    const paused: SelectionPause = { client, capture, token, mediaSessionId };
    selectionPauseRef.current = paused;
    setPhase("suspended");
    void token.ready.then(() => {
      if (
        selectionPauseRef.current === paused &&
        activeRef.current &&
        clientRef.current === client &&
        client.activeMediaSessionId === mediaSessionId
      ) {
        setPhase("suspended");
      }
    }).catch((pauseError: unknown) => {
      if (selectionPauseRef.current !== paused) return;
      const message = pauseError instanceof Error
        ? pauseError.message
        : "Talk could not pause safely for task selection.";
      setError(message);
      setRetryAvailable(true);
      void stopTalk("Talk stopped because uncommitted input could not be cleared safely.");
    });
  }, [stopTalk]);

  const resumeAfterSelection = useCallback(() => {
    const paused = selectionPauseRef.current;
    if (!paused) return;
    selectionPauseRef.current = null;
    if (
      !activeRef.current ||
      clientRef.current !== paused.client ||
      captureRef.current !== paused.capture ||
      paused.client.activeMediaSessionId !== paused.mediaSessionId
    ) return;
    void paused.capture.resume(paused.token).then(() => {
      if (
        activeRef.current &&
        clientRef.current === paused.client &&
        captureRef.current === paused.capture &&
        paused.client.activeMediaSessionId === paused.mediaSessionId
      ) {
        setPhase("listening");
      }
    }).catch((resumeError: unknown) => {
      const message = resumeError instanceof Error
        ? resumeError.message
        : "Talk could not resume after task selection.";
      setError(message);
      setRetryAvailable(true);
      void stopTalk("Talk stopped because the original capture could not be resumed.");
    });
  }, [stopTalk]);

  useEffect(() => {
    if (selectionOpen) pauseForSelection();
    else resumeAfterSelection();
  }, [selectionOpen, pauseForSelection, resumeAfterSelection]);

  useEffect(() => {
    if (sessionId && avatarState.session_id !== sessionId) {
      setAvatarState(initialVoiceAvatarState(sessionId));
    }
  }, [avatarState.session_id, sessionId]);

  useLayoutEffect(() => {
    const node = statusCardRef.current;
    if (!node) {
      setStatusCardHeight(0);
      return;
    }
    const measure = () => setStatusCardHeight(node.getBoundingClientRect().height);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [phase, error, retryAvailable, speaking]);

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
        toolSurface: "m5",
        onMessage: (message: RealtimeServerMessage) => {
          if (!activeRef.current) return;
          if (message.type === "m5_selection_opened") {
            pauseForSelection();
            onSelectionOpened?.(message.selection);
            return;
          }
          if (message.type === "ready") {
            setAvatarState((current) => {
              const sessionState = current.session_id === message.session_id
                ? current
                : initialVoiceAvatarState(message.session_id);
              return setVoiceAvatarMediaSession(sessionState, message.media_session_id);
            });
            return;
          }
          if (message.type === "memory_recall") {
            if (
              message.session_id !== clientRef.current?.activeSessionId ||
              message.media_session_id !== clientRef.current?.activeMediaSessionId
            ) return;
            onMemoryRecallRef.current?.(message);
            return;
          }
          if (message.type === "event") {
            if (message.media_session_id !== clientRef.current?.activeMediaSessionId) return;
            if (message.event.event_type === "voice_model_call_started") {
              const payload = message.event.payload;
              const responseId = payload.response_id;
              const playbackId = payload.playback_id;
              const outputEpoch = payload.output_epoch;
              if (
                typeof responseId === "string" &&
                typeof playbackId === "string" &&
                typeof outputEpoch === "number" && Number.isSafeInteger(outputEpoch)
              ) {
                activeBargeTargetRef.current = {
                  response_id: responseId,
                  playback_id: playbackId,
                  output_epoch: outputEpoch,
                };
              }
            }
            setAvatarState((current) => {
              const sessionState = current.session_id === message.session_id
                ? current
                : initialVoiceAvatarState(message.session_id, message.media_session_id);
              const mediaState = sessionState.media_session_id === message.media_session_id
                ? sessionState
                : setVoiceAvatarMediaSession(sessionState, message.media_session_id);
              return applyVoiceBehaviorEvent(mediaState, message.event);
            });
            return;
          }
          if (message.type === "audio_chunk") {
            if (invalidatedResponsesRef.current.has(message.response_id)) return;
            activeBargeTargetRef.current = {
              response_id: message.response_id,
              playback_id: message.playback_id,
              output_epoch: message.output_epoch,
            };
            let output = outputsRef.current.get(message.response_id);
            if (!output) {
              setError(null);
              setRetryAvailable(false);
              const queue = new NativeOutputQueue();
              const receiptChain = { current: Promise.resolve() };
              const identity = {
                playback_id: message.playback_id,
                output_epoch: message.output_epoch,
                response_id: message.response_id,
                media_session_id: message.media_session_id,
              };
              const player = new NativeOutputPlayer({
                identity: {
                  ...identity,
                  turn_id: message.turn_id ?? undefined,
                },
                session_id: message.session_id,
                onVisualFeedback: (value) => {
                  if (!activeRef.current || clientRef.current?.activeMediaSessionId !== message.media_session_id) return;
                  setAvatarState((current) => applyLocalPlaybackFeedback(current, value));
                },
                emit: (event: NativePlaybackEventRequest) => {
                  const endpoint = clientRef.current?.activePlaybackEndpoint ?? "";
                  if (event.action === "audible" && output) {
                    output.speaking = true;
                    refreshSpeaking();
                  }
                  if (event.action === "end" || event.action === "interrupt") {
                    invalidateResponse(message.response_id);
                    if (activeBargeTargetRef.current?.response_id === message.response_id) {
                      activeBargeTargetRef.current = null;
                    }
                    outputsRef.current.delete(message.response_id);
                    refreshSpeaking();
                    void queue[Symbol.asyncIterator]().return?.();
                    void player.destroy().catch(() => undefined);
                  }
                  receiptChain.current = receiptChain.current.then(async () => {
                    await postPlaybackReceipt(endpoint, event);
                    player.acknowledgeReceipt(event.producer_seq);
                  }).catch((receiptError: unknown) => {
                    if (mountedRef.current && !output?.failed) {
                      setError(receiptError instanceof Error ? receiptError.message : "Playback receipt could not be saved.");
                      setRetryAvailable(true);
                    }
                  });
                },
              });
              player.attach({ epoch: message.output_epoch, source: "real" });
              output = { queue, player, failed: false, speaking: false, identity };
              outputsRef.current.set(message.response_id, output);
              const source: NativePcmSource = { sample_rate: REALTIME_OUTPUT_SAMPLE_RATE, channels: 1, chunks: queue };
              void player.start(source, Math.round(performance.now())).catch((playbackError: unknown) => {
                if (!activeRef.current || clientRef.current?.activeMediaSessionId !== message.media_session_id) return;
                failOutput(message.response_id, output!, playbackError);
              });
            }
            try {
              output.queue.push({
                data: decodeBase64ToPcm(message.pcm_base64),
                segment_id: message.segment_id,
                chunk_seq: message.chunk_seq,
              });
            } catch (outputError) {
              failOutput(message.response_id, output, outputError);
            }
            return;
          }
          if (message.type === "generation") {
            if (invalidatedResponsesRef.current.has(message.response_id)) return;
            const output = outputsRef.current.get(message.response_id);
            if (!output) {
              if (activeBargeTargetRef.current?.response_id === message.response_id) {
                activeBargeTargetRef.current = null;
              }
              return;
            }
            if (message.kind === "done") output.queue.close();
            if (message.kind === "failed") {
              failOutput(message.response_id, output, new Error(message.reason || "Voice generation failed."));
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
        () => activeBargeTargetRef.current,
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
  }, [onSessionId, onSelectionOpened, pauseForSelection, stopOutputsForBargeIn, stopTalk, failOutput, invalidateResponse, refreshSpeaking]);

  const phaseLabel: Record<TalkPhase, string> = {
    idle: "Talk off",
    connecting: "Connecting Talk",
    listening: speaking ? "Speaking" : "Listening",
    suspended: "Talk paused for task selection",
    failed: "Talk needs attention",
    closing: "Stopping Talk",
  };

  return (
    <div className={`relative flex-none ${phase !== "idle" ? "z-40" : ""}`}>
      {phase !== "idle" && phase !== "closing" ? (
        <VoiceAvatar
          composition={avatar.composition}
          bubble={avatar.bubble}
          emotionLabel={avatar.emotionLabel}
          generation={0}
          bottomOffsetPx={statusCardHeight + 16}
        />
      ) : null}
      {phase === "idle" ? (
        <button
          type="button"
          disabled={disabled || selectionOpen}
          onClick={() => void startTalk()}
          aria-label="Start Talk mode"
          title="Start realtime Talk"
          className="flex h-10 items-center gap-1.5 rounded-md px-2 text-xs font-medium text-[var(--ink-soft)] hover:bg-[var(--outline)] hover:text-[var(--foreground)] disabled:opacity-40"
        >
          <Radio size={16} /> Talk
        </button>
      ) : phase === "closing" ? (
        <button
          type="button"
          disabled
          aria-label="Stopping Talk mode"
          className="flex h-10 items-center gap-1.5 rounded-md bg-[var(--outline)] px-2 text-xs font-medium text-[var(--ink-soft)]"
        >
          Stopping
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
        <div ref={statusCardRef} className="absolute bottom-12 left-0 z-30 w-72 rounded-lg border border-[var(--outline)] bg-white p-3 shadow-xl" data-testid="voice-status-card">
          <p className="text-xs font-semibold">{phaseLabel[phase]}</p>
          <p className="mt-2 text-[11px] text-[var(--ink-soft)]">
            {phase === "listening"
              ? speaking ? "Hexis is speaking. You can interrupt by speaking." : "The microphone stays open while Hexis listens."
              : phase === "suspended"
                ? "The microphone is paused until you confirm or cancel the task selection."
              : phase === "closing"
                ? "The microphone and audio output are closing."
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
