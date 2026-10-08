import config from "./speech-detection-config.json";
import type { BargeInTarget } from "./capture";

export type ConfirmedBargeInTarget = BargeInTarget & {
  captureGeneration: number;
};

type TargetIdentity = BargeInTarget & { captureGeneration: number };

export class BargeInGate {
  private target: TargetIdentity | null = null;
  private probabilities: number[] = [];
  private handledTarget: string | null = null;

  setTarget(target: TargetIdentity | null): void {
    if (sameTarget(this.target, target)) return;
    this.target = target ? { ...target } : null;
    this.probabilities = [];
    this.handledTarget = null;
  }

  observe(probability: number, generation: number, sampledTarget: TargetIdentity | null = null): ConfirmedBargeInTarget | null {
    if (!Number.isFinite(probability) || probability < 0 || probability > 1) {
      throw new Error("Barge-in gate probability must be between 0 and 1.");
    }
    const target = this.target;
    if (!target || !sampledTarget || target.captureGeneration !== generation || sampledTarget.captureGeneration !== generation || !sameTarget(target, sampledTarget)) {
      this.probabilities = [];
      return null;
    }
    const targetKey = keyOf(target);
    if (this.handledTarget === targetKey) return null;
    this.probabilities.push(probability);
    const maxWindow = config.barge_in_gate.normal.window_frames;
    if (this.probabilities.length > maxWindow) this.probabilities.shift();

    const fast = config.barge_in_gate.fast;
    const fastWindow = this.probabilities.slice(-fast.window_frames);
    if (
      probability >= fast.probability_threshold &&
      fastWindow.length === fast.window_frames &&
      fastWindow.filter((value) => value >= fast.probability_threshold).length >= fast.minimum_positive_frames
    ) {
      this.handledTarget = targetKey;
      return { ...target };
    }

    const normal = config.barge_in_gate.normal;
    if (
      probability >= normal.probability_threshold &&
      this.probabilities.length === normal.window_frames &&
      this.probabilities.filter((value) => value >= normal.probability_threshold).length >= normal.minimum_positive_frames
    ) {
      this.handledTarget = targetKey;
      return { ...target };
    }
    return null;
  }

  reset(): void {
    this.target = null;
    this.probabilities = [];
    this.handledTarget = null;
  }
}

function keyOf(target: TargetIdentity): string {
  return `${target.captureGeneration}:${target.response_id}:${target.output_epoch}:${target.playback_id}`;
}

function sameTarget(left: TargetIdentity | null, right: TargetIdentity | null): boolean {
  return left === null || right === null
    ? left === right
    : keyOf(left) === keyOf(right);
}
