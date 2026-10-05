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
};

export class VoiceRealtimeClient {
  private generation = 0;
  private stream: MediaStream | null = null;
  private socket: WebSocket | null = null;
  private currentState: VoiceRealtimeState = "idle";
  private pendingClose: Promise<void> = Promise.resolve();
  private sessionId: string | null = null;
  private mediaSessionId: string | null = null;

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
        body: JSON.stringify({ session_id: effectiveSessionId }),
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
          this.options.onMessage?.(JSON.parse(String(event.data)));
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
    const socket = this.socket;
    this.socket = null;
    this.stopStream();
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
              producer_seq: 0,
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
    this.socket.send(JSON.stringify(message));
  }

  private stopStream() {
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
  }

  private fail(message: string) {
    this.generation += 1;
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
