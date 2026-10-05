import type { NativePcmChunk } from "@/lib/voice-playback/native-output-source";

export class NativeOutputQueue implements AsyncIterable<NativePcmChunk> {
  private readonly values: NativePcmChunk[] = [];
  private readonly readers: Array<{
    resolve: (result: IteratorResult<NativePcmChunk>) => void;
    reject: (error: Error) => void;
  }> = [];
  private ended = false;
  private failure: Error | null = null;

  constructor(readonly capacity = 16) {
    if (!Number.isInteger(capacity) || capacity < 1) {
      throw new Error("Native output queue capacity must be a positive integer.");
    }
  }

  get size(): number {
    return this.values.length;
  }

  push(value: NativePcmChunk): void {
    if (this.ended) throw new Error("Native output queue is closed.");
    const reader = this.readers.shift();
    if (reader) {
      reader.resolve({ value, done: false });
      return;
    }
    if (this.values.length >= this.capacity) {
      throw new Error(`Native output queue is full (capacity ${this.capacity}).`);
    }
    this.values.push(value);
  }

  close(): void {
    if (this.ended) return;
    this.ended = true;
    for (const reader of this.readers.splice(0)) {
      reader.resolve({ value: undefined, done: true });
    }
  }

  fail(error: Error): void {
    if (this.ended) return;
    this.failure = error;
    this.ended = true;
    for (const reader of this.readers.splice(0)) reader.reject(error);
  }

  [Symbol.asyncIterator](): AsyncIterator<NativePcmChunk> {
    return {
      next: () => {
        if (this.values.length > 0) {
          return Promise.resolve({ value: this.values.shift()!, done: false });
        }
        if (this.failure) return Promise.reject(this.failure);
        if (this.ended) return Promise.resolve({ value: undefined, done: true });
        return new Promise((resolve, reject) => {
          this.readers.push({ resolve, reject });
        });
      },
    };
  }
}
