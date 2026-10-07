import { readFileSync } from "node:fs";
import path from "node:path";

describe("speech detection configuration", () => {
  const config = JSON.parse(
    readFileSync(path.join(process.cwd(), "lib/voice-realtime/speech-detection-config.json"), "utf8"),
  );

  it("keeps the sampling and model tensor contract explicit", () => {
    expect(config.audio).toMatchObject({
      sample_rate: 16000,
      channels: 1,
      sample_format: "s16le",
      model_frame_samples: 512,
      model_context_samples: 64,
      model_state_shape: [2, 1, 128],
    });
  });

  it("keeps initial thresholds labeled as unvalidated", () => {
    expect(config.validation_status).toBe("unvalidated");
    expect(config.capture).toMatchObject({ pre_roll_ms: 240, silence_threshold_ms: 520, candidate_buffer_limit_ms: 1000 });
    expect(config.input_gate).toMatchObject({ probability_threshold: 0.6, window_frames: 3, minimum_positive_frames: 2 });
    expect(config.barge_in_gate.normal).toMatchObject({ probability_threshold: 0.8, window_frames: 6, minimum_positive_frames: 5 });
    expect(config.barge_in_gate.fast).toMatchObject({ probability_threshold: 0.95, window_frames: 4, minimum_positive_frames: 3 });
  });
});
