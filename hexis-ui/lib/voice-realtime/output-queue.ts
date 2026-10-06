import type { NativePcmChunk } from "@/lib/voice-playback/native-output-source";

// 接收预算与播放器的一秒调度窗口独立：24kHz mono PCM16 最多缓存5.76MB。
// 120秒是本地内存策略，超过预算明确恢复，不改变模型语速或无限排队。
export const DEFAULT_OUTPUT_BUFFER_MS = 120_000;
export const REALTIME_OUTPUT_SAMPLE_RATE = 24_000;

export class NativeOutputQueue implements AsyncIterable<NativePcmChunk> {
  private readonly values: NativePcmChunk[] = [];
  private readonly readers: Array<{
    resolve: (result: IteratorResult<NativePcmChunk>) => void;
    reject: (error: Error) => void;
  }> = [];
  private ended = false;
  private failure: Error | null = null;
  private bufferedBytes = 0;
  private readonly sampleRate: number;
  private readonly maxBufferedBytes: number;

  constructor({
    sampleRate = REALTIME_OUTPUT_SAMPLE_RATE,
    maxBufferedMs = DEFAULT_OUTPUT_BUFFER_MS,
  }: { sampleRate?: number; maxBufferedMs?: number } = {}) {
    if (!Number.isInteger(sampleRate) || sampleRate < 1 || !Number.isFinite(maxBufferedMs) || maxBufferedMs <= 0) {
      throw new Error("PCM采样率与缓冲时长必须为正数，采样率必须为整数。");
    }
    this.sampleRate = sampleRate;
    this.maxBufferedBytes = Math.floor(sampleRate * maxBufferedMs / 1000) * 2;
    if (!Number.isSafeInteger(this.maxBufferedBytes) || this.maxBufferedBytes < 2) {
      throw new Error("PCM缓冲预算必须至少容纳一个16-bit帧。");
    }
  }

  get size(): number {
    return this.values.length;
  }

  get bufferedMs(): number {
    return this.bufferedBytes / (this.sampleRate * 2) * 1000;
  }

  push(value: NativePcmChunk): void {
    if (this.failure) throw this.failure;
    if (this.ended) throw new Error("Native output queue is closed.");
    const bytes = value.data.byteLength;
    if (bytes === 0 || bytes % 2 !== 0) throw new Error("PCM chunk必须是非空16-bit mono帧；请重试 Talk。");
    if (this.bufferedBytes + bytes > this.maxBufferedBytes) {
      const seconds = this.maxBufferedBytes / (this.sampleRate * 2);
      throw new Error(`音频播放缓冲已达到${seconds}秒上限；请打断当前回答或重试 Talk。`);
    }
    const reader = this.readers.shift();
    if (reader) {
      reader.resolve({ value, done: false });
      return;
    }
    this.values.push(value);
    this.bufferedBytes += bytes;
  }

  close(): void {
    if (this.ended) return;
    this.ended = true;
    for (const reader of this.readers.splice(0)) {
      reader.resolve({ value: undefined, done: true });
    }
  }

  fail(error: Error): void {
    if (this.failure) return;
    this.failure = error;
    this.ended = true;
    this.values.length = 0;
    this.bufferedBytes = 0;
    for (const reader of this.readers.splice(0)) reader.reject(error);
  }

  [Symbol.asyncIterator](): AsyncIterator<NativePcmChunk> {
    return {
      next: () => {
        if (this.failure) return Promise.reject(this.failure);
        if (this.values.length > 0) {
          const value = this.values.shift()!;
          this.bufferedBytes -= value.data.byteLength;
          return Promise.resolve({ value, done: false });
        }
        if (this.ended) return Promise.resolve({ value: undefined, done: true });
        return new Promise((resolve, reject) => {
          this.readers.push({ resolve, reject });
        });
      },
      return: () => {
        this.values.length = 0;
        this.bufferedBytes = 0;
        this.close();
        return Promise.resolve({ value: undefined, done: true });
      },
    };
  }
}
