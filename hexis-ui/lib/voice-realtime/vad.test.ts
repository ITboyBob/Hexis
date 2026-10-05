import { describe, expect, it } from "vitest";

import { StreamingVad } from "./vad";

function pcm(value: number, count = 160) {
  return new Int16Array(count).fill(value);
}

describe("StreamingVad", () => {
  it("includes pre-roll, holds through a pause, and ends once after hangover", () => {
    const vad = new StreamingVad({
      sampleRate: 16_000,
      preRollMs: 20,
      hangoverMs: 40,
      silenceThresholdMs: 60,
      minimumSpeechMs: 20,
    });
    expect(vad.push(pcm(0), 0, 0, 10)).toEqual([]);
    expect(vad.push(pcm(0), 160, 10, 20)).toEqual([]);

    expect(vad.push(pcm(18_000), 320, 20, 30)).toEqual([]);
    const confirmed = vad.push(pcm(18_000), 480, 30, 40);
    expect(confirmed[0]?.type).toBe("speech_start");
    if (confirmed[0]?.type === "speech_start") {
      expect(confirmed[0].startFrame).toBe(0);
      expect(confirmed[0].samples.slice(0, 320)).toEqual(pcm(0, 320));
    }

    expect(vad.push(pcm(0), 640, 40, 50).some((event) => event.type === "utterance_end")).toBe(false);
    expect(vad.push(pcm(18_000), 800, 50, 60).some((event) => event.type === "utterance_end")).toBe(false);
    expect(vad.push(pcm(0), 960, 60, 70).some((event) => event.type === "utterance_end")).toBe(false);
    expect(vad.push(pcm(0), 1120, 70, 80).some((event) => event.type === "utterance_end")).toBe(false);
    expect(vad.push(pcm(0), 1280, 80, 90).map((event) => event.type)).toEqual([
      "audio",
    ]);
    expect(vad.push(pcm(0), 1440, 90, 100).map((event) => event.type)).toEqual([
      "audio",
    ]);
    expect(vad.push(pcm(0), 1600, 100, 110).map((event) => event.type)).toEqual([
      "audio",
    ]);
    expect(vad.push(pcm(0), 1760, 110, 120).map((event) => event.type)).toEqual([
      "audio",
      "utterance_end",
    ]);
    expect(vad.push(pcm(0), 1920, 120, 130)).toEqual([]);

    expect(vad.push(pcm(18_000), 2080, 130, 140)).toEqual([]);
    const nextUtterance = vad.push(pcm(18_000), 2240, 140, 150);
    expect(nextUtterance[0]?.type).toBe("speech_start");
    if (nextUtterance[0]?.type === "speech_start") {
      expect(nextUtterance[0].startFrame).toBeGreaterThanOrEqual(1920);
    }
  });

  it("does not commit healthy silence or short noise", () => {
    const vad = new StreamingVad({ sampleRate: 16_000, minimumSpeechMs: 30 });
    for (let index = 0; index < 80; index += 1) {
      expect(vad.push(pcm(index % 2 ? 100 : -100), index * 160, index * 10, index * 10 + 10))
        .not.toContainEqual(expect.objectContaining({ type: "utterance_end" }));
    }
    expect(vad.active).toBe(false);
  });
});
