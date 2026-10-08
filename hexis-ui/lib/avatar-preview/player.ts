import type { Playback } from "./contracts";

type Identity = {
  id: string;
  epoch: number;
  response_id?: string;
  media_session_id?: string;
};
type Options = { audio?: HTMLAudioElement; createContext?: () => AudioContext };

export class LocalPlayer {
  private readonly audio: HTMLAudioElement;
  private readonly createContext: () => AudioContext;
  private context?: AudioContext;
  private analyser?: AnalyserNode;
  private samples?: Uint8Array<ArrayBuffer>;
  private url?: string;
  private raf = 0;
  private dead = false;
  private started = false;
  private closed = true;
  private playing = false;
  private current?: Identity;
  private lastFeedbackAt = 0;
  private handlers: Array<[string, EventListener]> = [];

  constructor(private readonly emit: (value: Playback) => void, options: Options = {}) {
    this.audio = options.audio ?? new Audio();
    this.createContext = options.createContext ?? (() => new AudioContext());
    const events: Array<[string, Playback["phase"]]> = [
      ["playing", "playing"], ["waiting", "buffering"], ["stalled", "buffering"],
      ["pause", "buffering"], ["ended", "drained"], ["error", "error"],
    ];
    for (const [name, phase] of events) {
      const handler: EventListener = () => this.feedback(phase);
      this.handlers.push([name, handler]);
      this.audio.addEventListener(name, handler);
    }
  }

  private feedback(phase: Playback["phase"], reason?: string): void {
    if (this.dead || this.closed || !this.current) return;
    this.playing = phase === "playing";
    if (["drained", "cancel", "error"].includes(phase)) this.closed = true;
    cancelAnimationFrame(this.raf);
    this.emitFeedback(this.current, phase, 0, reason);
    if (this.playing) this.frame();
  }

  private emitFeedback(identity: Identity, phase: Playback["phase"], amplitude: number, reason?: string): void {
    const now = performance.now();
    const interval_ms = this.lastFeedbackAt ? Math.max(0, now - this.lastFeedbackAt) : 0;
    this.lastFeedbackAt = now;
    const progress = Number.isFinite(this.audio.currentTime) ? this.audio.currentTime : 0;
    this.emit({
      playback_id: identity.id,
      output_epoch: identity.epoch,
      response_id: identity.response_id,
      media_session_id: identity.media_session_id,
      phase,
      progress,
      played_ms: Math.round(progress * 1000),
      interval_ms,
      amplitude,
      source: "real",
      ...(phase === "cancel" && reason ? { reason } : {}),
    });
  }

  private frame = (): void => {
    if (this.dead || this.closed || !this.current || !this.playing || !this.analyser || !this.samples) return;
    this.analyser.getByteTimeDomainData(this.samples);
    const sum = this.samples.reduce((total, byte) => total + ((byte - 128) / 128) ** 2, 0);
    const amplitude = Math.sqrt(sum / this.samples.length) * (this.audio.muted ? 0 : this.audio.volume);
    this.emitFeedback(this.current, "playing", Math.max(0, Math.min(1, amplitude)));
    this.raf = requestAnimationFrame(this.frame);
  };

  private setupAudioGraph(): void {
    if (this.context) return;
    this.context = this.createContext();
    this.analyser = this.context.createAnalyser();
    this.samples = new Uint8Array(this.analyser.fftSize) as Uint8Array<ArrayBuffer>;
    const source = this.context.createMediaElementSource(this.audio);
    source.connect(this.analyser);
    this.analyser.connect(this.context.destination);
  }

  async start(file: File, identity: Identity): Promise<void> {
    if (this.dead || this.started) throw new Error("每个播放窗口只能启动一次；请创建新播放器实例和epoch");
    this.started = true;
    this.current = identity;
    this.closed = false;
    try {
      this.setupAudioGraph();
      this.url = URL.createObjectURL(file);
      this.audio.src = this.url;
      await this.context!.resume();
      if (this.dead || this.closed || this.current !== identity) return;
      await this.audio.play();
      if (this.dead || this.closed || this.current !== identity) this.audio.pause();
    } catch {
      if (this.dead || this.closed || this.current !== identity) return;
      this.feedback("error");
      throw new Error("文件未能播放；请选择受支持的音频文件并点击播放");
    }
  }

  pause(): void { this.audio.pause(); }

  async resume(): Promise<void> {
    if (this.dead || this.closed || !this.current) throw new Error("当前播放窗口已结束；请选择文件并启动新播放");
    const identity = this.current;
    try { await this.audio.play(); }
    catch {
      if (this.dead || this.closed || this.current !== identity) return;
      throw new Error("播放未能恢复；请点击停止并重新启动所选文件");
    }
    if (this.dead || this.closed || this.current !== identity) this.audio.pause();
  }

  setVolume(value: number): void {
    if (!Number.isFinite(value)) return;
    this.audio.volume = Math.max(0, Math.min(1, value));
    if (this.playing) { cancelAnimationFrame(this.raf); this.frame(); }
  }

  setMuted(value: boolean): void {
    this.audio.muted = value;
    if (this.playing) { cancelAnimationFrame(this.raf); this.frame(); }
  }

  stop(reason = "player-stop"): void {
    const previous = this.current;
    this.current = undefined;
    this.closed = true;
    this.playing = false;
    cancelAnimationFrame(this.raf);
    this.audio.pause();
    if (previous) this.emitFeedback(previous, "cancel", 0, reason);
    if (this.url) URL.revokeObjectURL(this.url);
    this.url = undefined;
  }

  async destroy(): Promise<void> {
    if (this.dead) return;
    this.stop();
    this.dead = true;
    for (const [name, handler] of this.handlers) this.audio.removeEventListener(name, handler);
    this.handlers = [];
    this.audio.removeAttribute("src");
    this.audio.load();
    try { await this.context?.close(); } catch {
      throw new Error("播放器视觉反馈已停止，但AudioContext关闭失败；可刷新预览后重试");
    }
  }
}
