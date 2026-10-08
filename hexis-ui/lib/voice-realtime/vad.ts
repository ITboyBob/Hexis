export type VadEvent =
  | {
      type: "speech_start" | "audio";
      samples: Int16Array;
      startFrame: number;
      endFrame: number;
      startMonotonicMs: number;
      endMonotonicMs: number;
    }
  | {
      type: "utterance_end";
      startFrame: number;
      endFrame: number;
      startMonotonicMs: number;
      endMonotonicMs: number;
    };

export type VadOptions = {
  sampleRate: number;
  preRollMs?: number;
  hangoverMs?: number;
  silenceThresholdMs?: number;
  minimumSpeechMs?: number;
  absoluteThreshold?: number;
};

// New Talk capture uses these probability-gated states; StreamingVad below is
// retained for older consumers and is not the product capture path.
export { SpeechGate } from "./speech-gate";
export type { SpeechFrame, SpeechGateEvent } from "./speech-gate";

type AudioBlock = {
  samples: Int16Array;
  startFrame: number;
  startMonotonicMs: number;
  endMonotonicMs: number;
};

/** @deprecated Product Talk uses SpeechGate and Worker probabilities. */
export class StreamingVad {
  private readonly preRollFrames: number;
  private readonly hangoverFrames: number;
  private readonly silenceFrames: number;
  private readonly minimumSpeechFrames: number;
  private readonly absoluteThreshold: number;
  private readonly preRoll: AudioBlock[] = [];
  private preRollFrameCount = 0;
  private candidate: AudioBlock[] = [];
  private candidateFrameCount = 0;
  private currentStartFrame = 0;
  private currentStartMs = 0;
  private currentEndFrame = 0;
  private currentEndMs = 0;
  private silenceFrameCount = 0;
  private activeState = false;
  private noiseFloor = 180;

  constructor(options: VadOptions) {
    if (!Number.isInteger(options.sampleRate) || options.sampleRate <= 0) {
      throw new Error("VAD sampleRate must be a positive integer.");
    }
    this.preRollFrames = msToFrames(options.preRollMs ?? 240, options.sampleRate);
    this.hangoverFrames = msToFrames(options.hangoverMs ?? 240, options.sampleRate);
    this.silenceFrames = msToFrames(
      options.silenceThresholdMs ?? 520,
      options.sampleRate,
    );
    this.minimumSpeechFrames = msToFrames(
      options.minimumSpeechMs ?? 60,
      options.sampleRate,
    );
    this.absoluteThreshold = options.absoluteThreshold ?? 650;
  }

  get active(): boolean {
    return this.activeState;
  }

