"use client";

import { StreamingVad, type VadEvent } from "./vad";

export class StreamingPcmResampler {
  private input = new Float32Array(0);
  private inputBaseFrame = 0;
  private inputFrameCount = 0;
  private nextOutputPosition = 0;
  private closed = false;
  private readonly radius = 16;
  private readonly step: number;
  private readonly cutoff: number;

  constructor(
    readonly inputSampleRate: number,
    readonly outputSampleRate: number,
  ) {
    if (
      !Number.isFinite(inputSampleRate) ||
      !Number.isFinite(outputSampleRate) ||
      inputSampleRate <= 0 ||
      outputSampleRate <= 0
    ) {
      throw new Error("Sample rates must be positive finite numbers.");
    }
    this.step = inputSampleRate / outputSampleRate;
    this.cutoff = Math.min(1, outputSampleRate / inputSampleRate) * 0.94;
  }

  push(samples: Float32Array): Int16Array {
    if (this.closed) throw new Error("The PCM resampler is closed.");
    if (!(samples instanceof Float32Array)) {
      throw new Error("AudioWorklet samples must be Float32Array values.");
    }
    if (samples.length === 0) return new Int16Array(0);

    const merged = new Float32Array(this.input.length + samples.length);
    merged.set(this.input);
    merged.set(samples, this.input.length);
    this.input = merged;
    this.inputFrameCount += samples.length;

    const output: number[] = [];
    const latestFrame = this.inputBaseFrame + this.input.length - 1;
    while (this.nextOutputPosition + this.radius <= latestFrame) {
      output.push(this.sampleAt(this.nextOutputPosition));
      this.nextOutputPosition += this.step;
    }
    this.trimHistory();
    return Int16Array.from(output, (sample) => {
      const bounded = Math.max(-1, Math.min(1, sample));
      return bounded < 0 ? Math.round(bounded * 32768) : Math.round(bounded * 32767);
    });
  }

  close(): void {
    this.closed = true;
    this.input = new Float32Array(0);
  }

  private sampleAt(position: number): number {
    const center = Math.floor(position);
    let sum = 0;
    let weightSum = 0;
    for (let frame = center - this.radius + 1; frame <= center + this.radius; frame += 1) {
      const distance = position - frame;
      const normalized = distance / this.radius;
      if (Math.abs(normalized) >= 1) continue;
      const window = 0.5 + 0.5 * Math.cos(Math.PI * normalized);
      const weight = this.cutoff * sinc(this.cutoff * distance) * window;
      const offset = frame - this.inputBaseFrame;
      const value = offset >= 0 && offset < this.input.length ? this.input[offset] : 0;
      sum += value * weight;
      weightSum += weight;
    }
    return weightSum === 0 ? 0 : sum / weightSum;
  }

  private trimHistory(): void {
    const keepFrom = Math.max(
      this.inputBaseFrame,
      Math.floor(this.nextOutputPosition) - this.radius - 1,
    );
    const remove = Math.min(this.input.length, keepFrom - this.inputBaseFrame);
    if (remove <= 0) return;
    this.input = this.input.slice(remove);
    this.inputBaseFrame += remove;
  }
}

export class BoundedAudioQueue<T> {
  private readonly values: T[] = [];

  constructor(readonly capacity: number) {
    if (!Number.isInteger(capacity) || capacity <= 0) {
      throw new Error("Audio queue capacity must be a positive integer.");
    }
  }

  get size(): number {
    return this.values.length;
  }

  push(value: T): void {
    if (this.values.length >= this.capacity) {
      throw new Error(`Audio capture queue is full (capacity ${this.capacity}).`);
    }
    this.values.push(value);
  }

  shift(): T | undefined {
    return this.values.shift();
  }

  clear(): void {
    this.values.length = 0;
  }
}

export type AudioWorkletBlock = {
  samples: Float32Array;
  startFrame: number;
  observedAtMs: number;
};

export function monoBlockFromWorklet(data: unknown): AudioWorkletBlock {
  if (!data || typeof data !== "object") {
    throw new Error("Audio capture returned an invalid worklet block.");
  }
  const value = data as Partial<AudioWorkletBlock>;
  if (
    !(value.samples instanceof Float32Array) ||
    !Number.isSafeInteger(value.startFrame) ||
    typeof value.observedAtMs !== "number" ||
    !Number.isFinite(value.observedAtMs)
  ) {
    throw new Error("Audio capture returned an invalid worklet block.");
  }
  return value as AudioWorkletBlock;
}

