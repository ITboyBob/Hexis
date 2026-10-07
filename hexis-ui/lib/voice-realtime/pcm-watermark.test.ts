import { PcmWatermark } from "./speech-detector";

describe("PcmWatermark", () => {
  it("retains partial frames and emits raw PCM only after a contiguous result prefix", () => {
    const watermark = new PcmWatermark({ sampleRate: 16000, frameSamples: 512, maxBufferedMs: 256 });
    expect(watermark.append(new Int16Array(300).fill(11), 0, 0, 18.75)).toEqual([]);
    const frames = watermark.append(new Int16Array(724).fill(22), 300, 18.75, 64);
    expect(frames.map(({ startSample, endSample }) => [startSample, endSample])).toEqual([[0, 512], [512, 1024]]);
    expect(watermark.pendingSampleCount).toBe(1024);

    expect(watermark.resolve(frames[1].frameId, frames[1].generation, 0.9)).toEqual([]);
    const released = watermark.resolve(frames[0].frameId, frames[0].generation, 0.8);
    expect(released).toHaveLength(1);
    expect(released[0]).toMatchObject({ startSample: 0, endSample: 1024, probabilities: [0.8, 0.9] });
    expect(released[0].samples).toEqual(new Int16Array([...new Array(300).fill(11), ...new Array(724).fill(22)]));
    expect(watermark.pendingSampleCount).toBe(0);
  });

  it("rejects gaps, duplicate results, and buffers larger than the configured budget", () => {
    const watermark = new PcmWatermark({ sampleRate: 16000, frameSamples: 512, maxBufferedMs: 256 });
    const frames = watermark.append(new Int16Array(512), 10, 0, 32);
    expect(() => watermark.append(new Int16Array(512), 600, 32, 64)).toThrow(/gap/);
    watermark.resolve(frames[0].frameId, frames[0].generation, 0.7);
    expect(() => watermark.resolve(frames[0].frameId, frames[0].generation, 0.7)).toThrow(/already/);

    const overBudget = new PcmWatermark({ sampleRate: 16000, frameSamples: 512, maxBufferedMs: 256 });
    expect(() => overBudget.append(new Int16Array(4609), 0, 0, 288.0625)).toThrow(/budget/);
    expect(overBudget.pendingSampleCount).toBe(0);
  });

  it("invalidates every pending frame when the capture generation resets", () => {
    const watermark = new PcmWatermark({ sampleRate: 16000, frameSamples: 512, maxBufferedMs: 256 });
    const old = watermark.append(new Int16Array(512), 0, 0, 32)[0];
    watermark.reset(1);
    expect(() => watermark.resolve(old.frameId, old.generation, 0.9)).toThrow(/stale generation/);
    expect(watermark.pendingSampleCount).toBe(0);
    expect(watermark.generation).toBe(1);
  });
});
