import config from "./speech-detection-config.json";

export type DetectorFailure = { code: string; message: string };

export type DetectorWorkerCommand =
  | {
      type: "initialize";
      sessionId: string;
      mediaSessionId: string;
      captureInstanceId: string;
      generation: number;
    }
  | {
      type: "frame";
      captureInstanceId: string;
      generation: number;
      frameId: number;
      startSample: number;
      endSample: number;
      startMonotonicMs: number;
      endMonotonicMs: number;
      samples: Int16Array;
    }
  | {
      type: "reset";
      captureInstanceId: string;
      generation: number;
      controlId: string;
    }
  | { type: "close"; captureInstanceId: string; generation: number };

export type SpeechDetectorMessage =
  | {
      type: "ready";
      sessionId: string;
      mediaSessionId: string;
      captureInstanceId: string;
      generation: number;
      model: string;
    }
  | {
      type: "frame_result";
      captureInstanceId: string;
      generation: number;
      frameId: number;
      startSample: number;
      endSample: number;
      probability: number;
    }
  | {
      type: "reset_ack";
      captureInstanceId: string;
      generation: number;
      controlId: string;
    }
  | { type: "closed"; captureInstanceId: string; generation: number }
  | {
      type: "error";
      captureInstanceId: string;
      generation: number;
      code: string;
      message: string;
    };

export interface DetectorWorkerPort {
  onmessage: ((event: MessageEvent<SpeechDetectorMessage>) => void) | null;
  onerror: ((event: ErrorEvent) => void) | null;
  postMessage(message: DetectorWorkerCommand, transfer?: Transferable[]): void;
  terminate(): void;
}

export type WorkerReadyIdentity = {
  sessionId: string;
  mediaSessionId: string;
  captureInstanceId: string;
  generation: number;
  bargeInSupported: boolean;
};

export type PcmTarget = Readonly<{
  response_id: string;
  playback_id: string;
  output_epoch: number;
  captureGeneration: number;
}>;

export type PcmFrame = {
  frameId: number;
  generation: number;
  startSample: number;
  endSample: number;
  startMonotonicMs: number;
  endMonotonicMs: number;
  samples: Int16Array;
  target?: PcmTarget | null;
};

export type ReleasedPcmPrefix = {
  generation: number;
  startSample: number;
  endSample: number;
  startMonotonicMs: number;
  endMonotonicMs: number;
  samples: Int16Array;
  probabilities: number[];
  target?: PcmTarget | null;
};

type PendingFrame = PcmFrame & { probability?: number };

function samePcmTarget(left: PcmTarget | null, right: PcmTarget | null): boolean {
  return left === null || right === null ? left === right :
    left.response_id === right.response_id && left.playback_id === right.playback_id &&
    left.output_epoch === right.output_epoch && left.captureGeneration === right.captureGeneration;
}

export class PcmWatermark {
  private readonly budgetSamples: number;
  private readonly pending: PendingFrame[] = [];
  private tail = new Int16Array(0);
  private tailStartSample: number | null = null;
  private tailStartMonotonicMs: number | null = null;
  private tailTarget: PcmTarget | null = null;
  private expectedNextSample: number | null = null;
  private frameId = 0;
  private readonly completed = new Set<number>();

  generation = 0;

  constructor(
    private readonly options: { sampleRate: number; frameSamples: number; maxBufferedMs: number },
  ) {
    const { sampleRate, frameSamples, maxBufferedMs } = options;
    if (!Number.isSafeInteger(sampleRate) || sampleRate <= 0) throw new Error("sampleRate must be a positive integer");
    if (!Number.isSafeInteger(frameSamples) || frameSamples <= 0) throw new Error("frameSamples must be a positive integer");
    if (!Number.isSafeInteger(maxBufferedMs) || maxBufferedMs <= 0) throw new Error("maxBufferedMs must be a positive integer");
    this.budgetSamples = Math.floor((sampleRate * maxBufferedMs) / 1000);
  }

  get pendingSampleCount(): number {
    return this.tail.length + this.pending.reduce((total, frame) => total + frame.samples.length, 0);
  }

