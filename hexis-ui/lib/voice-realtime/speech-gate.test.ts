import { SpeechGate, type SpeechFrame } from "./speech-gate";

function frame(
  frameId: number,
  probability: number,
  value = frameId,
  sampleCount = 512,
): SpeechFrame {
  const startSample = frameId * 512;
  return {
    generation: 0,
    frameId,
    startSample,
    endSample: startSample + sampleCount,
    startMonotonicMs: frameId * 32,
    endMonotonicMs: frameId * 32 + (sampleCount / 16_000) * 1_000,
    samples: new Int16Array(sampleCount).fill(value),
    probability,
  };
}

describe("SpeechGate", () => {
  it("保留最近恰好3840个pre-roll样本并与candidate范围连续", () => {
    const gate = new SpeechGate();
    const quiet = Array.from({ length: 10 }, (_, index) => frame(index, 0.1));
    for (const input of quiet) expect(gate.consume(input)).toEqual([]);
    gate.consume(frame(10, 0.7));
    gate.consume(frame(11, 0.7));
    const events = gate.consume(frame(12, 0.7));
    const released = events.flatMap((event) => "frame" in event ? [event.frame] : []);
    const preRoll = released.filter((input) => input.endSample <= 5120);
    expect(preRoll.reduce((samples, input) => samples + input.samples.length, 0)).toBe(3840);
    expect(preRoll[0]).toMatchObject({ startSample: 1280, startMonotonicMs: 80 });
    expect(preRoll.at(-1)?.endSample).toBe(5120);
    const actualSamples = Int16Array.from(released.flatMap((input) => [...input.samples]));
    const expectedSamples = Int16Array.from([
      ...quiet.flatMap((input) => [...input.samples]).slice(-3840),
      ...frame(10, 0.7).samples, ...frame(11, 0.7).samples, ...frame(12, 0.7).samples,
    ]);
    expect(actualSamples).toEqual(expectedSamples);
    for (let index = 1; index < released.length; index += 1) {
      expect(released[index].startSample).toBe(released[index - 1].endSample);
      expect(released[index].startMonotonicMs).toBe(released[index - 1].endMonotonicMs);
    }
  });

  it("只在2/3帧确认输入后释放真实pre-roll与候选PCM", () => {
    const gate = new SpeechGate();
    expect(gate.consume(frame(0, 0.1, 10))).toEqual([]);
    expect(gate.consume(frame(1, 0.7, 11))).toEqual([]);
    expect(gate.consume(frame(2, 0.1, 12))).toEqual([]);

    const confirmed = gate.consume(frame(3, 0.65, 13));

    expect(confirmed.map((event) => event.type)).toEqual([
      "input_start", "input_audio", "input_audio", "input_audio",
    ]);
    expect(confirmed.flatMap((event) => "frame" in event ? [event.frame.samples[0]] : [])).toEqual([10, 11, 12, 13]);
    expect(confirmed[0]).toMatchObject({ type: "input_start", frame: { startSample: 0 } });
  });

  it("三帧噪音候选被拒绝且不产生正式输入", () => {
    const gate = new SpeechGate();
    gate.consume(frame(0, 0.5));
    gate.consume(frame(1, 0.4));
    const rejected = gate.consume(frame(2, 0.3));

    expect(rejected).toEqual([expect.objectContaining({
      type: "candidate_rejected",
      startSample: 0,
      endSample: 1536,
      reason: "probability",
    })]);
  });

  it("候选缓存超过1000ms时明确拒绝并限制缓存", () => {
    const gate = new SpeechGate();
    const observed: string[] = [];
    for (let index = 0; index < 32; index += 1) {
      observed.push(...gate.consume(frame(index, index % 3 === 0 ? 0.7 : 0.4)).map((item) => item.type));
    }

    expect(observed).toEqual(["candidate_rejected"]);
  });

  it("有效输入不因低于打断门槛而丢弃，并按样本量累计520ms结束静音", () => {
    const gate = new SpeechGate();
    gate.consume(frame(0, 0.7));
    gate.consume(frame(1, 0.7));
    const started = gate.consume(frame(2, 0.4));
    expect(started[0]?.type).toBe("input_start");

    const belowInterruptThreshold = gate.consume(frame(3, 0.5));
    expect(belowInterruptThreshold.map((item) => item.type)).toEqual(["input_audio"]);

    for (let index = 4; index < 20; index += 1) {
      expect(gate.consume(frame(index, 0.4)).some((item) => item.type === "input_end")).toBe(false);
    }
    const ended = gate.consume(frame(20, 0.4));
    expect(ended.at(-1)).toMatchObject({ type: "input_end", endSample: 21 * 512 });
  });
});