type CaptureTransport = {
  sendControl?(action: "barge_in", reason: string): void;
  openCapture(captureInstanceId: string): void;
  suspendCapture(captureInstanceId: string): Promise<void>;
  resumeCapture(captureInstanceId: string): Promise<void>;
  sendAudioChunk(
    captureInstanceId: string,
    range: {
      startFrame: number;
      endFrame: number;
      startMonotonicMs: number;
      endMonotonicMs: number;
      pcmBase64: string;
    },
  ): void;
  endUtterance(
    captureInstanceId: string,
    range: {
      startFrame: number;
      endFrame: number;
      startMonotonicMs: number;
      endMonotonicMs: number;
      audioSha256: string;
    },
  ): void;
};

export type CapturePauseToken = {
  captureInstanceId: string;
  trackId: string;
  ready: Promise<void>;
};

export type BargeInTarget = {
  response_id: string;
  playback_id: string;
  output_epoch: number;
};

export class AudioCaptureController {
  private context: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private audioTrack: MediaStreamTrack | null = null;
  private worklet: AudioWorkletNode | null = null;
  private scriptProcessor: ScriptProcessorNode | null = null;
  private silenceGain: GainNode | null = null;
  private resampler: StreamingPcmResampler | null = null;
  private vad: StreamingVad | null = null;
  private closed = false;
  private paused = false;
  private failed = false;
  private pauseToken: CapturePauseToken | null = null;
  private outputFrame = 0;
  private utteranceParts: Uint8Array[] = [];
  private utteranceByteLength = 0;
  private pendingSamples: number[] = [];
  private pendingStartFrame = 0;
  private pendingStartMs = 0;
  private pendingEndMs = 0;
  private utteranceStartMs: number | null = null;
  private utteranceEndMs: number | null = null;
  private outboundChain: Promise<void> = Promise.resolve();
  private workletStarted = false;
  private fallbackActive = false;
  private fallbackFrame = 0;
  private fallbackTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly fallbackQueue = new BoundedAudioQueue<AudioWorkletBlock>(12);
  private readonly workletQueue = new BoundedAudioQueue<AudioWorkletBlock>(16);

  constructor(
    private readonly transport: CaptureTransport,
    private readonly captureInstanceId: string,
    private readonly onFailure: (message: string) => void,
    private readonly onSpeechStart?: (target?: BargeInTarget) => void,
  ) {}

  get deviceSettings(): MediaTrackSettings | undefined {
    return this.audioTrack?.getSettings();
  }

  get id(): string {
    return this.captureInstanceId;
  }