  append(
    samples: Int16Array,
    startSample: number,
    startMonotonicMs: number,
    endMonotonicMs: number,
    target: PcmTarget | null = null,
  ): PcmFrame[] {
    if (!(samples instanceof Int16Array) || samples.length === 0) return [];
    if (!Number.isSafeInteger(startSample) || startSample < 0) throw new Error("sample positions must be non-negative safe integers");
    if (!Number.isFinite(startMonotonicMs) || !Number.isFinite(endMonotonicMs) || endMonotonicMs < startMonotonicMs) {
      throw new Error("monotonic range is invalid");
    }
    if (this.expectedNextSample !== null && startSample !== this.expectedNextSample) {
      throw new Error(`PCM sample gap: expected ${this.expectedNextSample}, received ${startSample}`);
    }
    if (this.pendingSampleCount + samples.length > this.budgetSamples) {
      throw new Error(`PCM backlog exceeds ${this.options.maxBufferedMs}ms budget`);
    }
    if (target && target.captureGeneration !== this.generation) throw new Error("PCM target capture generation is stale");
    const sampledTarget = target ? Object.freeze({ ...target }) : null;
    const oldTailLength = this.tail.length;
    const oldTailTarget = this.tailTarget;

    let combined: Int16Array;
    let combinedStart: number;
    let combinedStartMs: number;
    if (this.tail.length > 0) {
      combined = new Int16Array(this.tail.length + samples.length);
      combined.set(this.tail);
      combined.set(samples, this.tail.length);
      combinedStart = this.tailStartSample!;
      combinedStartMs = this.tailStartMonotonicMs!;
    } else {
      combined = samples.slice();
      combinedStart = startSample;
      combinedStartMs = startMonotonicMs;
    }

    const frames: PcmFrame[] = [];
    let offset = 0;
    while (combined.length - offset >= this.options.frameSamples) {
      const frameStart = combinedStart + offset;
      const frameSamples = combined.slice(offset, offset + this.options.frameSamples);
      const frameDurationMs = (frameSamples.length * 1000) / this.options.sampleRate;
      const startMs = combinedStartMs + (offset * 1000) / this.options.sampleRate;
      const frame: PcmFrame = {
        frameId: this.frameId++,
        generation: this.generation,
        startSample: frameStart,
        endSample: frameStart + frameSamples.length,
        startMonotonicMs: startMs,
        endMonotonicMs: startMs + frameDurationMs,
        samples: frameSamples,
        target: offset >= oldTailLength || samePcmTarget(oldTailTarget, sampledTarget) ? sampledTarget : null,
      };
      frames.push(frame);
      this.pending.push(frame);
      offset += this.options.frameSamples;
    }
    this.tail = combined.slice(offset);
    this.tailTarget = this.tail.length > 0 && (offset >= oldTailLength || samePcmTarget(oldTailTarget, sampledTarget)) ? sampledTarget : null;
    this.tailStartSample = this.tail.length > 0 ? combinedStart + offset : null;
    this.tailStartMonotonicMs = this.tail.length > 0
      ? combinedStartMs + (offset * 1000) / this.options.sampleRate
      : null;
    this.expectedNextSample = startSample + samples.length;
    return frames;
  }

  resolve(frameId: number, generation: number, probability: number): ReleasedPcmPrefix[] {
    if (generation !== this.generation) throw new Error(`stale generation ${generation}; active generation is ${this.generation}`);
    if (!Number.isFinite(probability) || probability < 0 || probability > 1) throw new Error("speech probability must be between 0 and 1");
    const frame = this.pending.find((item) => item.frameId === frameId);
    if (!frame) {
      if (this.completed.has(frameId)) throw new Error(`frame ${frameId} result already resolved`);
      throw new Error(`unknown pending frame ${frameId}`);
    }
    if (frame.probability !== undefined) throw new Error(`frame ${frameId} result already resolved`);
    frame.probability = probability;

    const released: ReleasedPcmPrefix[] = [];
    while (this.pending[0]?.probability !== undefined) {
      const prefixFrames: PendingFrame[] = [];
      const prefixTarget = this.pending[0].target ?? null;
      while (this.pending[0]?.probability !== undefined && samePcmTarget(this.pending[0].target ?? null, prefixTarget)) {
        prefixFrames.push(this.pending.shift()!);
      }
      for (let index = 1; index < prefixFrames.length; index += 1) {
        if (prefixFrames[index - 1].endSample !== prefixFrames[index].startSample) {
          throw new Error("resolved PCM prefix is not contiguous");
        }
      }
      const sampleCount = prefixFrames.reduce((total, item) => total + item.samples.length, 0);
      const pcm = new Int16Array(sampleCount);
      let writeOffset = 0;
      for (const resolved of prefixFrames) {
        pcm.set(resolved.samples, writeOffset);
        writeOffset += resolved.samples.length;
        this.completed.add(resolved.frameId);
      }
      released.push({
        generation: this.generation,
        startSample: prefixFrames[0].startSample,
        endSample: prefixFrames[prefixFrames.length - 1].endSample,
        startMonotonicMs: prefixFrames[0].startMonotonicMs,
        endMonotonicMs: prefixFrames[prefixFrames.length - 1].endMonotonicMs,
        samples: pcm,
        probabilities: prefixFrames.map((item) => item.probability!),
        target: prefixTarget,
      });
    }
    return released;
  }

