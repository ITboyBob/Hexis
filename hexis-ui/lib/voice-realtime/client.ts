"use client";

import type {
  RealtimeClientMessage,
  RealtimeServerMessage,
  RealtimeSessionResponse,
} from "./contracts";

export type VoiceRealtimeState =
  | "idle"
  | "connecting"
  | "ready"
  | "closed"
  | "failed";

type Options = {
  onMessage?: (message: RealtimeServerMessage) => void;
  onStateChange?: (state: VoiceRealtimeState) => void;
  toolSurface?: "m4" | "m5";
};

export class VoiceRealtimeClient {
  private generation = 0;
  private stream: MediaStream | null = null;
  private socket: WebSocket | null = null;
  private currentState: VoiceRealtimeState = "idle";
  private pendingClose: Promise<void> = Promise.resolve();
  private sessionId: string | null = null;
  private mediaSessionId: string | null = null;
  private playbackEndpoint: string | null = null;
  private producerSequence = 0;
  private pendingCaptureControls = new Map<
    string,
    {
      type: "capture_suspend" | "capture_resume";
      captureInstanceId: string;
      resolve: () => void;
      reject: (error: Error) => void;
      timer: ReturnType<typeof setTimeout>;
    }
  >();

  constructor(private readonly options: Options = {}) {}

  get state(): VoiceRealtimeState {
    return this.currentState;
  }

