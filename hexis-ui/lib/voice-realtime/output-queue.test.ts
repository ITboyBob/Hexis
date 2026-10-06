import { describe, expect, it } from "vitest";
import { NativeOutputQueue } from "./output-queue";

const chunk = (chunk_seq: number) => ({
  data: new Uint8Array([chunk_seq & 0xff, 0]).buffer,
  segment_id: "segment-1",
  chunk_seq,
});

describe("NativeOutputQueue", () => {
  it("delivers arriving PCM immediately and closes after queued frames drain", async () => {
    const queue = new NativeOutputQueue({ sampleRate: 1000, maxBufferedMs: 2 });
    const iterator = queue[Symbol.asyncIterator]();
    const pending = iterator.next();
    queue.push(chunk(1));
    expect(await pending).toMatchObject({ value: { chunk_seq: 1 }, done: false });
    queue.push(chunk(2));
    queue.close();
    expect(await iterator.next()).toMatchObject({ value: { chunk_seq: 2 }, done: false });
    expect(await iterator.next()).toMatchObject({ done: true });
    expect(() => queue.push(chunk(3))).toThrow(/closed/);
  });

  it("生成结束后仍可因播放故障立即清空已关闭队列", async () => {
    const queue = new NativeOutputQueue();
    queue.push(chunk(1));
    queue.close();
    const error = new Error("device failure after generation done");
    queue.fail(error);
    await expect(queue[Symbol.asyncIterator]().next()).rejects.toBe(error);
    expect(queue.bufferedMs).toBe(0);
  });

  it("fails loudly when full or failed while a reader waits", async () => {
    const queue = new NativeOutputQueue({ sampleRate: 1000, maxBufferedMs: 1 });
    queue.push(chunk(1));
    expect(() => queue.push(chunk(2))).toThrow(/缓冲.*上限/);
    const error = new Error("transport failed");
    queue.fail(error);
    const iterator = queue[Symbol.asyncIterator]();
    await expect(iterator.next()).rejects.toBe(error);
    expect(queue.size).toBe(0);
    expect(queue.bufferedMs).toBe(0);
    expect(() => queue.push(chunk(3))).toThrow(error);

    const waiting = new NativeOutputQueue();
    const pending = waiting[Symbol.asyncIterator]().next();
    waiting.fail(error);
    await expect(pending).rejects.toBe(error);
  });

  it("同一音频按不同块大小到达时具有相同的时长预算", async () => {
    for (const framesPerChunk of [24, 240, 2400]) {
      const queue = new NativeOutputQueue({ maxBufferedMs: 1000 });
      for (let frames = 0; frames < 24_000; frames += framesPerChunk) {
        queue.push({ ...chunk(frames), data: new ArrayBuffer(framesPerChunk * 2) });
      }
      expect(queue.bufferedMs).toBe(1000);
      expect(() => queue.push(chunk(24_000))).toThrow(/缓冲.*上限/);
      await queue[Symbol.asyncIterator]().next();
      expect(queue.bufferedMs).toBeCloseTo(1000 - framesPerChunk / 24);
      queue.push({ ...chunk(24_000), data: new ArrayBuffer(framesPerChunk * 2) });
      expect(queue.bufferedMs).toBe(1000);
    }
  });

  it("校验等待中的读者收到的音频，失败保留首因并丢弃待播数据", async () => {
    const queue = new NativeOutputQueue({ maxBufferedMs: 1000 });
    const pending = queue[Symbol.asyncIterator]().next();
    expect(() => queue.push({ ...chunk(1), data: new ArrayBuffer(48_002) })).toThrow(/缓冲.*上限/);
    expect(() => queue.push({ ...chunk(1), data: new ArrayBuffer(3) })).toThrow(/16-bit/);
    const error = new Error("first failure");
    queue.fail(error);
    queue.fail(new Error("later failure"));
    await expect(pending).rejects.toBe(error);
    await expect(queue[Symbol.asyncIterator]().next()).rejects.toBe(error);
  });

  it("停止迭代释放未播数据，正常close仍逐块排空", async () => {
    const queue = new NativeOutputQueue();
    queue.push(chunk(1));
    queue.push(chunk(2));
    queue.close();
    await queue[Symbol.asyncIterator]().return?.();
    expect(queue.size).toBe(0);
    expect(queue.bufferedMs).toBe(0);
    expect(await queue[Symbol.asyncIterator]().next()).toMatchObject({ done: true });
  });

  it.each([
    { sampleRate: 0 }, { sampleRate: 1.5 }, { maxBufferedMs: 0 },
    { maxBufferedMs: Infinity }, { maxBufferedMs: 0.001 },
  ])("拒绝无效缓冲预算 %o", (options) => {
    expect(() => new NativeOutputQueue(options)).toThrow();
  });
});
