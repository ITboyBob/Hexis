import config from "./speech-detection-config.json";

export type SpeechFrame = {
  generation: number;
  frameId: number;
  startSample: number;
  endSample: number;
  startMonotonicMs: number;
  endMonotonicMs: number;
  samples: Int16Array;
  probability: number;
};

export type SpeechGateEvent =
  | { type: "input_start" | "input_audio"; frame: Omit<SpeechFrame, "generation" | "frameId" | "probability"> }
  | { type: "input_end"; startSample: number; endSample: number; endMonotonicMs: number }
  | { type: "candidate_rejected"; startSample: number; endSample: number; reason: "probability" | "buffer_limit" };

export class SpeechGate {
  private readonly activeThreshold = config.capture.active_probability_threshold;
  private readonly candidateRejectWindow = config.capture.candidate_reject_window_frames;
  private readonly candidateRejectThreshold = config.capture.candidate_reject_probability_threshold;
  private readonly candidateBufferLimitSamples = Math.floor(
    (config.capture.candidate_buffer_limit_ms * config.audio.sample_rate) / 1000,
  );
  private readonly silenceLimitSamples = Math.floor(
    (config.capture.silence_threshold_ms * config.audio.sample_rate) / 1000,
  );
  private readonly preRollLimitSamples = Math.floor(
    (config.capture.pre_roll_ms * config.audio.sample_rate) / 1000,
  );
  private readonly inputWindowFrames = config.input_gate.window_frames;
  private readonly inputPositiveFrames = config.input_gate.minimum_positive_frames;
  private readonly inputThreshold = config.input_gate.probability_threshold;
  private preRoll: SpeechFrame[] = [];
  private preRollSamples = 0;
  private candidate: SpeechFrame[] = [];
  private candidateSamples = 0;
  private active = false;
  private silenceSamples = 0;
  private activeStartSample = 0;

  consume(frame: SpeechFrame): SpeechGateEvent[] {
    this.validateFrame(frame);
    if (this.active) return this.consumeActive(frame);
    return this.consumeCandidate(frame);
  }

  reset(): void {
    this.preRoll = [];
    this.preRollSamples = 0;
    this.candidate = [];
    this.candidateSamples = 0;
    this.active = false;
    this.silenceSamples = 0;
    this.activeStartSample = 0;
  }

  private consumeCandidate(frame: SpeechFrame): SpeechGateEvent[] {
    if (this.candidate.length === 0 && frame.probability < this.activeThreshold) {
      this.keepPreRoll(frame);
      return [];
    }

    this.candidate.push(frame);
    this.candidateSamples += frame.samples.length;
    if (this.candidateSamples > this.candidateBufferLimitSamples) {
      const rejected = this.rejectCandidate("buffer_limit");
      return [rejected];
    }

    if (
      this.candidate.length >= this.inputWindowFrames &&
      this.candidate.slice(-this.inputWindowFrames).filter((item) => item.probability >= this.inputThreshold).length >= this.inputPositiveFrames
    ) {
      const prefix = [...this.preRoll, ...this.candidate];
      this.active = true;
      this.activeStartSample = prefix[0]?.startSample ?? frame.startSample;
      this.silenceSamples = 0;
      for (let index = this.candidate.length - 1; index >= 0; index -= 1) {
        const item = this.candidate[index];
        if (item.probability >= this.activeThreshold) break;
        this.silenceSamples += item.samples.length;
      }
      this.preRoll = [];
      this.preRollSamples = 0;
      this.candidate = [];
      this.candidateSamples = 0;
      return prefix.map((item, index) => ({
        type: index === 0 ? "input_start" : "input_audio",
        frame: this.toFrame(item),
      }));
    }

    const recent = this.candidate.slice(-this.candidateRejectWindow);
    if (
      recent.length === this.candidateRejectWindow &&
      recent.every((item) => item.probability < this.candidateRejectThreshold)
    ) {
      return [this.rejectCandidate("probability")];
    }
    return [];
  }

  private consumeActive(frame: SpeechFrame): SpeechGateEvent[] {
    if (frame.probability >= this.activeThreshold) this.silenceSamples = 0;
    else this.silenceSamples += frame.samples.length;
    const events: SpeechGateEvent[] = [{ type: "input_audio", frame: this.toFrame(frame) }];
    if (this.silenceSamples >= this.silenceLimitSamples) {
      events.push({
        type: "input_end",
        startSample: this.activeStartSample,
        endSample: frame.endSample,
        endMonotonicMs: frame.endMonotonicMs,
      });
      this.active = false;
      this.activeStartSample = 0;
      this.silenceSamples = 0;
    }
    return events;
  }

  private rejectCandidate(reason: "probability" | "buffer_limit"): SpeechGateEvent {
    const startSample = this.candidate[0]?.startSample ?? 0;
    const endSample = this.candidate.at(-1)?.endSample ?? startSample;
    for (const frame of this.candidate) this.keepPreRoll(frame);
    this.candidate = [];
    this.candidateSamples = 0;
    return { type: "candidate_rejected", startSample, endSample, reason };
  }

  private keepPreRoll(frame: SpeechFrame): void {
    if (this.preRollLimitSamples === 0) return;
    this.preRoll.push(frame);
    this.preRollSamples += frame.samples.length;
    while (this.preRoll.length > 0 && this.preRollSamples > this.preRollLimitSamples) {
      const first = this.preRoll[0];
      const excess = this.preRollSamples - this.preRollLimitSamples;
      const trim = Math.min(excess, first.samples.length);
      if (trim === first.samples.length) {
        this.preRoll.shift();
      } else {
        this.preRoll[0] = {
          ...first,
          startSample: first.startSample + trim,
          startMonotonicMs: first.startMonotonicMs + (trim * 1000) / config.audio.sample_rate,
          samples: first.samples.slice(trim),
        };
      }
      this.preRollSamples -= trim;
    }
  }

  private validateFrame(frame: SpeechFrame): void {
    if (
      !Number.isSafeInteger(frame.generation) || frame.generation < 0 ||
      !Number.isSafeInteger(frame.frameId) || frame.frameId < 0 ||
      !Number.isSafeInteger(frame.startSample) || frame.startSample < 0 ||
      frame.endSample - frame.startSample !== frame.samples.length ||
      !(frame.samples instanceof Int16Array) || frame.samples.length === 0 ||
      !Number.isFinite(frame.startMonotonicMs) || !Number.isFinite(frame.endMonotonicMs) ||
      frame.endMonotonicMs < frame.startMonotonicMs ||
      !Number.isFinite(frame.probability) || frame.probability < 0 || frame.probability > 1
    ) {
      throw new Error("Speech gate received an invalid frame or probability.");
    }
  }

  private toFrame(frame: SpeechFrame): Omit<SpeechFrame, "generation" | "frameId" | "probability"> {
    return {
      startSample: frame.startSample,
      endSample: frame.endSample,
      startMonotonicMs: frame.startMonotonicMs,
      endMonotonicMs: frame.endMonotonicMs,
      samples: frame.samples.slice(),
    };
  }
}
