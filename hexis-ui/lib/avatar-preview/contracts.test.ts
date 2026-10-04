import { describe, expect, it } from "vitest";
import { EMOTIONS, WORDSET_VERSION, eyeKey, isEmotion } from "./contracts";

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
});