  reset(generation: number): void {
    if (!Number.isSafeInteger(generation) || generation <= this.generation) {
      throw new Error("reset generation must increase monotonically");
    }
    this.generation = generation;
    this.pending.length = 0;
    this.tail = new Int16Array(0);
    this.tailStartSample = null;
    this.tailStartMonotonicMs = null;
    this.tailTarget = null;
    this.expectedNextSample = null;
    this.completed.clear();
  }
}

export type SpeechDetectorOptions = {
  sessionId: string;
  mediaSessionId: string;
  captureInstanceId: string;
  workerFactory?: () => DetectorWorkerPort;
  onReleasedPcm?: (prefix: ReleasedPcmPrefix) => void;
};

export class SpeechDetector {
  private worker: DetectorWorkerPort | null = null;
  private state: "idle" | "initializing" | "ready" | "resetting" | "failed" | "closed" = "idle";
  private workerReadyGeneration: number | null = null;
  private serverReadyGeneration: number | null = null;
  private currentGeneration = 0;
  private pendingReset: { generation: number; controlId: string; resolve: () => void; reject: (error: Error) => void } | null = null;
  private failureValue: DetectorFailure | null = null;
  private readinessWaiters = new Set<{ resolve: () => void; reject: (error: Error) => void }>();
  private readonly waterline = new PcmWatermark({
    sampleRate: config.audio.sample_rate,
    frameSamples: config.audio.model_frame_samples,
    maxBufferedMs: config.worker.maximum_queued_audio_ms,
  });

  constructor(private readonly options: SpeechDetectorOptions) {}

  get generation(): number { return this.currentGeneration; }
  get failure(): DetectorFailure | null { return this.failureValue; }
  get canOpenCapture(): boolean {
    return this.state === "ready"
      && this.failureValue === null
      && this.workerReadyGeneration === this.currentGeneration
      && this.serverReadyGeneration === this.currentGeneration;
  }
  get pendingSampleCount(): number { return this.waterline.pendingSampleCount; }

  waitUntilReady(): Promise<void> {
    if (this.canOpenCapture) return Promise.resolve();
    if (this.state === "failed") return Promise.reject(new Error(this.failureValue?.message ?? "Speech detector failed."));
    if (this.state === "closed") return Promise.reject(new Error("Speech detector closed before ready."));
    return new Promise<void>((resolve, reject) => {
      this.readinessWaiters.add({ resolve, reject });
    });
  }

  start(): void {
    if (this.state !== "idle") throw new Error(`cannot start detector from ${this.state}`);
    const factory = this.options.workerFactory ?? (() => new Worker(
      new URL("./speech-detector.worker.ts", import.meta.url),
      { type: "module" },
    ) as unknown as DetectorWorkerPort);
    this.worker = factory();
    this.worker.onmessage = (event) => this.handleWorkerMessage(event.data);
    this.worker.onerror = (event) => this.fail("worker_error", event.message || "Speech detector Worker failed.");
    this.state = "initializing";
    this.worker.postMessage({
      type: "initialize",
      sessionId: this.options.sessionId,
      mediaSessionId: this.options.mediaSessionId,
      captureInstanceId: this.options.captureInstanceId,
      generation: this.currentGeneration,
    });
  }

  noteServerReady(identity: WorkerReadyIdentity): boolean {
    if (this.state === "closed" || this.state === "failed") return false;
    if (identity.generation !== this.currentGeneration) return false;
    if (
      identity.sessionId !== this.options.sessionId
      || identity.mediaSessionId !== this.options.mediaSessionId
      || identity.captureInstanceId !== this.options.captureInstanceId
    ) {
      this.fail("server_identity_mismatch", "Server ready identity does not match the selected media session and capture instance.");
      return false;
    }
    if (!identity.bargeInSupported) {
      this.fail("barge_in_capability_unavailable", "The selected server does not support confirmed voice barge-in.");
      return false;
    }
    this.serverReadyGeneration = identity.generation;
    this.resolveReadinessIfReady();
    return true;
  }