  push(
    samples: Int16Array,
    startFrame: number,
    startMonotonicMs: number,
    endMonotonicMs: number,
  ): VadEvent[] {
    if (!(samples instanceof Int16Array) || samples.length === 0) return [];
    if (!Number.isSafeInteger(startFrame) || startFrame < 0) {
      throw new Error("VAD frame positions must be non-negative safe integers.");
    }
    if (endMonotonicMs < startMonotonicMs) {
      throw new Error("VAD monotonic range end precedes its start.");
    }
    const block: AudioBlock = {
      samples: samples.slice(),
      startFrame,
      startMonotonicMs,
      endMonotonicMs,
    };
    const voiced = this.isVoice(block.samples);

    if (!this.activeState) {
      if (!voiced) {
        this.updateNoiseFloor(block.samples);
        // Rejected short speech still occupies frames in the pre-roll timeline.
        for (const candidate of this.candidate) this.keepPreRoll(candidate);
        this.keepPreRoll(block);
        this.candidate = [];
        this.candidateFrameCount = 0;
        return [];
      }
      this.candidate.push(block);
      this.candidateFrameCount += block.samples.length;
      if (this.candidateFrameCount < this.minimumSpeechFrames) return [];

      const leading = this.preRoll;
      const combined = [...leading, ...this.candidate];
      this.currentStartFrame = leading[0]?.startFrame ?? this.candidate[0].startFrame;
      this.currentStartMs =
        leading[0]?.startMonotonicMs ?? this.candidate[0].startMonotonicMs;
      const candidateEnd = this.candidate[this.candidate.length - 1];
      this.currentEndFrame = candidateEnd.startFrame + candidateEnd.samples.length;
      this.currentEndMs = candidateEnd.endMonotonicMs;
      this.activeState = true;
      this.silenceFrameCount = 0;
      this.preRoll.length = 0;
      this.preRollFrameCount = 0;
      this.candidate = [];
      this.candidateFrameCount = 0;
      return [this.eventFromBlocks("speech_start", combined)];
    }

    this.currentEndFrame = block.startFrame + block.samples.length;
    this.currentEndMs = block.endMonotonicMs;
    if (voiced) {
      this.silenceFrameCount = 0;
    } else {
      this.silenceFrameCount += block.samples.length;
    }
    const audioEvent = this.eventFromBlocks("audio", [block]);
    if (
      !voiced &&
      this.silenceFrameCount >= this.hangoverFrames &&
      this.silenceFrameCount >= this.silenceFrames
    ) {
      const endEvent: VadEvent = {
        type: "utterance_end",
        startFrame: this.currentStartFrame,
        endFrame: this.currentEndFrame,
        startMonotonicMs: this.currentStartMs,
        endMonotonicMs: this.currentEndMs,
      };
      this.resetActive();
      return [audioEvent, endEvent];
    }
    return [audioEvent];
  }

  reset(): void {
    this.preRoll.length = 0;
    this.preRollFrameCount = 0;
    this.candidate = [];
    this.candidateFrameCount = 0;
    this.resetActive();
  }

  private isVoice(samples: Int16Array): boolean {
    let squares = 0;
    for (const sample of samples) squares += sample * sample;
    const rms = Math.sqrt(squares / samples.length);
    return rms >= Math.max(this.absoluteThreshold, this.noiseFloor * 2.8);
  }

  private updateNoiseFloor(samples: Int16Array): void {
    let squares = 0;
    for (const sample of samples) squares += sample * sample;
    const rms = Math.sqrt(squares / samples.length);
    this.noiseFloor = Math.max(80, Math.min(1_400, this.noiseFloor * 0.94 + rms * 0.06));
  }

  private keepPreRoll(block: AudioBlock): void {
    if (this.preRollFrames === 0) return;
    this.preRoll.push(block);
    this.preRollFrameCount += block.samples.length;
    while (
      this.preRoll.length > 1 &&
      this.preRollFrameCount - this.preRoll[0].samples.length >= this.preRollFrames
    ) {
      this.preRollFrameCount -= this.preRoll.shift()!.samples.length;
    }
  }

  private eventFromBlocks(type: "speech_start" | "audio", blocks: AudioBlock[]): VadEvent {
    const samples = new Int16Array(
      blocks.reduce((count, block) => count + block.samples.length, 0),
    );
    let offset = 0;
    for (const block of blocks) {
      samples.set(block.samples, offset);
      offset += block.samples.length;
    }
    return {
      type,
      samples,
      startFrame: blocks[0].startFrame,
      endFrame: blocks[blocks.length - 1].startFrame + blocks[blocks.length - 1].samples.length,
      startMonotonicMs: blocks[0].startMonotonicMs,
      endMonotonicMs: blocks[blocks.length - 1].endMonotonicMs,
    };
  }

  private resetActive(): void {
    this.activeState = false;
    this.silenceFrameCount = 0;
    this.currentStartFrame = 0;
    this.currentStartMs = 0;
    this.currentEndFrame = 0;
    this.currentEndMs = 0;
  }
}

function msToFrames(ms: number, sampleRate: number): number {
  if (!Number.isFinite(ms) || ms < 0) throw new Error("VAD timings must be non-negative.");
  return Math.round((ms / 1_000) * sampleRate);
}