  async start(stream: MediaStream): Promise<void> {
    if (this.context || this.closed) throw new Error("Audio capture already started or closed.");
    const track = stream.getAudioTracks()[0];
    if (!track) throw new Error("The microphone stream has no audio track.");
    this.audioTrack = track;
    track.addEventListener("ended", this.handleTrackEnded);
    if (track.readyState === "ended") {
      this.handleTrackEnded();
      throw new Error("The microphone track ended before capture could start.");
    }
    const context = new AudioContext();
    this.context = context;
    context.addEventListener("statechange", this.handleContextStateChange);
    try {
      await context.audioWorklet.addModule("/voice-realtime/capture-worklet.js");
      if (this.closed) return;
      this.resampler = new StreamingPcmResampler(context.sampleRate, 16_000);
      this.vad = new StreamingVad({ sampleRate: 16_000 });
      this.transport.openCapture(this.captureInstanceId);
      const source = context.createMediaStreamSource(stream);
      const worklet = new AudioWorkletNode(context, "hexis-voice-capture", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        channelCount: 1,
        channelCountMode: "explicit",
        channelInterpretation: "speakers",
      });
      const gain = context.createGain();
      // Keep the processor pulled while discarding its silent output.
      gain.gain.value = 0;
      const scriptProcessor = context.createScriptProcessor(2048, 1, 1);
      scriptProcessor.onaudioprocess = (event) => {
        const input = event.inputBuffer.getChannelData(0);
        const block: AudioWorkletBlock = {
          samples: input.slice(),
          startFrame: this.fallbackFrame,
          observedAtMs: performance.now(),
        };
        this.fallbackFrame += input.length;
        const output = event.outputBuffer.getChannelData(0);
        output.fill(0);
        if (this.workletStarted || this.closed || this.paused || this.failed) return;
        try {
          if (this.fallbackActive) this.consumeBlock(block);
          else this.fallbackQueue.push(block);
        } catch (error) {
          this.fail(error);
        }
      };
      source.connect(worklet);
      worklet.connect(gain);
      source.connect(scriptProcessor);
      scriptProcessor.connect(gain);
      gain.connect(context.destination);
      this.source = source;
      this.worklet = worklet;
      this.scriptProcessor = scriptProcessor;
      this.silenceGain = gain;
      worklet.port.onmessage = (event: MessageEvent<unknown>) => {
        if (this.closed || this.paused || this.failed) return;
        try {
          this.workletStarted = true;
          if (this.fallbackTimer !== null) clearTimeout(this.fallbackTimer);
          this.fallbackTimer = null;
          this.fallbackQueue.clear();
          this.workletQueue.push(monoBlockFromWorklet(event.data));
          this.drainWorkletQueue();
        } catch (error) {
          this.fail(error);
        }
      };
      this.fallbackTimer = setTimeout(() => {
        if (this.closed || this.paused || this.failed || this.workletStarted) return;
        this.fallbackActive = true;
        this.worklet?.disconnect();
        for (let block = this.fallbackQueue.shift(); block; block = this.fallbackQueue.shift()) {
          this.consumeBlock(block);
        }
      }, 250);
      await context.resume();
    } catch (error) {
      await this.close();
      throw error instanceof Error ? error : new Error("The microphone audio pipeline failed.");
    }
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    this.paused = false;
    this.pauseToken = null;
    this.workletQueue.clear();
    this.fallbackQueue.clear();
    if (this.fallbackTimer !== null) clearTimeout(this.fallbackTimer);
    this.fallbackTimer = null;
    if (this.worklet) {
      this.worklet.port.onmessage = null;
      this.worklet.port.close();
      this.worklet.disconnect();
    }
    this.source?.disconnect();
    if (this.scriptProcessor) {
      this.scriptProcessor.onaudioprocess = null;
      this.scriptProcessor.disconnect();
    }
    this.silenceGain?.disconnect();
    this.vad?.reset();
    this.resampler?.close();
    await this.outboundChain.catch(() => undefined);
    const context = this.context;
    this.context = null;
    this.worklet = null;
    this.scriptProcessor = null;
    this.source = null;
    const audioTrack = this.audioTrack;
    this.audioTrack = null;
    this.silenceGain = null;
    audioTrack?.removeEventListener("ended", this.handleTrackEnded);
    context?.removeEventListener("statechange", this.handleContextStateChange);
    if (context && context.state !== "closed") await context.close();
  }

  pause(): CapturePauseToken | null {
    if (this.closed || this.failed || !this.context || !this.audioTrack) return null;
    if (this.pauseToken) return this.pauseToken;

    const track = this.audioTrack;
    const token: CapturePauseToken = {
      captureInstanceId: this.captureInstanceId,
      trackId: track.id,
      ready: Promise.resolve(),
    };
    this.paused = true;
    this.pauseToken = token;
    track.enabled = false;
    this.workletQueue.clear();
    this.fallbackQueue.clear();
    if (this.fallbackTimer !== null) clearTimeout(this.fallbackTimer);
    this.fallbackTimer = null;
    this.pendingSamples = [];
    this.pendingStartFrame = 0;
    this.pendingStartMs = 0;
    this.pendingEndMs = 0;
    this.utteranceParts = [];
    this.utteranceByteLength = 0;
    this.utteranceStartMs = null;
    this.utteranceEndMs = null;
    this.vad?.reset();
    this.resampler?.close();
    this.resampler = new StreamingPcmResampler(this.context.sampleRate, 16_000);
    token.ready = this.outboundChain.then(async () => {
      if (this.closed || this.pauseToken !== token) {
        throw new Error("Talk capture changed before suspension completed.");
      }
      await this.transport.suspendCapture(this.captureInstanceId);
    });
    return token;
  }

  async resume(token: CapturePauseToken): Promise<void> {
    if (
      this.closed ||
      this.failed ||
      !this.context ||
      !this.audioTrack ||
      this.pauseToken !== token ||
      token.captureInstanceId !== this.captureInstanceId ||
      token.trackId !== this.audioTrack.id
    ) {
      throw new Error("Talk capture identity changed while the task selection was open.");
    }
    await token.ready;
    await this.transport.resumeCapture(this.captureInstanceId);
    if (this.closed || this.pauseToken !== token || !this.audioTrack) {
      throw new Error("Talk capture closed before it could resume.");
    }
    this.vad?.reset();
    this.audioTrack.enabled = true;
    this.pauseToken = null;
    this.paused = false;
  }

  private drainWorkletQueue(): void {
    for (let block = this.workletQueue.shift(); block; block = this.workletQueue.shift()) {
      this.consumeBlock(block);
    }
  }

  private consumeBlock(block: AudioWorkletBlock): void {
    if (this.paused || this.closed || this.failed) return;
    const resampler = this.resampler;
    const vad = this.vad;
    if (!resampler || !vad) return;
    const pcm = resampler.push(block.samples);
    if (pcm.length === 0) return;
    const endMs = block.observedAtMs;
    const durationMs = (pcm.length / 16_000) * 1_000;
    const startMs = Math.max(0, endMs - durationMs);
    const events = vad.push(pcm, this.outputFrame, startMs, endMs);
    this.outputFrame += pcm.length;
    for (const event of events) this.handleVadEvent(event);
  }

  private handleVadEvent(event: VadEvent): void {
    if (this.paused || this.closed || this.failed) return;
    if (event.type === "speech_start") {
      this.onSpeechStart?.();
      this.utteranceParts = [];
      this.utteranceByteLength = 0;
      this.utteranceStartMs = null;
      this.utteranceEndMs = null;
      this.appendAudio(event);
      return;
    }
    if (event.type === "audio") {
      this.appendAudio(event);
      return;
    }
    this.flushAudio();
    const bytes = concatBytes(this.utteranceParts);
    const receipt = {
      startFrame: event.startFrame,
      endFrame: event.endFrame,
      startMonotonicMs: this.utteranceStartMs ?? Math.round(event.startMonotonicMs),
      endMonotonicMs: this.utteranceEndMs ?? Math.round(event.endMonotonicMs),
    };
    this.utteranceParts = [];
    this.utteranceByteLength = 0;
    this.utteranceStartMs = null;
    this.utteranceEndMs = null;
    this.outboundChain = this.outboundChain.then(async () => {
      if (this.closed || this.failed) return;
      const digest = await crypto.subtle.digest("SHA-256", bytes.slice().buffer as ArrayBuffer);
      this.transport.endUtterance(this.captureInstanceId, {
        ...receipt,
        audioSha256: toHex(new Uint8Array(digest)),
      });
    }).catch((error: unknown) => this.fail(error));
  }

  private appendAudio(event: Extract<VadEvent, { samples: Int16Array }>): void {
    if (event.samples.length === 0) return;
    const bytes = new Uint8Array(event.samples.length * 2);
    const view = new DataView(bytes.buffer);
    for (let index = 0; index < event.samples.length; index += 1) {
      view.setInt16(index * 2, event.samples[index], true);
    }
    if (this.utteranceByteLength + bytes.length > 32_000_000) {
      throw new Error("This speech segment exceeded the safe audio buffer. Stop and retry with a shorter turn.");
    }
    this.utteranceParts.push(bytes);
    this.utteranceByteLength += bytes.length;
    if (this.pendingSamples.length === 0) {
      this.pendingStartFrame = event.startFrame;
      this.pendingStartMs = event.startMonotonicMs;
    }
    this.pendingSamples.push(...event.samples);
    this.pendingEndMs = event.endMonotonicMs;
    if (this.pendingSamples.length >= 1_600) this.flushAudio();
  }

  private flushAudio(): void {
    if (this.pendingSamples.length === 0) return;
    const samples = Int16Array.from(this.pendingSamples);
    const bytes = new Uint8Array(samples.length * 2);
    const view = new DataView(bytes.buffer);
    for (let index = 0; index < samples.length; index += 1) {
      view.setInt16(index * 2, samples[index], true);
    }
    const startMonotonicMs = Math.max(
      Math.round(this.pendingStartMs),
      this.utteranceEndMs ?? 0,
    );
    const endMonotonicMs = Math.max(startMonotonicMs, Math.round(this.pendingEndMs));
    this.utteranceStartMs ??= startMonotonicMs;
    this.utteranceEndMs = endMonotonicMs;
    const chunk = {
      startFrame: this.pendingStartFrame,
      endFrame: this.pendingStartFrame + samples.length,
      startMonotonicMs,
      endMonotonicMs,
      pcmBase64: toBase64(bytes),
    };
    this.outboundChain = this.outboundChain
      .then(() => {
        if (!this.closed && !this.failed) {
          this.transport.sendAudioChunk(this.captureInstanceId, chunk);
        }
      })
      .catch((error: unknown) => this.fail(error));
    this.pendingSamples = [];
  }

  private fail(error: unknown): void {
    if (this.failed || this.closed) return;
    this.failed = true;
    this.onFailure(
      error instanceof Error ? error.message : "The microphone audio pipeline failed.",
    );
    void this.close();
  }

  private readonly handleTrackEnded = (): void => {
    this.fail(new Error("The microphone device disconnected. Reconnect it, then restart Talk."));
  };

  private readonly handleContextStateChange = (): void => {
    if (this.context?.state === "suspended" && !this.closed) {
      this.fail(new Error("The microphone audio pipeline was suspended. Restart Talk to continue."));
    }
  };
}

function toBase64(value: Uint8Array): string {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function concatBytes(values: Uint8Array[]): Uint8Array {
  const total = values.reduce((size, value) => size + value.length, 0);
  const combined = new Uint8Array(total);
  let offset = 0;
  for (const value of values) {
    combined.set(value, offset);
    offset += value.length;
  }
  return combined;
}

function toHex(value: Uint8Array): string {
  return Array.from(value, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function sinc(value: number): number {
  if (Math.abs(value) < 1e-8) return 1;
  const angle = Math.PI * value;
  return Math.sin(angle) / angle;
}
