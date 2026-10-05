export type NativePcmChunk = {
  data: ArrayBuffer;
  segment_id: string;
  chunk_seq: number;
};

export type NativePcmSource = {
  sample_rate: number;
  channels: number;
  chunks: AsyncIterable<NativePcmChunk>;
};

export function isNativePcmSource(value: unknown): value is NativePcmSource {
  return (
    typeof value === "object" &&
    value !== null &&
    "sample_rate" in value &&
    "channels" in value &&
    "chunks" in value &&
    typeof (value as NativePcmSource).sample_rate === "number" &&
    typeof (value as NativePcmSource).channels === "number" &&
    typeof (value as NativePcmSource).chunks === "object"
  );
}

export function decodeBase64ToPcm(value: string): ArrayBuffer {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes.buffer;
}
