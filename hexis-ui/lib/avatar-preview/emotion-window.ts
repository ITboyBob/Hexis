import type { Identity, VisualWindowFact, VisualWindowOwner } from "./contracts";
import { identityKey } from "./projection";

type Timer = ReturnType<typeof setTimeout>;
type Entry = { identity: Identity; fact: VisualWindowFact; deadline: number; timer?: unknown };

export type EmotionWindowOptions = {
  owner: VisualWindowOwner;
  now?: () => number;
  schedule?: (callback: () => void, delayMs: number) => unknown;
  cancel?: (handle: unknown) => void;
  onChange?: (identity: Identity, fact: VisualWindowFact) => void;
};

export class EmotionWindowHost {
  private readonly entries = new Map<string, Entry>();
  private readonly now: () => number;
  private readonly schedule: (callback: () => void, delayMs: number) => unknown;
  private readonly cancel: (handle: unknown) => void;

  constructor(private readonly options: EmotionWindowOptions) {
    this.now = options.now ?? (() => performance.now());
    this.schedule = options.schedule ?? ((callback, delay) => setTimeout(callback, delay));
    this.cancel = options.cancel ?? ((handle) => clearTimeout(handle as Timer));
  }

  open(identity: Identity, timeoutMs: number): VisualWindowFact {
    const key = identityKey(identity);
    const existing = this.entries.get(key);
    if (existing) return existing.fact;
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new RangeError("timeoutMs 必须为正数");
    const entry: Entry = { identity: { ...identity }, fact: { status: "open", owner: this.options.owner, decision_seq: 0 }, deadline: this.now() + timeoutMs };
    entry.timer = this.schedule(() => this.expire(key, entry), timeoutMs);
    this.entries.set(key, entry);
    return entry.fact;
  }

  current(identity: Identity): VisualWindowFact | undefined {
    return this.entries.get(identityKey(identity))?.fact;
  }

  acceptResult(identity: Identity): { accepted: boolean; fact?: VisualWindowFact } {
    const key = identityKey(identity);
    const entry = this.entries.get(key);
    if (!entry || entry.fact.status !== "open") return { accepted: false, fact: entry?.fact };
    if (this.now() >= entry.deadline) {
      this.close(key, entry);
      return { accepted: false, fact: entry.fact };
    }
    if (entry.timer !== undefined) this.cancel(entry.timer);
    entry.fact = { status: "accepted", owner: this.options.owner, decision_seq: entry.fact.decision_seq + 1 };
    this.options.onChange?.(entry.identity, entry.fact);
    return { accepted: true, fact: entry.fact };
  }

  private expire(key: string, entry: Entry): void {
    if (this.entries.get(key) !== entry || entry.fact.status !== "open") return;
    const remaining = entry.deadline - this.now();
    if (remaining > 0) {
      entry.timer = this.schedule(() => this.expire(key, entry), remaining);
      return;
    }
    this.close(key, entry);
  }

  private close(key: string, entry: Entry): void {
    if (this.entries.get(key) !== entry || entry.fact.status !== "open") return;
    entry.fact = { status: "closed", owner: this.options.owner, decision_seq: entry.fact.decision_seq + 1, reason: "model_timeout" };
    entry.timer = undefined;
    this.options.onChange?.(entry.identity, entry.fact);
  }
}
