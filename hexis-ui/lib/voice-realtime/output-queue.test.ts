import { describe, expect, it } from "vitest";
import { NativeOutputQueue } from "./output-queue";

const chunk = (chunk_seq: number) => ({
  data: new Uint8Array([chunk_seq & 0xff, 0]).buffer,
  segment_id: "segment-1",
  chunk_seq,
});

describe("NativeOutputQueue", () => {
  it("delivers arriving PCM immediately and closes after queued frames drain", async () => {
    const queue = new NativeOutputQueue(2);
    const iterator = queue[Symbol.asyncIterator]();
    const pending = iterator.next();
    queue.push(chunk(1));
    expect(await pending).toMatchObject({ value: { chunk_seq: 1 }, done: false });
    queue.push(chunk(2));
    queue.close();
    expect(await iterator.next()).toMatchObject({ value: { chunk_seq: 2 }, done: false });
    expect(await iterator.next()).toMatchObject({ done: true });
  });

  it("fails loudly when full or failed while a reader waits", async () => {
    const queue = new NativeOutputQueue(1);
    queue.push(chunk(1));
    expect(() => queue.push(chunk(2))).toThrow(/full/);
    const error = new Error("transport failed");
    queue.fail(error);
    const iterator = queue[Symbol.asyncIterator]();
    expect(await iterator.next()).toMatchObject({ value: { chunk_seq: 1 }, done: false });
    await expect(iterator.next()).rejects.toBe(error);

    const waiting = new NativeOutputQueue(1);
    const pending = waiting[Symbol.asyncIterator]().next();
    waiting.fail(error);
    await expect(pending).rejects.toBe(error);
  });
});
