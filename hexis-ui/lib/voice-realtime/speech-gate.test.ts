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

  it("有效输入不因低于打断门槛而丢弃，并按样本量累计300ms结束静音", () => {
    const gate = new SpeechGate();
    gate.consume(frame(0, 0.9));
    gate.consume(frame(1, 0.9));
    const started = gate.consume(frame(2, 0.4));
    expect(started[0]?.type).toBe("input_start");

    const belowInterruptThreshold = gate.consume(frame(3, 0.77));
    expect(belowInterruptThreshold.map((item) => item.type)).toEqual(["input_audio"]);

    for (let index = 4; index < 13; index += 1) {
      expect(gate.consume(frame(index, 0.4)).some((item) => item.type === "input_end")).toBe(false);
    }
    const ended = gate.consume(frame(13, 0.4));
    expect(ended.at(-1)).toMatchObject({ type: "input_end", endSample: 14 * 512 });
  });

  it("0.65的人声概率仍按现有2/3帧规则启动输入", () => {
    const gate = new SpeechGate();
    expect(gate.consume(frame(0, 0.65))).toEqual([]);
    expect(gate.consume(frame(1, 0.65))).toEqual([]);
    expect(gate.consume(frame(2, 0.65)).map((event) => event.type)).toEqual([
      "input_start", "input_audio", "input_audio",
    ]);
  });

  it.each([0.45, 0.65, 0.749])("录入后概率为%f的帧累计静音，第10个完整帧结束输入", (probability) => {
    const gate = new SpeechGate();
    for (let index = 0; index < 3; index += 1) gate.consume(frame(index, 0.9));

    for (let index = 3; index < 12; index += 1) {
      expect(gate.consume(frame(index, probability)).map((event) => event.type)).toEqual(["input_audio"]);
    }
    const ended = gate.consume(frame(12, probability));
    expect(ended.map((event) => event.type)).toEqual(["input_audio", "input_end"]);
    expect(ended.at(-1)).toMatchObject({ startSample: 0, endSample: 13 * 512, endMonotonicMs: 416 });
  });

  it.each([0.75, 0.9])("录入后概率为%f的帧清零静音累计", (probability) => {
    const gate = new SpeechGate();
    for (let index = 0; index < 3; index += 1) gate.consume(frame(index, 0.9));
    for (let index = 3; index < 12; index += 1) gate.consume(frame(index, 0.65));

    expect(gate.consume(frame(12, probability)).map((event) => event.type)).toEqual(["input_audio"]);
    for (let index = 13; index < 22; index += 1) {
      expect(gate.consume(frame(index, 0.65)).some((event) => event.type === "input_end")).toBe(false);
    }
    expect(gate.consume(frame(22, 0.65)).at(-1)).toMatchObject({ type: "input_end", endSample: 23 * 512 });
  });

  it("候选确认时累计低于结束阈值的尾帧，并在较高概率帧处停止回溯", () => {
    const gate = new SpeechGate();
    gate.consume(frame(0, 0.9));
    gate.consume(frame(1, 0.65));
    expect(gate.consume(frame(2, 0.4))[0]?.type).toBe("input_start");

    for (let index = 3; index < 10; index += 1) {
      expect(gate.consume(frame(index, 0.1)).some((event) => event.type === "input_end")).toBe(false);
    }
    expect(gate.consume(frame(10, 0.1)).at(-1)).toMatchObject({ type: "input_end", endSample: 11 * 512 });
  });

  it("候选尾部全部低于结束阈值时，只累计候选样本而不计入pre-roll", () => {
    const gate = new SpeechGate();
    for (let index = 0; index < 10; index += 1) gate.consume(frame(index, 0.1));
    gate.consume(frame(10, 0.65));
    gate.consume(frame(11, 0.65));
    const started = gate.consume(frame(12, 0.65));
    expect(started[0]).toMatchObject({ type: "input_start", frame: { startSample: 1280 } });
    expect(started.some((event) => event.type === "input_end")).toBe(false);

    for (let index = 13; index < 19; index += 1) {
      expect(gate.consume(frame(index, 0.1)).some((event) => event.type === "input_end")).toBe(false);
    }
    expect(gate.consume(frame(19, 0.1)).at(-1)).toMatchObject({ type: "input_end", endSample: 20 * 512 });
  });

  it("候选确认时已累计超过300ms尾部静音，应当帧结束且不被后续人声清零", () => {
    const gate = new SpeechGate();
    for (let index = 0; index < 10; index += 1) {
      expect(gate.consume(frame(index, index % 3 === 0 ? 0.65 : 0.4))).toEqual([]);
    }

    const confirmed = gate.consume(frame(10, 0.65));
    expect(confirmed[0]?.type).toBe("input_start");
    expect(confirmed.filter((event) => "frame" in event)).toHaveLength(11);
    expect(confirmed.filter((event) => event.type === "input_end")).toEqual([{
      type: "input_end", startSample: 0, endSample: 11 * 512, endMonotonicMs: 352,
    }]);
    expect(gate.consume(frame(11, 0.9))).toEqual([]);
    expect(gate.consume(frame(12, 0.9))).toEqual([]);
    expect(gate.consume(frame(13, 0.9))[0]?.type).toBe("input_start");
  });

  it("累计4799个静音样本不结束，达到4800个样本时结束", () => {
    const gate = new SpeechGate();
    for (let index = 0; index < 3; index += 1) gate.consume(frame(index, 0.9));
    for (let index = 3; index < 12; index += 1) {
      expect(gate.consume(frame(index, 0.65)).some((event) => event.type === "input_end")).toBe(false);
    }
    const beforeBoundary = frame(12, 0.65, 12, 191);
    expect(gate.consume(beforeBoundary).some((event) => event.type === "input_end")).toBe(false);
    const finalSample: SpeechFrame = {
      ...frame(13, 0.65, 13, 1),
      startSample: beforeBoundary.endSample,
      endSample: beforeBoundary.endSample + 1,
      startMonotonicMs: beforeBoundary.endMonotonicMs,
      endMonotonicMs: beforeBoundary.endMonotonicMs + 1 / 16,
    };
    expect(gate.consume(finalSample).at(-1)).toMatchObject({ type: "input_end", endSample: 3 * 512 + 4800 });
  });

  it("reset后不继承上一段的静音样本", () => {
    const gate = new SpeechGate();
    for (let index = 0; index < 3; index += 1) gate.consume(frame(index, 0.9));
    for (let index = 3; index < 12; index += 1) gate.consume(frame(index, 0.65));
    gate.reset();

    for (let index = 12; index < 14; index += 1) expect(gate.consume(frame(index, 0.9))).toEqual([]);
    expect(gate.consume(frame(14, 0.9))[0]?.type).toBe("input_start");
    for (let index = 15; index < 24; index += 1) {
      expect(gate.consume(frame(index, 0.65)).some((event) => event.type === "input_end")).toBe(false);
    }
    expect(gate.consume(frame(24, 0.65)).at(-1)).toMatchObject({ type: "input_end", endSample: 25 * 512 });
  });

  it("每段只产生一次input_end，结束后仍可启动下一段", () => {
    const gate = new SpeechGate();
    const events: string[] = [];
    for (let index = 0; index < 36; index += 1) {
      const voiced = index < 3 || (index >= 23 && index < 26);
      const trailing = (index >= 3 && index < 13) || index >= 26;
      const probability = voiced ? 0.9 : trailing ? 0.65 : 0.1;
      events.push(...gate.consume(frame(index, probability)).map((event) => event.type));
      if (index === 22) expect(events.filter((type) => type === "input_end")).toHaveLength(1);
    }
    expect(events.filter((type) => type === "input_start")).toHaveLength(2);
    expect(events.filter((type) => type === "input_end")).toHaveLength(2);
    for (let index = 36; index < 46; index += 1) {
      expect(gate.consume(frame(index, 0.1)).some((event) => event.type === "input_end")).toBe(false);
    }
  });
});
