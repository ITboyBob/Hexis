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

  it("独立结束阈值尚未完成校准，保留现有启动和打断阈值", () => {
    expect(config.validation_status).toBe("unvalidated");
    expect(config.configuration_id).toBe("voice-end-threshold-20261009-a");
    expect(config.calibration).toMatchObject({
      dataset_id: "d1-20261008-a",
      scope: "synthetic_tts_and_deterministic_noise",
      acoustic_validation: "not_claimed",
    });
    expect(config.capture).toMatchObject({
      pre_roll_ms: 240,
      silence_threshold_ms: 300,
      active_probability_threshold: 0.45,
      end_probability_threshold: 0.75,
      candidate_buffer_limit_ms: 1000,
    });
    expect(config.input_gate).toMatchObject({ probability_threshold: 0.6, window_frames: 3, minimum_positive_frames: 2 });
    expect(config.barge_in_gate.normal).toMatchObject({ probability_threshold: 0.8, window_frames: 6, minimum_positive_frames: 5 });
    expect(config.barge_in_gate.fast).toMatchObject({ probability_threshold: 0.95, window_frames: 4, minimum_positive_frames: 3 });
  });

  it("keeps the server-ready and Worker initialization budgets separate", () => {
    expect(config.configuration_id).toBe("voice-end-threshold-20261009-a");
    expect(config.readiness.server_ready_timeout_ms).toBe(30000);
    expect(config.worker.initialization_timeout_ms).toBe(30000);
  });
});
