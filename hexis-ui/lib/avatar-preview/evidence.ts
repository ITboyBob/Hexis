import type { Event } from "./contracts";

export type EvidenceVerdict = {
  claim: string;
  status: "pass" | "fail" | "unknown";
  reason: string;
  source?: string;
  event_range?: [number, number];
};

export type EvidenceMetadata = Record<string, unknown>;

export type Evidence = {
  schema_version: "avatar-preview-evidence-v1";
  created_at: string;
  meta: EvidenceMetadata;
  events: Event[];
  verdicts: EvidenceVerdict[];
};

const requiredMetaKeys = [
  "artifacts",
  "resources",
  "runtime",
  "animation",
  "context",
  "model",
  "calls",
  "identities",
  "attribution",
  "media",
  "trajectory",
] as const;

export function makeEvidence(
  meta: EvidenceMetadata,
  events: readonly Event[],
  verdicts: readonly EvidenceVerdict[],
  options: { now?: () => Date } = {},
): Evidence {
  for (const key of requiredMetaKeys) {
    if (!(key in meta)) throw new Error(`证据meta缺少${key}`);
  }
  if (!Array.isArray(events)) throw new Error("事件轨迹必须是数组");
  if (!Array.isArray(verdicts)) throw new Error("验收主张必须是数组");
  for (const verdict of verdicts) {
    if (verdict.status !== "pass" && verdict.status !== "fail" && verdict.status !== "unknown") {
      throw new Error("验收主张状态无效");
    }
    if (!verdict.claim.trim() || !verdict.reason.trim()) {
      throw new Error("验收主张和理由不能为空");
    }
  }
  return {
    schema_version: "avatar-preview-evidence-v1",
    created_at: (options.now ?? (() => new Date()))().toISOString(),
    meta,
    events: [...events],
    verdicts: [...verdicts],
  };
}

export function downloadEvidence(value: Evidence, filename = "avatar-preview-evidence.json"): void {
  const blob = new Blob([JSON.stringify(value, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}
