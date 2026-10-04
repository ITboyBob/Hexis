import { describe, expect, it, vi } from "vitest";
import { downloadEvidence, makeEvidence } from "./evidence";
import type { Event } from "./contracts";

const event: Event = {
  event_id: "event-1",
  session_id: "session-a",
  stream_id: "preview-local",
  producer_seq: 1,
  payload: { type: "input_activity", activity_id: "mic", speaking: true, source: "manual" },
};

const meta = {
  artifacts: {
    design: { version: "v1.7", sha256: "810978d3fa2d8221c366ff3de6e5aa552fc103f264b3dfec2c65d4633b7b5af0" },
    plan: { version: "v1.3", sha256: "plan-sha" },
    wordset: { version: "v1", sha256: "wordset-sha" },
    prompt: { version: "v1", sha256: "prompt-sha" },
  },
  resources: {
    commit: "647e9bd7c60290c42a738fad586589b3f36a4680",
    files: { "character.js": { upstream_sha256: "upstream", adapted_sha256: "adapted", adapted: true } },
  },
  runtime: { browser: "Chromium", device: "desktop", viewport: "1280x1024", actual_size_px: 259 },
  animation: { motion_limit_px: 12, effect_min_gap_ms: 20 },
  context: { source: "fixture", allowed_categories: ["identity", "worldview", "goals", "relationships"] },
  model: { provider: "provider", model: "qwen-plus", credential_source: "DASHSCOPE_API_KEY", business_budget: "none", timeout_seconds: 30 },
  calls: { logical_call_count: 1, transport_attempts: null },
  identities: { current: "identity-key", playback: "playback-key", activities: ["activity-key"] },
  attribution: { state_projection: "projection.ts", property_owners: { eye: "emotion", pose: "attention", playback: "player" } },
  media: { screenshots: ["cache://shot.png"], videos: ["cache://video.webm"] },
  trajectory: { complete: true, event_count: 1, dropped_event_count: 0 },
};

describe("Avatar预览证据", () => {
  it("生成带固定schema、完整meta和逐主张verdict的证据", () => {
    const evidence = makeEvidence(meta, [event], [
      { claim: "182转换覆盖", status: "pass", reason: "E2E逐对执行", event_range: [1, 1] },
    ], { now: () => new Date("2026-10-04T12:00:00.000Z") });

    expect(evidence).toMatchObject({
      schema_version: "avatar-preview-evidence-v1",
      created_at: "2026-10-04T12:00:00.000Z",
      meta,
      events: [event],
      verdicts: [{ claim: "182转换覆盖", status: "pass", reason: "E2E逐对执行", event_range: [1, 1] }],
    });
  });

  it("拒绝无效verdict并生成可释放的下载Blob", () => {
    expect(() => makeEvidence(meta, [], [
      { claim: "无效状态", status: "invalid" as never, reason: "状态不在协议内" },
    ])).toThrow("验收主张状态无效");

    const createObjectURL = vi.fn(() => "blob:evidence");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const evidence = makeEvidence(meta, [event], []);
    downloadEvidence(evidence, "avatar-preview-evidence.json");
    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(click).toHaveBeenCalledOnce();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:evidence");
    click.mockRestore();
    vi.unstubAllGlobals();
  });
});
