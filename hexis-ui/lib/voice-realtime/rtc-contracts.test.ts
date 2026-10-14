import { describe, expect, it } from "vitest";
import { parseRtcDescriptor, parseRtcMessage } from "./rtc-contracts";

const config = { instructions: "真实上下文", modalities: ["text", "audio"], input_audio_transcription: { model: "qwen3-asr-flash-realtime" },
  turn_detection: { type: "semantic_vad" }, tools: [] };

describe("RTC 控制契约", () => {
  it("只接受开启独立转录的云端 VAD 配置", () => {
    const value = { schema_version: 2, transport: "webrtc", session_id: "chat", media_session_id: "media",
      control_url: "ws://127.0.0.1:43817/control", offer_url: "/api/offer", close_url: "/api/close",
      timeout_ms: 30000, context_version: "a".repeat(64), model: "selected", session_config: config };
    expect(parseRtcDescriptor(value).session_config).toEqual(config);
    expect(() => parseRtcDescriptor({ ...value, session_config: { ...config, input_audio_transcription: { model: "asr" } } })).toThrow();
    expect(() => parseRtcDescriptor({ ...value, control_url: "https://invalid" })).toThrow();
  });
  it("接纳用户文字与情绪事件，拒绝不合法标签", () => {
    const identity = { schema_version: 2, session_id: "chat", media_session_id: "media", producer_seq: 1,
      input_turn_id: "input", input_order: 0, turn_id: "turn" };
    expect(parseRtcMessage({ ...identity, type: "user_text", text: "你好" }).type).toBe("user_text");
    expect(parseRtcMessage({ ...identity, type: "emotion_result", label: "happy", wordset_version: "v1" }).type).toBe("emotion_result");
    expect(() => parseRtcMessage({ ...identity, type: "emotion_result", label: "invalid", wordset_version: "v1" })).toThrow();
  });
  it("拒绝旧 PCM 消息和无身份的控制消息", () => {
    const envelope = { schema_version: 2, session_id: "chat", media_session_id: "media", producer_seq: 0 };
    expect(parseRtcMessage({ ...envelope, type: "ready", cloud_session_id: "cloud" }).type).toBe("ready");
    expect(() => parseRtcMessage({ ...envelope, type: "audio_chunk", pcm_base64: "AAAA" })).toThrow();
    expect(() => parseRtcMessage({ type: "ready" })).toThrow();
  });
});
