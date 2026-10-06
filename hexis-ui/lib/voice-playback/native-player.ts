import type { Playback } from "../avatar-preview/contracts";
import { LocalPlayer } from "../avatar-preview/player";
import { isNativePcmSource, type NativePcmSource } from "./native-output-source";

export type NativePlaybackIdentity = {
  playback_id: string;
  output_epoch: number;
  response_id: string;
  media_session_id: string;
  turn_id?: string;
};

type NativePlaybackAction =
  | "open"
  | "audible"
  | "progress"
  | "end"
  | "interrupt";

export type NativePlaybackEventRequest = {
  action: NativePlaybackAction;
  session_id: string;
  playback_id: string;
  response_id: string;
  producer_seq: number;
  output_epoch: number;
  media_session_id: string;
  turn_id?: string;
  start_ms?: number;
  audible_ms?: number;
  played_ms?: number;
  interval_ms?: number;
  phase?: Playback["phase"];
  scope?: "complete" | "partial" | "unplayed" | "unknown";
  reason?: string;
};

type NativeOutputPlayerOptions = {
  identity: NativePlaybackIdentity;
  session_id: string;
  emit: (event: NativePlaybackEventRequest) => void;
  audio?: HTMLAudioElement;
  createContext?: () => AudioContext;
};

class NativePcmPlayer {
  private context?: AudioContext;
  private analyser?: AnalyserNode;
  private samples?: Uint8Array<ArrayBuffer>;
  private sources = new Set<AudioBufferSourceNode>();
  private raf = 0;
  private audibleTimer?: ReturnType<typeof setTimeout>;
  private dead = false;
  private started = false;
  private opened = false;
  private closed = true;
  private playing = false;
  private sourceComplete = false;
  private scheduledUntil = 0;
  private current?: { id: string; epoch: number; response_id: string; media_session_id: string };
  private lastFeedbackAt = 0;
  private startAt = 0;

  constructor(
    private readonly emit: (value: Playback) => void,
    private readonly createContext: () => AudioContext,
    private readonly onStarted: () => void,
  ) {}

  async start(
    source: NativePcmSource,
    identity: { id: string; epoch: number; response_id: string; media_session_id: string },
  ): Promise<void> {
    if (this.dead || this.started) {
      throw new Error("每个播放窗口只能启动一次；请创建新播放器实例和epoch");
    }
    this.started = true;
    this.current = identity;
    this.closed = false;
    try {
      this.context = this.createContext();
      this.analyser = this.context.createAnalyser();
      this.samples = new Uint8Array(this.analyser.fftSize) as Uint8Array<ArrayBuffer>;
      this.analyser.connect(this.context.destination);
      await this.context.resume();
      if (this.dead || this.closed || this.current !== identity) return;

      let nextTime = this.context.currentTime + 0.02;
      let first = true;
      let lastSource: AudioBufferSourceNode | undefined;
      for await (const chunk of source.chunks) {
        if (this.dead || this.closed || this.current !== identity) return;
        if (source.channels !== 1) throw new Error("PCM播放仅支持mono");
        const bytes = new Uint8Array(chunk.data);
        if (bytes.byteLength === 0 || bytes.byteLength % 2 !== 0) {
          throw new Error("PCM chunk必须是16-bit mono帧");
        }
        const frames = bytes.byteLength / 2;
        const view = new DataView(chunk.data);
        // 大块也拆成至多100ms的节点，网络分块大小不能扩大WebAudio调度窗口。
        const sliceFrames = Math.max(1, Math.floor(source.sample_rate / 10));
        for (let offset = 0; offset < frames; offset += sliceFrames) {
          await this.waitForScheduleWindow(nextTime);
          if (this.dead || this.closed || this.current !== identity) return;
          const count = Math.min(sliceFrames, frames - offset);
          const buffer = this.context.createBuffer(1, count, source.sample_rate);
          const channel = buffer.getChannelData(0);
          for (let index = 0; index < count; index += 1) {
            channel[index] = view.getInt16((offset + index) * 2, true) / 32768;
          }
          const node = this.context.createBufferSource();
          node.buffer = buffer;
          node.connect(this.analyser);
          node.start(nextTime);
          const chunkStart = nextTime;
          nextTime += count / source.sample_rate;
          this.scheduledUntil = nextTime;
          node.onended = () => {
            this.sources.delete(node);
            node.disconnect();
            if (this.sourceComplete && lastSource === node) this.feedback("drained");
          };
          if (first) {
            this.startAt = chunkStart;
            this.opened = true;
            this.onStarted();
            this.audibleTimer = setTimeout(() => {
              this.audibleTimer = undefined;
              this.feedback("playing");
            }, 20);
            first = false;
          }
          lastSource = node;
          this.sources.add(node);
        }
      }
      if (!lastSource) throw new Error("PCM source不包含音频帧");
      this.sourceComplete = true;
      if (this.context.currentTime >= this.scheduledUntil) this.feedback("drained");
    } catch (error) {
      if (this.dead || this.closed) return;
      this.clearScheduledSources();
      if (this.audibleTimer !== undefined) clearTimeout(this.audibleTimer);
      if (this.opened && !this.dead && !this.closed && this.current === identity) {
        this.feedback("error");
      }
      const cause = error instanceof Error ? error : new Error(String(error));
      throw new Error(`语音播放失败：${cause.message}；请重试 Talk。`, { cause });
    }
  }

