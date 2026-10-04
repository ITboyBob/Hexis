import type { Character } from "./engine";

export type Effect = {
  id: string;
  kind: "bounce" | "burst";
  duration_ms: number;
};

export type EffectSnapshot = { active?: string; queued: string[]; min_gap_ms: number };

export class Effects {
  private queue: Effect[] = [];
  private active?: Effect;
  private timer?: ReturnType<typeof setTimeout>;
  private timerIsGap = false;
  private generation = 0;
  private dead = false;
  private reduced = false;

  constructor(
    private readonly character: Character,
    private gap_ms: number,
    private readonly onChange?: (snapshot: EffectSnapshot) => void,
  ) {
    if (!Number.isFinite(gap_ms) || gap_ms < 0) {
      throw new Error("视觉效果最小间隔无效");
    }
  }

  enqueue(effect: Effect): void {
    if (this.dead || this.reduced) return;
    if (this.active?.id === effect.id || this.queue.some((item) => item.id === effect.id)) return;
    if (!Number.isFinite(effect.duration_ms) || effect.duration_ms <= 0) {
      throw new Error("视觉效果持续时间无效");
    }
    this.queue.push(effect);
    this.notify();
    this.pump();
  }

  private notify(): void {
    this.onChange?.({ active: this.active?.id, queued: this.queue.map(({ id }) => id), min_gap_ms: this.gap_ms });
  }

  setGap(milliseconds: number): void {
    if (!Number.isFinite(milliseconds) || milliseconds < 0) throw new Error("视觉效果最小间隔无效");
    if (milliseconds === this.gap_ms) return;
    this.gap_ms = milliseconds;
    if (this.timer && this.timerIsGap) {
      clearTimeout(this.timer);
      this.timer = undefined;
      this.timerIsGap = false;
      this.generation += 1;
      this.waitForGap(this.generation);
    }
    this.notify();
  }

  private pump(): void {
    if (this.dead || this.reduced || this.active || this.timer || this.queue.length === 0) return;
    const effect = this.queue.shift()!;
    this.active = effect;
    this.notify();
    const token = this.generation;
    if (effect.kind === "bounce") this.character.bounceOnce();
    else this.character.burstOnce();
    this.schedule(effect.duration_ms, token, () => {
      this.character.stopVisualEffect();
      this.active = undefined;
      this.notify();
      this.waitForGap(token);
    });
  }

  private schedule(delay: number, token: number, callback: () => void): void {
    const handle = setTimeout(() => {
      if (this.dead || token !== this.generation) return;
      if (this.timer === handle) {
        this.timer = undefined;
        this.timerIsGap = false;
      }
      callback();
    }, delay);
    this.timer = handle;
  }

  private waitForGap(token: number): void {
    if (this.gap_ms === 0) {
      this.pump();
      return;
    }
    this.timerIsGap = true;
    this.schedule(this.gap_ms, token, () => this.pump());
  }

  stop(id: string): void {
    if (this.dead) return;
    const queuedCount = this.queue.length;
    this.queue = this.queue.filter((effect) => effect.id !== id);
    if (this.active?.id !== id) {
      if (this.queue.length !== queuedCount) this.notify();
      return;
    }
    clearTimeout(this.timer);
    this.timer = undefined;
    this.timerIsGap = false;
    this.generation += 1;
    this.character.stopVisualEffect();
    this.active = undefined;
    this.notify();
    this.waitForGap(this.generation);
  }

  reset(): void {
    if (this.dead) return;
    this.cancelEffects();
    this.character.resetVisual();
  }

  cancelEffects(): void {
    if (this.dead) return;
    this.generation += 1;
    clearTimeout(this.timer);
    this.timer = undefined;
    this.timerIsGap = false;
    this.queue = [];
    this.active = undefined;
    this.notify();
  }

  setReduced(value: boolean): void {
    if (this.dead || value === this.reduced) return;
    this.reduced = value;
    if (value) this.reset();
    else this.pump();
  }

  destroy(): void {
    if (this.dead) return;
    this.reset();
    this.dead = true;
    this.character.destroy();
  }
}
