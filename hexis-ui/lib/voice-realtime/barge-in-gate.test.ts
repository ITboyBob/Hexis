import { BargeInGate } from "./barge-in-gate";

const firstTarget = {
  response_id: "response-a",
  playback_id: "playback-a",
  output_epoch: 1,
  captureGeneration: 0,
};

describe("BargeInGate", () => {
  it("普通门槛要求最近6帧5帧达标且当前帧达标", () => {
    const gate = new BargeInGate();
    gate.setTarget(firstTarget);
    const probabilities = [0.9, 0.85, 0.84, 0.9, 0.2, 0.81];
    let confirmed = null;
    for (const probability of probabilities) confirmed = gate.observe(probability, 0, firstTarget) ?? confirmed;
    expect(confirmed).toEqual(firstTarget);
    expect(gate.observe(0.9, 0, firstTarget)).toBeNull();
  });

  it("高置信快速门槛要求最近4帧3帧达标且当前帧达标", () => {
    const gate = new BargeInGate();
    gate.setTarget(firstTarget);
    expect(gate.observe(0.96, 0, firstTarget)).toBeNull();
    expect(gate.observe(0.98, 0, firstTarget)).toBeNull();
    expect(gate.observe(0.4, 0, firstTarget)).toBeNull();
    expect(gate.observe(0.97, 0, firstTarget)).toEqual(firstTarget);
  });

  it("目标或采集代次切换会清空证据窗，旧结果不能确认新目标", () => {
    const gate = new BargeInGate();
    gate.setTarget(firstTarget);
    for (let index = 0; index < 4; index += 1) expect(gate.observe(0.9, 0, firstTarget)).toBeNull();
    const secondTarget = {
      response_id: "response-b",
      playback_id: "playback-b",
      output_epoch: 2,
      captureGeneration: 0,
    };
    gate.setTarget(secondTarget);
    for (let index = 0; index < 4; index += 1) expect(gate.observe(0.99, 0, firstTarget)).toBeNull();
    expect(gate.observe(0.9, 0, secondTarget)).toBeNull();
    expect(gate.observe(0.9, 0, secondTarget)).toBeNull();
    expect(gate.observe(0.9, 0, secondTarget)).toBeNull();
    expect(gate.observe(0.9, 0, secondTarget)).toBeNull();
    expect(gate.observe(0.9, 0, secondTarget)).toBeNull();
    expect(gate.observe(0.9, 0, secondTarget)).toEqual(secondTarget);

    gate.setTarget({ ...secondTarget, captureGeneration: 1 });
    for (let index = 0; index < 5; index += 1) expect(gate.observe(0.9, 1, { ...secondTarget, captureGeneration: 1 })).toBeNull();
    expect(gate.observe(0.9, 1, { ...secondTarget, captureGeneration: 1 })).toEqual({ ...secondTarget, captureGeneration: 1 });
  });
});