  async open(
    sessionId: string | null,
    onSessionId?: (sessionId: string) => void,
  ): Promise<void> {
    await this.pendingClose;
    if (this.currentState === "connecting" || this.currentState === "ready") return;
    const generation = ++this.generation;
    this.setState("connecting");

    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      if (generation !== this.generation) {
        this.stopStream();
        return;
      }
      let effectiveSessionId = sessionId;
      if (!effectiveSessionId) {
        const created = await fetch("/api/chat/session", { method: "POST" });
        const payload = (await created.json().catch(() => ({}))) as {
          session_id?: string;
          error?: string;
        };
        if (!created.ok || !payload.session_id) {
          throw new Error(payload.error || "Could not create a chat session for Talk.");
        }
        effectiveSessionId = payload.session_id;
        onSessionId?.(effectiveSessionId);
      }
      if (generation !== this.generation) {
        this.stopStream();
        return;
      }

      const response = await fetch("/api/voice/realtime/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: effectiveSessionId,
          tool_surface: this.options.toolSurface ?? "m4",
        }),
      });
      const payload = (await response.json().catch(() => ({}))) as
        | RealtimeSessionResponse
        | { error?: string };
      if (!response.ok || !("websocket_url" in payload)) {
        throw new Error(
          ("error" in payload && payload.error) ||
            "Could not start the realtime voice service.",
        );
      }
      if (generation !== this.generation) {
        this.stopStream();
        return;
      }
      this.sessionId = payload.session_id;
      this.mediaSessionId = payload.media_session_id;
      this.playbackEndpoint = "/api/voice/playback";
      this.producerSequence = 0;

      const socket = new WebSocket(payload.websocket_url);
      await new Promise<void>((resolve, reject) => {
        socket.onopen = () => resolve();
        socket.onerror = () => reject(new Error("The realtime voice connection failed."));
      });
      if (generation !== this.generation) {
        socket.close();
        this.stopStream();
        return;
      }

      this.socket = socket;
      socket.onmessage = (event) => {
        if (generation !== this.generation) return;
        try {
          const message = JSON.parse(String(event.data)) as RealtimeServerMessage;
          if (message.type === "capture_suspended" || message.type === "capture_resumed") {
            const pending = this.pendingCaptureControls.get(message.control_id);
            if (pending) {
              clearTimeout(pending.timer);
              this.pendingCaptureControls.delete(message.control_id);
              const expectedType = pending.type === "capture_suspend"
                ? "capture_suspended"
                : "capture_resumed";
              if (
                message.type !== expectedType ||
                message.capture_instance_id !== pending.captureInstanceId ||
                message.media_session_id !== this.mediaSessionId
              ) {
                pending.reject(new Error("The voice service acknowledged a different capture identity."));
              } else pending.resolve();
            }
            return;
          }
          this.options.onMessage?.(message);
        } catch {
          this.fail("The voice service sent an invalid message.");
        }
      };
      socket.onclose = () => {
        if (generation !== this.generation || this.currentState !== "ready") return;
        this.fail("The voice connection closed unexpectedly.");
      };
      this.setState("ready");
    } catch (error) {
      if (generation !== this.generation) {
        this.stopStream();
        return;
      }
      this.stopStream();
      this.socket?.close();
      this.socket = null;
      this.setState("failed");
      throw error instanceof Error ? error : new Error("Talk could not start.");
    }
  }

  close(): Promise<void> {
    this.generation += 1;
    this.rejectPendingCaptureControls(new Error("Talk closed before capture control completed."));
    const socket = this.socket;
    this.socket = null;
    this.stopStream();
    this.playbackEndpoint = null;
    this.setState("closed");
    if (socket && socket.readyState <= WebSocket.OPEN) {
      this.pendingClose = new Promise((resolve) => {
        socket.addEventListener("close", () => resolve(), { once: true });
        if (this.sessionId && this.mediaSessionId && socket.readyState === WebSocket.OPEN) {
          socket.send(
            JSON.stringify({
              schema_version: 1,
              type: "control",
              session_id: this.sessionId,
              media_session_id: this.mediaSessionId,
              producer_seq: this.producerSequence++,
              action: "close",
              reason: "manual",
            }),
          );
        } else {
          socket.close();
        }
      });
    } else {
      this.pendingClose = Promise.resolve();
    }
    return this.pendingClose;
  }

  send(message: RealtimeClientMessage): void {
    if (this.currentState !== "ready" || this.socket?.readyState !== WebSocket.OPEN) {
      throw new Error("Talk is not connected.");
    }
    if (this.socket.bufferedAmount > 2_000_000) {
      throw new Error("Talk audio queue is full. Stop Talk and retry when the connection is stable.");
    }
    this.socket.send(
      JSON.stringify({
        ...message,
        session_id: this.sessionId,
        media_session_id: this.mediaSessionId,
        producer_seq: this.producerSequence++,
      }),
    );
  }

  sendControl(action: "barge_in", reason: string): void {
    if (!reason.trim() || reason.length > 200) {
      throw new Error("Talk control reason must contain 1 to 200 characters.");
    }
    this.send({
      schema_version: 1,
      type: "control",
      session_id: "",
      media_session_id: "",
      producer_seq: 0,
      action,
      reason,
    });
  }

  suspendCapture(captureInstanceId: string): Promise<void> {
    return this.sendCaptureControl("capture_suspend", captureInstanceId);
  }

  resumeCapture(captureInstanceId: string): Promise<void> {
    return this.sendCaptureControl("capture_resume", captureInstanceId);
  }

  private sendCaptureControl(
    type: "capture_suspend" | "capture_resume",
    captureInstanceId: string
  ): Promise<void> {
    const controlId = crypto.randomUUID();
    return new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingCaptureControls.delete(controlId);
        reject(new Error(`The voice service did not acknowledge ${type}.`));
      }, 5_000);
      this.pendingCaptureControls.set(controlId, {
        type,
        captureInstanceId,
        resolve,
        reject,
        timer,
      });
      try {
        this.send({
          schema_version: 1,
          type,
          session_id: "",
          media_session_id: "",
          producer_seq: 0,
          capture_instance_id: captureInstanceId,
          control_id: controlId,
        });
      } catch (error: unknown) {
        clearTimeout(timer);
        this.pendingCaptureControls.delete(controlId);
        reject(error instanceof Error ? error : new Error("Talk capture control failed."));
      }
    });
  }

  private rejectPendingCaptureControls(error: Error): void {
    for (const [controlId, pending] of this.pendingCaptureControls) {
      clearTimeout(pending.timer);
      this.pendingCaptureControls.delete(controlId);
      pending.reject(error);
    }
  }

  get mediaStream(): MediaStream | null {
    return this.stream;
  }

  get activeSessionId(): string | null {
    return this.sessionId;
  }

  get activeMediaSessionId(): string | null {
    return this.mediaSessionId;
  }

  get activePlaybackEndpoint(): string | null {
    return this.playbackEndpoint;
  }

  openCapture(captureInstanceId: string): void {
    this.send({
      schema_version: 1,
      type: "capture_open",
      session_id: "",
      media_session_id: "",
      producer_seq: 0,
      capture_instance_id: captureInstanceId,
      sample_rate: 16_000,
      channels: 1,
      sample_format: "s16le",
    });
  }

  sendAudioChunk(
    captureInstanceId: string,
    range: {
      startFrame: number;
      endFrame: number;
      startMonotonicMs: number;
      endMonotonicMs: number;
      pcmBase64: string;
    },
  ): void {
    this.send({
      schema_version: 1,
      type: "audio_chunk",
      session_id: "",
      media_session_id: "",
      producer_seq: 0,
      capture_instance_id: captureInstanceId,
      start_frame: range.startFrame,
      end_frame: range.endFrame,
      start_monotonic_ms: range.startMonotonicMs,
      end_monotonic_ms: range.endMonotonicMs,
      pcm_base64: range.pcmBase64,
    });
  }

  endUtterance(
    captureInstanceId: string,
    range: {
      startFrame: number;
      endFrame: number;
      startMonotonicMs: number;
      endMonotonicMs: number;
      audioSha256: string;
    },
  ): void {
    this.send({
      schema_version: 1,
      type: "utterance_end",
      session_id: "",
      media_session_id: "",
      producer_seq: 0,
      capture_instance_id: captureInstanceId,
      start_frame: range.startFrame,
      end_frame: range.endFrame,
      start_monotonic_ms: range.startMonotonicMs,
      end_monotonic_ms: range.endMonotonicMs,
      audio_sha256: range.audioSha256,
    });
  }

  private stopStream() {
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
  }

  private fail(message: string) {
    this.generation += 1;
    this.rejectPendingCaptureControls(new Error(message));
    this.socket?.close();
    this.socket = null;
    this.stopStream();
    this.setState("failed");
    this.options.onMessage?.({
      schema_version: 1,
      type: "error",
      session_id: this.sessionId ?? "",
      media_session_id: this.mediaSessionId ?? "",
      producer_seq: 0,
      stage: "client",
      code: "connection_failed",
      message,
      recoverable: true,
    });
  }

  private setState(state: VoiceRealtimeState) {
    this.currentState = state;
    this.options.onStateChange?.(state);
  }
}
