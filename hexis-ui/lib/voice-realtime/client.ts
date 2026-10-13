"use client";

import { parseRtcDescriptor, parseRtcMessage, type RtcDescriptor, type RtcServerMessage } from "./rtc-contracts";

export type VoiceRealtimeState = "idle" | "connecting" | "ready" | "closed" | "failed";
type Options = {
  onMessage?: (message: RtcServerMessage) => void;
  onStateChange?: (state: VoiceRealtimeState) => void;
  onError?: (error: Error) => void;
  toolSurface?: "m4" | "m5";
};

function deferred() {
  let resolve!: () => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
  void promise.catch(() => undefined);
  return { promise, resolve, reject };
}

export class VoiceRealtimeClient {
  private currentState: VoiceRealtimeState = "idle";
  private generation = 0;
  private descriptor: RtcDescriptor | null = null;
  private stream: MediaStream | null = null;
  private peer: RTCPeerConnection | null = null;
  private senders: Array<{ sender: RTCRtpSender; track: MediaStreamTrack }> = [];
  private channels = new Set<RTCDataChannel>();
  private socket: WebSocket | null = null;
  private audio: HTMLAudioElement | null = null;
  private abort: AbortController | null = null;
  private startup: ReturnType<typeof deferred> | null = null;
  private controlReady: ReturnType<typeof deferred> | null = null;
  private producerSequence = 0;
  private serverSequence = 0;
  private cloudSessionId: string | null = null;
  private configSent = false;
  private restoring = false;
  private heartbeat: ReturnType<typeof setInterval> | null = null;
  private providerQueue: Promise<void> = Promise.resolve();
  private commandIds = new Set<string>();
  private captureEnabled = true;

  constructor(private readonly options: Options = {}) {}
  get state() { return this.currentState; }
  get activeSessionId() { return this.descriptor?.session_id ?? null; }
  get activeMediaSessionId() { return this.descriptor?.media_session_id ?? null; }

  private setState(state: VoiceRealtimeState) {
    this.currentState = state;
    this.options.onStateChange?.(state);
  }

