import { describe, expect, expectTypeOf, it } from "vitest";
import { EMOTIONS, WORDSET_VERSION, eyeKey, isEmotion } from "./contracts";
import type { Playback } from "./contracts";

describe("封闭情绪词表", () => {
  it("包含 14 个唯一的 v1 机器值", () => {
    expect(EMOTIONS).toHaveLength(14);
    expect(new Set(EMOTIONS).size).toBe(14);
    expect(WORDSET_VERSION).toBe("v1");
  });

  it("neutral 仅映射到 idle 眼形资源", () => {
    expect(eyeKey("neutral")).toBe("idle");
    expect(eyeKey("happy")).toBe("happy");
  });

  it.each(["joy", "Happy", " happy", "happy sad", '{"label":"happy"}', null, 1])(
    "拒绝非法业务值 %s",
    (value) => expect(isEmotion(value)).toBe(false),
  );

  it.each([
    "neutral", "happy", "excited", "curious", "surprised", "confused",
    "suspicious", "angry", "sad", "scared", "proud", "shy", "bored", "playful",
  ])("接受精确标签 %s", (value) => expect(isEmotion(value)).toBe(true));

  it("播放反馈契约携带输出媒体身份与真实反馈字段", () => {
    expectTypeOf<Playback>().toEqualTypeOf<{
      playback_id: string;
      output_epoch: number;
      response_id?: string;
      media_session_id?: string;
      phase: Playback["phase"];
      progress: number;
      played_ms: number;
      interval_ms: number;
      amplitude: number;
      source: Playback["source"];
    }>();
  });
});