  stop(reason = "player-stop"): void {
    if (this.dead) return;
    const previous = this.current;
    const playedMs = this.context
      ? Math.round(
          Math.max(
            0,
            Math.min(
              this.context.currentTime - this.startAt,
              this.scheduledUntil - this.startAt,
            ),
          ) * 1000,
        )
      : 0;
    this.current = undefined;
    this.closed = true;
    this.playing = false;
    if (this.audibleTimer !== undefined) clearTimeout(this.audibleTimer);
    cancelAnimationFrame(this.raf);
    this.clearScheduledSources();
    if (previous && this.opened) this.emitFeedback(previous, "cancel", 0, reason, playedMs);
  }

  async destroy(): Promise<void> {
    if (this.dead) return;
    this.stop();
    this.dead = true;
    this.sources.clear();
    try {
      await this.context?.close();
    } catch {
      throw new Error("播放器视觉反馈已停止，但AudioContext关闭失败；可刷新预览后重试");
    }
  }

  private feedback(phase: Playback["phase"]): void {
    if (this.dead || this.closed || !this.current) return;
    this.playing = phase === "playing";
    if (["drained", "cancel", "error"].includes(phase)) this.closed = true;
    cancelAnimationFrame(this.raf);
    this.emitFeedback(this.current, phase, 0);
    if (this.playing) this.frame();
  }

  private clearScheduledSources(): void {
    for (const source of this.sources) {
      source.onended = null;
      source.stop();
      source.disconnect();
    }
    this.sources.clear();
  }

  private emitFeedback(
    identity: { id: string; epoch: number; response_id: string; media_session_id: string },
    phase: Playback["phase"],
    amplitude: number,
    reason = "player-stop",
    playedMs = 0,
  ): void {
    const now = performance.now();
    const interval_ms = this.lastFeedbackAt ? Math.max(0, now - this.lastFeedbackAt) : 0;
    this.lastFeedbackAt = now;
    const elapsedSeconds = this.context
      ? Math.min(
          Math.max(0, this.context.currentTime - this.startAt),
          Math.max(0, this.scheduledUntil - this.startAt),
        )
      : 0;
    this.emit({
      playback_id: identity.id,
      output_epoch: identity.epoch,
      response_id: identity.response_id,
      media_session_id: identity.media_session_id,
      phase,
      progress: elapsedSeconds,
      played_ms: phase === "cancel" ? playedMs : Math.round(elapsedSeconds * 1000),
      interval_ms,
      amplitude,
      source: "real",
      ...(phase === "cancel" ? { reason } : {}),
    });
  }

  private frame = (): void => {
    if (this.dead || this.closed || !this.current || !this.playing || !this.analyser || !this.samples) return;
    this.analyser.getByteTimeDomainData(this.samples);
    const sum = this.samples.reduce((total, byte) => total + ((byte - 128) / 128) ** 2, 0);
    const amplitude = Math.sqrt(sum / this.samples.length);
    this.emitFeedback(this.current, "playing", Math.max(0, Math.min(1, amplitude)));
    this.raf = requestAnimationFrame(this.frame);
  };

  private async waitForScheduleWindow(nextTime: number): Promise<void> {
    while (
      !this.dead &&
      !this.closed &&
      this.context &&
      nextTime - this.context.currentTime > 1
    ) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
  }
}

export class NativeOutputPlayer {
  private closed = false;
  private attached = false;
  private started = false;
  private player?: LocalPlayer | NativePcmPlayer;
  private pendingFeedback: Playback[] = [];
  private lastProgressPhase?: Playback["phase"];
  private suppressingProgressEcho = false;
  private producerSeq = 0;
  private audibleEmitted = false;
  private lastProgressReceiptAtMs = Number.NEGATIVE_INFINITY;

  constructor(private readonly options: NativeOutputPlayerOptions) {}

  accepts(input: { epoch: number; source: string }): boolean {
    return (
      !this.closed &&
      input.epoch === this.options.identity.output_epoch &&
      input.source === "real"
    );
  }