  async open(sessionId: string | null, onSessionId?: (id: string) => void): Promise<void> {
    if (this.state === "connecting" || this.state === "ready") return;
    const generation = ++this.generation;
    this.abort = new AbortController();
    const abort = this.abort;
    this.startup = deferred();
    const startup = this.startup;
    this.controlReady = deferred();
    this.cloudSessionId = null;
    this.configSent = false;
    this.restoring = false;
    this.producerSequence = 0;
    this.serverSequence = 0;
    this.captureEnabled = true;
    this.commandIds.clear();
    this.providerQueue = Promise.resolve();
    this.setState("connecting");
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || typeof RTCPeerConnection === "undefined") {
        throw new Error("Talk requires microphone access and WebRTC in a secure browser context.");
      }
      const stream = await navigator.mediaDevices.getUserMedia({ audio: {
        channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      } });
      if (generation !== this.generation) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      this.stream = stream;
      if (!sessionId) {
        const created = await fetch("/api/chat/session", { method: "POST", signal: abort.signal });
        const body = await created.json();
        if (generation !== this.generation) return;
        if (!created.ok || typeof body.session_id !== "string") throw new Error(body.error || "Could not create a chat session for Talk.");
        sessionId = body.session_id;
        onSessionId?.(sessionId!);
      }
      const response = await fetch("/api/voice/realtime/sessions", {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: abort.signal,
        body: JSON.stringify({ session_id: sessionId, tool_surface: this.options.toolSurface ?? "m4" }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || body.detail || "Could not prepare the RTC voice session.");
      const descriptor = parseRtcDescriptor(body);
      if (generation !== this.generation) {
        await fetch(descriptor.close_url, { method: "DELETE", signal: AbortSignal.timeout(descriptor.timeout_ms) });
        return;
      }
      if (descriptor.session_id !== sessionId) throw new Error("RTC descriptor belongs to another chat session.");
      this.descriptor = descriptor;
      timer = setTimeout(() => this.fail(new Error("RTC startup timed out. Check the selected provider and retry Talk.")), descriptor.timeout_ms);
      this.socket = new WebSocket(descriptor.control_url);
      this.socket.onmessage = (event) => {
        if (generation !== this.generation) return;
        try { void this.handleControl(parseRtcMessage(JSON.parse(String(event.data)))).catch((error) => this.fail(error)); }
        catch (error) { this.fail(error); }
      };
      this.socket.onerror = () => this.fail(new Error("RTC control connection failed. Check the local API address and retry Talk."));
      this.socket.onclose = () => {
        if (generation === this.generation && this.state !== "closed" && this.state !== "failed") this.fail(new Error("RTC control disconnected. Reopen Talk to continue."));
      };
      await this.controlReady.promise;
      if (generation !== this.generation) return;
      const peer = new RTCPeerConnection({ iceServers: [] });
      this.peer = peer;
      peer.onconnectionstatechange = () => {
        if (generation === this.generation && ["failed", "disconnected", "closed"].includes(peer.connectionState)) {
          this.fail(new Error(`WebRTC connection ${peer.connectionState}. Reopen Talk to continue.`));
        }
      };
      peer.ontrack = (event) => {
        if (generation !== this.generation || event.track.kind !== "audio") return;
        if (!this.audio) {
          this.audio = document.createElement("audio");
          this.audio.autoplay = true;
          this.audio.setAttribute("playsinline", "");
          this.audio.hidden = true;
          document.body.appendChild(this.audio);
        }
        this.audio.srcObject = event.streams[0] ?? new MediaStream([event.track]);
        void this.audio.play().catch(() => {
          if (generation === this.generation) this.fail(new Error("Browser audio playback was blocked. Allow audio and restart Talk."));
        });
      };
      peer.ondatachannel = (event) => this.installChannel(event.channel, generation);
      this.installChannel(peer.createDataChannel("oai-events", { ordered: true }), generation);
      for (const track of stream.getAudioTracks()) {
        const sender = peer.addTrack(track, stream);
        this.senders.push({ sender, track });
        await sender.replaceTrack(null);
        if (generation !== this.generation) return;
      }
      const description = await peer.createOffer();
      if (generation !== this.generation) return;
      await peer.setLocalDescription(description);
      if (generation !== this.generation) return;
      if (peer.iceGatheringState !== "complete") {
        await new Promise<void>((resolve, reject) => {
          const check = () => {
            if (peer.iceGatheringState === "complete") { cleanup(); resolve(); }
          };
          const cancel = () => { cleanup(); reject(new Error("RTC startup was closed.")); };
          const cleanup = () => { peer.removeEventListener("icegatheringstatechange", check); abort.signal.removeEventListener("abort", cancel); };
          peer.addEventListener("icegatheringstatechange", check);
          abort.signal.addEventListener("abort", cancel, { once: true });
          if (abort.signal.aborted) cancel(); else check();
        });
      }
      if (generation !== this.generation) return;
      const offer = peer.localDescription?.sdp;
      if (!offer) throw new Error("Browser returned no SDP offer.");
      const answer = await fetch(descriptor.offer_url, { method: "POST", signal: abort.signal,
        headers: { "Content-Type": "application/sdp" }, body: offer });
      if (generation !== this.generation) return;
      if (!answer.ok) {
        const failure = await answer.json().catch(() => ({}));
        throw new Error(failure.error || failure.detail || `RTC signaling failed (${answer.status}).`);
      }
      const sdp = (await answer.text()).trim().replace(/\r?\n/g, "\r\n") + "\r\n";
      if (generation !== this.generation) return;
      await peer.setRemoteDescription({ type: "answer", sdp });
      if (generation !== this.generation) return;
      await startup.promise;
      if (generation !== this.generation) return;
      this.heartbeat = setInterval(() => {
        try { this.sendControl({ type: "ping" }); } catch (error) { this.fail(error); }
      }, descriptor.timeout_ms / 2);
    } catch (error) {
      const cause = error instanceof Error ? error : new Error("RTC startup failed.");
      const failure = cause.name === "NotAllowedError"
        ? new Error("Microphone permission was denied. Allow it in browser settings, then retry Talk.")
        : cause.name === "NotFoundError"
          ? new Error("No microphone is available. Connect one, then retry Talk.") : cause;
      if (generation === this.generation) this.fail(failure);
      throw failure;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  private installChannel(channel: RTCDataChannel, generation: number) {
    this.channels.add(channel);
    channel.onmessage = (message) => {
      this.providerQueue = this.providerQueue.then(async () => {
        if (generation !== this.generation) return;
        const raw = typeof message.data === "string" ? message.data
          : message.data instanceof ArrayBuffer ? new TextDecoder().decode(message.data)
          : await (message.data as Blob).text();
        if (generation !== this.generation) return;
        if (raw.length > 1_048_576) throw new Error("RTC provider control event is too large.");
        const event = JSON.parse(raw) as Record<string, unknown>;
        this.sendControl({ type: "provider_event", event });
        if (event.type === "session.created") {
          const session = event.session as { id?: string };
          if (typeof session?.id !== "string") throw new Error("RTC cloud session identity is missing.");
          if (this.cloudSessionId && this.cloudSessionId !== session.id) throw new Error("RTC cloud session identity changed.");
          this.cloudSessionId = session.id;
          if (!this.configSent) {
            this.sendProvider({ type: "session.update", event_id: crypto.randomUUID(), session: this.descriptor!.session_config });
            this.configSent = true;
          }
        }
      }).catch((error) => this.fail(error));
    };
    channel.onerror = () => { if (generation === this.generation) this.fail(new Error("RTC DataChannel failed.")); };
    channel.onclose = () => { if (generation === this.generation && this.state !== "closed" && this.state !== "failed") this.fail(new Error("RTC DataChannel closed. Reopen Talk to continue.")); };
  }

  private sendProvider(event: Record<string, unknown>) {
    const channel = [...this.channels].find((candidate) => candidate.readyState === "open");
    if (!channel || channel.bufferedAmount > 1_048_576) throw new Error("RTC DataChannel is unavailable or its queue is full.");
    channel.send(JSON.stringify(event));
  }

  private sendControl(value: Record<string, unknown>) {
    const descriptor = this.descriptor;
    if (!descriptor || this.socket?.readyState !== WebSocket.OPEN || this.socket.bufferedAmount > 1_048_576) throw new Error("RTC host control is unavailable or its queue is full.");
    this.socket.send(JSON.stringify({ ...value, schema_version: 2, session_id: descriptor.session_id,
      media_session_id: descriptor.media_session_id, producer_seq: this.producerSequence++ }));
  }

  private async handleControl(message: RtcServerMessage) {
    const generation = this.generation;
    if (message.session_id !== this.descriptor?.session_id || message.media_session_id !== this.descriptor.media_session_id
        || message.producer_seq !== this.serverSequence++) throw new Error("RTC host message identity or ordering does not match.");
    if (message.type === "control_ready") this.controlReady?.resolve();
    if (message.type === "ready") {
      if (message.cloud_session_id !== this.cloudSessionId) throw new Error("RTC cloud and host identities disagree.");
      if (!this.restoring) {
        this.restoring = true;
        for (const { sender, track } of this.senders) {
          track.enabled = this.captureEnabled;
          await sender.replaceTrack(track);
          if (generation !== this.generation) return;
        }
        this.setState("ready");
        this.startup?.resolve();
      }
    }
    if (message.type === "provider_command") {
      if (this.commandIds.has(message.command_id)) throw new Error("RTC host command was repeated; it was not replayed.");
      this.commandIds.add(message.command_id);
      if (!["conversation.item.create", "response.create"].includes(String(message.event.type))) throw new Error("Unsupported RTC host command.");
      try {
        this.sendProvider(message.event);
        this.sendControl({ type: "command_ack", command_id: message.command_id, status: "sent" });
      } catch (error) {
        this.sendControl({ type: "command_ack", command_id: message.command_id, status: "unknown" });
        throw error;
      }
    }
    if (message.type === "error") throw new Error(message.message);
    this.options.onMessage?.(message);
  }

  setCaptureEnabled(enabled: boolean) {
    this.captureEnabled = enabled;
    this.stream?.getAudioTracks().forEach((track) => { track.enabled = enabled; });
  }

  private fail(value: unknown) {
    if (this.state === "closed" || this.state === "failed") return;
    const error = value instanceof Error ? value : new Error(String(value));
    this.options.onError?.(error);
    void this.cleanup("failed", error);
  }

  private async cleanup(state: "closed" | "failed", error = new Error("Talk was closed.")) {
    ++this.generation;
    this.startup?.reject(error);
    this.controlReady?.reject(error);
    this.abort?.abort();
    this.abort = null;
    if (this.heartbeat) clearInterval(this.heartbeat);
    this.heartbeat = null;
    const descriptor = this.descriptor;
    if (this.socket?.readyState === WebSocket.OPEN) {
      try { this.sendControl({ type: "close", reason: state === "failed" ? "error" : "manual" }); } catch { /* 控制断线由宿主生命周期回收。 */ }
    }
    this.socket?.close(); this.socket = null;
    this.channels.forEach((channel) => channel.close()); this.channels.clear();
    this.peer?.close(); this.peer = null;
    this.stream?.getTracks().forEach((track) => track.stop()); this.stream = null;
    this.senders = [];
    if (this.audio) { this.audio.pause(); this.audio.srcObject = null; this.audio.remove(); this.audio = null; }
    this.descriptor = null;
    this.setState(state);
    if (descriptor) {
      try {
        const result = await fetch(descriptor.close_url, { method: "DELETE", signal: AbortSignal.timeout(descriptor.timeout_ms) });
        if (!result.ok) throw new Error("Host close was not confirmed.");
      } catch {
        // 本地资源已经关闭；不将失去回执误报为宿主已完成回收。
        this.options.onError?.(new Error(`${error.message} Local media is closed, but host cleanup is unconfirmed. Retry after the session lease expires.`));
      }
    }
  }

  close(): Promise<void> { return this.cleanup("closed"); }
  getStats(): Promise<RTCStatsReport> {
    if (!this.peer) return Promise.reject(new Error("RTC connection is unavailable."));
    return this.peer.getStats();
  }
}