  reset(generation: number, controlId: string): Promise<void> {
    if (this.state !== "ready" || !this.worker) throw new Error("only a ready detector can reset");
    if (!controlId) throw new Error("reset controlId is required");
    if (generation <= this.currentGeneration) throw new Error("reset generation must increase monotonically");
    this.currentGeneration = generation;
    this.serverReadyGeneration = null;
    this.workerReadyGeneration = null;
    this.waterline.reset(generation);
    this.state = "resetting";
    return new Promise<void>((resolve, reject) => {
      this.pendingReset = { generation, controlId, resolve, reject };
      this.worker!.postMessage({ type: "reset", captureInstanceId: this.options.captureInstanceId, generation, controlId });
    });
  }

  appendPcm(samples: Int16Array, startSample: number, startMonotonicMs: number, endMonotonicMs: number, target: PcmTarget | null = null): PcmFrame[] {
    if (this.state !== "ready" || this.workerReadyGeneration !== this.currentGeneration || !this.worker) {
      throw new Error("speech Worker is not ready to receive PCM");
    }
    const frames = this.waterline.append(samples, startSample, startMonotonicMs, endMonotonicMs, target);
    for (const frame of frames) {
      const transferableSamples = frame.samples.slice();
      this.worker.postMessage({
        type: "frame",
        captureInstanceId: this.options.captureInstanceId,
        generation: frame.generation,
        frameId: frame.frameId,
        startSample: frame.startSample,
        endSample: frame.endSample,
        startMonotonicMs: frame.startMonotonicMs,
        endMonotonicMs: frame.endMonotonicMs,
        samples: transferableSamples,
      }, [transferableSamples.buffer]);
    }
    return frames;
  }

  close(): void {
    if (this.state === "closed") return;
    this.state = "closed";
    this.serverReadyGeneration = null;
    this.workerReadyGeneration = null;
    this.pendingReset?.reject(new Error("detector closed while waiting for reset"));
    this.pendingReset = null;
    this.rejectReadiness(new Error("Speech detector closed before ready."));
    this.worker?.postMessage({ type: "close", captureInstanceId: this.options.captureInstanceId, generation: this.currentGeneration });
    this.worker?.terminate();
    this.worker = null;
    this.waterline.reset(this.currentGeneration + 1);
  }

  private handleWorkerMessage(message: SpeechDetectorMessage): void {
    if (this.state === "closed" || message.captureInstanceId !== this.options.captureInstanceId) return;
    if (message.type === "error") {
      this.fail(message.code, message.message);
      return;
    }
    if (message.type === "ready") {
      if (
        message.sessionId !== this.options.sessionId
        || message.mediaSessionId !== this.options.mediaSessionId
        || message.generation !== this.currentGeneration
      ) {
        this.fail("worker_identity_mismatch", "Speech detector Worker returned a mismatched session or generation.");
        return;
      }
      this.workerReadyGeneration = message.generation;
      this.state = "ready";
      this.resolveReadinessIfReady();
      return;
    }
    if (message.type === "reset_ack") {
      const reset = this.pendingReset;
      if (
        !reset
        || reset.generation !== message.generation
        || reset.controlId !== message.controlId
        || message.generation !== this.currentGeneration
      ) return;
      this.pendingReset = null;
      this.workerReadyGeneration = message.generation;
      this.state = "ready";
      reset.resolve();
      this.resolveReadinessIfReady();
      return;
    }
    if (message.type === "frame_result") {
      if (message.generation < this.currentGeneration) return;
      try {
        const released = this.waterline.resolve(message.frameId, message.generation, message.probability);
        for (const prefix of released) this.options.onReleasedPcm?.(prefix);
      } catch (error) {
        this.fail("waterline_result_invalid", error instanceof Error ? error.message : "Invalid Worker frame result.");
      }
    }
  }

  private fail(code: string, message: string): void {
    if (this.state === "closed") return;
    this.failureValue = { code, message };
    this.state = "failed";
    this.serverReadyGeneration = null;
    this.workerReadyGeneration = null;
    this.pendingReset?.reject(new Error(message));
    this.pendingReset = null;
    this.rejectReadiness(new Error(message));
    this.worker?.terminate();
    this.worker = null;
  }

  private resolveReadinessIfReady(): void {
    if (!this.canOpenCapture) return;
    for (const waiter of this.readinessWaiters) waiter.resolve();
    this.readinessWaiters.clear();
  }

  private rejectReadiness(error: Error): void {
    for (const waiter of this.readinessWaiters) waiter.reject(error);
    this.readinessWaiters.clear();
  }
}