  attach(input: { epoch: number; source: string }): void {
    if (!this.accepts(input)) throw new Error("拒绝旧epoch或错配来源");
    this.attached = true;
  }

  async start(input: File | NativePcmSource, start_ms: number): Promise<void> {
    if (!this.attached || this.closed) {
      throw new Error("播放窗口未接受当前real输出");
    }
    if (this.started || this.player) {
      throw new Error("每个播放实例只能启动一次");
    }

    const identity = {
      id: this.options.identity.playback_id,
      epoch: this.options.identity.output_epoch,
      response_id: this.options.identity.response_id,
      media_session_id: this.options.identity.media_session_id,
    };
    try {
      if (isNativePcmSource(input)) {
        const pcmPlayer = new NativePcmPlayer(
          (feedback) => this.handleFeedback(feedback),
          this.options.createContext ?? (() => new AudioContext()),
          () => {
            if (this.started || this.closed) return;
            this.started = true;
            this.emit({ action: "open", start_ms });
            this.flushPendingFeedback();
          },
        );
        this.player = pcmPlayer;
        await pcmPlayer.start(input, identity);
      } else {
        const localPlayer = new LocalPlayer((feedback) => this.handleFeedback(feedback), {
          audio: this.options.audio,
          createContext: this.options.createContext,
        });
        this.player = localPlayer;
        await localPlayer.start(input, identity);
      }
      if (!isNativePcmSource(input)) {
        this.started = true;
        this.emit({ action: "open", start_ms });
        this.flushPendingFeedback();
      }
    } catch (error) {
      this.flushPendingFeedback();
      this.close();
      throw error;
    }
  }

  stop(reason = "player-stop"): void {
    if (this.player instanceof NativePcmPlayer) this.player.stop(reason);
    else this.player?.stop();
  }

  async destroy(): Promise<void> {
    this.close();
    await this.player?.destroy();
  }

  close(): void {
    this.closed = true;
  }

  private handleFeedback(feedback: Playback): void {
    if (this.closed) return;
    if (!this.started) {
      this.pendingFeedback.push(feedback);
      return;
    }
    if (
      feedback.phase !== "drained" &&
      feedback.phase !== "cancel" &&
      feedback.phase !== "error" &&
      this.suppressingProgressEcho &&
      feedback.phase === this.lastProgressPhase
    ) {
      return;
    }
    this.emitPlaybackFeedback(feedback);
  }

  private flushPendingFeedback(): void {
    const feedback = this.pendingFeedback;
    this.pendingFeedback = [];
    for (const value of feedback) {
      if (!this.closed) this.emitPlaybackFeedback(value);
    }
  }

  private emitPlaybackFeedback(feedback: Playback): void {
    if (feedback.phase === "playing" && !this.audibleEmitted) {
      this.audibleEmitted = true;
      this.emit({
        action: "audible",
        audible_ms: feedback.played_ms,
      });
      return;
    }
    if (feedback.phase === "drained") {
      this.emit({
        action: "end",
        scope: "complete",
        played_ms: feedback.played_ms,
      });
      this.close();
      return;
    }
    if (feedback.phase === "cancel") {
      this.emit({
        action: "interrupt",
        reason: feedback.reason ?? "player-stop",
        played_ms: feedback.played_ms,
      });
      this.close();
      return;
    }
    if (feedback.phase === "error") {
      this.emit({
        action: "interrupt",
        reason: "error",
        played_ms: feedback.played_ms,
      });
      this.close();
      return;
    }
    const now = performance.now();
    if (now - this.lastProgressReceiptAtMs < 250) return;
    this.lastProgressReceiptAtMs = now;
    this.emit({
      action: "progress",
      played_ms: feedback.played_ms,
      interval_ms: Math.round(feedback.interval_ms),
      phase: feedback.phase,
    });
    this.lastProgressPhase = feedback.phase;
    this.suppressingProgressEcho = true;
    queueMicrotask(() => {
      this.suppressingProgressEcho = false;
    });
  }

  private emit(
    event: Omit<
      NativePlaybackEventRequest,
      | "session_id"
      | "playback_id"
      | "response_id"
      | "producer_seq"
      | "output_epoch"
      | "media_session_id"
      | "turn_id"
    >,
  ): void {
    const producer_seq = ++this.producerSeq;
    const request: NativePlaybackEventRequest = {
      session_id: this.options.session_id,
      playback_id: this.options.identity.playback_id,
      response_id: this.options.identity.response_id,
      producer_seq,
      output_epoch: this.options.identity.output_epoch,
      media_session_id: this.options.identity.media_session_id,
      ...(this.options.identity.turn_id ? { turn_id: this.options.identity.turn_id } : {}),
      ...event,
    };
    this.options.emit(request);
  }
}
