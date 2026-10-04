import type { Playback } from "../avatar-preview/contracts";
import { LocalPlayer } from "../avatar-preview/player";

export type NativePlaybackIdentity = {
  playback_id: string;
  output_epoch: number;
  response_id: string;
  media_session_id: string;
};

type NativePlaybackAction =
  | "open"
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
  start_ms?: number;
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

export class NativeOutputPlayer {
  private closed = false;
  private attached = false;
  private started = false;
  private player?: LocalPlayer;
  private pendingFeedback: Playback[] = [];
  private lastProgressPhase?: Playback["phase"];
  private suppressingProgressEcho = false;
  private producerSeq = 0;

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

  async start(file: File, start_ms: number): Promise<void> {
    if (!this.attached || this.closed) {
      throw new Error("播放窗口未接受当前real输出");
    }
    if (this.started || this.player) {
      throw new Error("每个播放实例只能启动一次");
    }

    this.player = new LocalPlayer((feedback) => this.handleFeedback(feedback), {
      audio: this.options.audio,
      createContext: this.options.createContext,
    });
    try {
      await this.player.start(file, {
        id: this.options.identity.playback_id,
        epoch: this.options.identity.output_epoch,
        response_id: this.options.identity.response_id,
        media_session_id: this.options.identity.media_session_id,
      });
      this.started = true;
      this.emit({
        action: "open",
        start_ms,
      });
      this.flushPendingFeedback();
    } catch (error) {
      this.flushPendingFeedback();
      this.close();
      throw error;
    }
  }

  stop(): void {
    this.player?.stop();
  }

  async destroy(): Promise<void> {
    await this.player?.destroy();
    this.close();
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
        reason: "player-stop",
        played_ms: feedback.played_ms,
      });
      this.close();
      return;
    }
    if (feedback.phase === "error") {
      this.emit({
        action: "end",
        scope: "unknown",
        played_ms: feedback.played_ms,
      });
      this.close();
      return;
    }
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
      "session_id" | "playback_id" | "response_id" | "output_epoch" | "media_session_id"
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
      ...event,
    };
    this.options.emit(request);
  }
}
