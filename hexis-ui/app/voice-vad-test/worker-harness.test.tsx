import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SpeechDetector, type DetectorWorkerCommand, type DetectorWorkerPort, type ReleasedPcmPrefix, type SpeechDetectorMessage } from "@/lib/voice-realtime/speech-detector";
import WorkerHarness, { decodeLocalWav } from "./worker-harness";

function detectorStub() {
  let ready = false;
  let generation = 0;
  const detector = {
    start: vi.fn(() => { ready = true; }),
    noteServerReady: vi.fn(() => true),
    appendPcm: vi.fn<SpeechDetector["appendPcm"]>(() => [{ frameId: 0, generation: 0, startSample: 0, endSample: 512, startMonotonicMs: 0, endMonotonicMs: 32, samples: new Int16Array(512) }]),
    reset: vi.fn(async (nextGeneration: number) => { generation = nextGeneration; }),
    close: vi.fn(),
    get canOpenCapture() { return ready; },
    get failure(): SpeechDetector["failure"] { return null; },
    get generation() { return generation; },
    get pendingSampleCount() { return 0; },
  };
  return { detector };
}

describe("voice VAD Worker harness", () => {
  it("decodes selected WAV bytes at the configured rate and converts actual samples to PCM16", async () => {
    const bytes = new ArrayBuffer(12);
    new Uint8Array(bytes).set([82, 73, 70, 70, 0, 0, 0, 0, 87, 65, 86, 69]);
    const decodeAudioData = vi.fn(async () => ({
      sampleRate: 16000, length: 3, numberOfChannels: 2,
      getChannelData: (channel: number) => channel === 0
        ? new Float32Array([1, -1, 0.5]) : new Float32Array([0, -1, 0.5]),
    }));
    const offlineContext = vi.fn(function () { return { decodeAudioData }; });
    vi.stubGlobal("OfflineAudioContext", offlineContext);
    try {
      const file = new File([bytes], "selected.wav", { type: "audio/wav" });
      Object.defineProperty(file, "arrayBuffer", { value: async () => bytes });
      const decoded = await decodeLocalWav(file);
      expect(offlineContext).toHaveBeenCalledWith(1, 1, 16000);
      expect(decodeAudioData).toHaveBeenCalledOnce();
      expect(decoded.samples).toEqual(new Int16Array([16384, -32768, 16384]));
      expect(decoded.channels).toBe(2);
      const invalid = new File(["not wav"], "invalid.wav");
      Object.defineProperty(invalid, "arrayBuffer", { value: async () => new ArrayBuffer(4) });
      await expect(decodeLocalWav(invalid)).rejects.toThrow(/WAV/);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("runs selected PCM sequentially and displays actual probabilities with explicit final padding", async () => {
    const stub = detectorStub();
    let release: (prefix: ReleasedPcmPrefix) => void = () => undefined;
    const samples = new Int16Array(513).fill(1234);
    const decoder = vi.fn(async () => ({ samples, sampleRate: 16000, channels: 1 }));
    stub.detector.appendPcm.mockImplementation((pcm, start, startMs, endMs) => {
      queueMicrotask(() => release({
        samples: pcm, generation: stub.detector.generation, startSample: start, endSample: start + pcm.length,
        startMonotonicMs: startMs, endMonotonicMs: endMs,
        probabilities: [start === 0 ? 0.125 : 0.875],
      }));
      return [];
    });
    render(<WorkerHarness detectorFactory={(callback) => { release = callback; return stub.detector; }} wavDecoder={decoder} />);
    fireEvent.click(screen.getByRole("button", { name: /start worker/i }));
    const file = new File(["selected"], "speech.wav", { type: "audio/wav" });
    fireEvent.change(screen.getByLabelText("Local WAV file"), { target: { files: [file] } });
    fireEvent.click(screen.getByRole("button", { name: "Run selected WAV" }));
    await waitFor(() => expect(screen.getByTestId("wav-run-status")).toHaveTextContent("completed"));
    expect(decoder).toHaveBeenCalledWith(file);
    expect(stub.detector.reset).toHaveBeenCalledOnce();
    expect(stub.detector.appendPcm).toHaveBeenCalledTimes(2);
    expect(stub.detector.appendPcm.mock.calls[0][0]).toEqual(samples.slice(0, 512));
    expect(stub.detector.appendPcm.mock.calls[1][0][0]).toBe(1234);
    expect(stub.detector.appendPcm.mock.calls[1][0].slice(1)).toEqual(new Int16Array(511));
    expect(stub.detector.appendPcm.mock.calls.map((call) => call[1])).toEqual([0, 512]);
    const result = JSON.parse(screen.getByTestId("wav-probability-trajectory").textContent!);
    expect(result).toMatchObject({ file_name: "speech.wav", sample_rate: 16000, real_samples: 513, padding_samples: 511 });
    expect(result.frames.map((frame: { probability: number }) => frame.probability)).toEqual([0.125, 0.875]);
    expect(result.frames[1]).toMatchObject({ start_sample: 512, real_samples: 1, padding_samples: 511 });
  });

  it("shows decode failure and does not submit any PCM", async () => {
    const stub = detectorStub();
    render(<WorkerHarness detectorFactory={() => stub.detector} wavDecoder={async () => { throw new Error("invalid WAV bytes"); }} />);
    fireEvent.click(screen.getByRole("button", { name: /start worker/i }));
    fireEvent.change(screen.getByLabelText("Local WAV file"), { target: { files: [new File(["invalid"], "invalid.wav")] } });
    fireEvent.click(screen.getByRole("button", { name: "Run selected WAV" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("invalid WAV bytes"));
    expect(stub.detector.appendPcm).not.toHaveBeenCalled();
    expect(screen.getByTestId("wav-run-status")).toHaveTextContent("failed");
    expect(screen.getByRole("button", { name: "Run selected WAV" })).toBeEnabled();
  });

  it("integrates reset acknowledgements and real watermark release without queueing the whole WAV", async () => {
    const commands: DetectorWorkerCommand[] = [];
    const post = (message: SpeechDetectorMessage) => queueMicrotask(() => worker.onmessage?.({ data: message } as MessageEvent<SpeechDetectorMessage>));
    const worker: DetectorWorkerPort = {
      onmessage: null, onerror: null, terminate: vi.fn(),
      postMessage: (command) => {
        commands.push(command);
        if (command.type === "initialize") post({ ...command, type: "ready", model: "v6" });
        if (command.type === "reset") post({ ...command, type: "reset_ack" });
        if (command.type === "frame") post({
          type: "frame_result", captureInstanceId: command.captureInstanceId, generation: command.generation,
          frameId: command.frameId, startSample: command.startSample, endSample: command.endSample,
          probability: command.startSample === 0 ? 0.2 : 0.9,
        });
      },
    };
    render(<WorkerHarness detectorFactory={(onReleasedPcm) => new SpeechDetector({
      sessionId: "worker-harness-session", mediaSessionId: "worker-harness-media", captureInstanceId: "worker-harness-capture",
      workerFactory: () => worker, onReleasedPcm,
    })} wavDecoder={async () => ({ samples: new Int16Array(4097).fill(2345), sampleRate: 16000, channels: 1 })} />);
    fireEvent.click(screen.getByRole("button", { name: /start worker/i }));
    await waitFor(() => expect(screen.getByText("Capture gate: open")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Local WAV file"), { target: { files: [new File(["speech"], "speech.wav")] } });
    fireEvent.click(screen.getByRole("button", { name: "Run selected WAV" }));
    await waitFor(() => expect(screen.getByTestId("wav-run-status")).toHaveTextContent("completed"));
    const frames = commands.filter((command) => command.type === "frame");
    expect(frames).toHaveLength(9);
    expect(frames.every((frame) => frame.generation === 1)).toBe(true);
    expect(screen.getByText("Pending raw samples: 0")).toBeTruthy();
    const result = JSON.parse(screen.getByTestId("wav-probability-trajectory").textContent!);
    expect(result.frames[0].probability).toBe(0.2);
    expect(result.frames[8]).toMatchObject({ probability: 0.9, start_sample: 4096, real_samples: 1, padding_samples: 511 });
  });

  it("cancels a pending inference on close and submits no later frames", async () => {
    const stub = detectorStub();
    render(<WorkerHarness detectorFactory={() => stub.detector}
      wavDecoder={async () => ({ samples: new Int16Array(1024).fill(1), sampleRate: 16000, channels: 1 })} />);
    fireEvent.click(screen.getByRole("button", { name: /start worker/i }));
    fireEvent.change(screen.getByLabelText("Local WAV file"), { target: { files: [new File(["speech"], "speech.wav")] } });
    fireEvent.click(screen.getByRole("button", { name: "Run selected WAV" }));
    await waitFor(() => expect(stub.detector.appendPcm).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("button", { name: /close worker/i }));
    await waitFor(() => expect(screen.getByTestId("wav-run-status")).toHaveTextContent("cancelled"));
    expect(stub.detector.appendPcm).toHaveBeenCalledOnce();
    expect(stub.detector.close).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: /start worker/i })).toBeEnabled();
  });

  it("keeps Worker and server readiness labeled as separate simulated facts", async () => {
    const stub = detectorStub();
    render(<WorkerHarness detectorFactory={() => stub.detector} />);
    expect(screen.getByText(/server readiness is simulated/i)).toBeTruthy();
    expect(screen.getByText(/capture gate: closed/i)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /start worker/i }));
    await waitFor(() => expect(screen.getByText(/capture gate: open/i)).toBeTruthy());
    expect(stub.detector.start).toHaveBeenCalledOnce();
    expect(stub.detector.noteServerReady).toHaveBeenCalledOnce();
  });

  it("sends only a deterministic local frame and can reset or close the Worker", async () => {
    const stub = detectorStub();
    render(<WorkerHarness detectorFactory={() => stub.detector} />);
    fireEvent.click(screen.getByRole("button", { name: /start worker/i }));
    await waitFor(() => expect(screen.getByText(/capture gate: open/i)).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /run zero-frame inference/i }));
    expect(stub.detector.appendPcm).toHaveBeenCalledOnce();
    expect(stub.detector.appendPcm.mock.calls[0][0]).toEqual(new Int16Array(512));

    fireEvent.click(screen.getByRole("button", { name: /reset worker/i }));
    await waitFor(() => expect(stub.detector.reset).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("button", { name: /close worker/i }));
    expect(stub.detector.close).toHaveBeenCalledOnce();
  });

  it("projects asynchronous readiness, backlog, and failure into rendered state", async () => {
    vi.useFakeTimers();
    const stub = detectorStub();
    const ready = vi.spyOn(stub.detector, "canOpenCapture", "get").mockReturnValue(false);
    const generation = vi.spyOn(stub.detector, "generation", "get");
    const pending = vi.spyOn(stub.detector, "pendingSampleCount", "get");
    const failure = vi.spyOn(stub.detector, "failure", "get");
    const view = render(<WorkerHarness detectorFactory={() => stub.detector} />);
    try {
      fireEvent.click(screen.getByRole("button", { name: /start worker/i }));
      expect(screen.getByText(/capture gate: closed/i)).toBeTruthy();
      ready.mockReturnValue(true);
      generation.mockReturnValue(2);
      pending.mockReturnValue(512);
      await act(() => vi.advanceTimersByTimeAsync(100));
      expect(screen.getByText(/capture gate: open/i)).toBeTruthy();
      expect(screen.getByText("Generation: 2")).toBeTruthy();
      expect(screen.getByText("Pending raw samples: 512")).toBeTruthy();

      ready.mockReturnValue(false);
      failure.mockReturnValue({ code: "inference_failed", message: "controlled failure" });
      await act(() => vi.advanceTimersByTimeAsync(100));
      expect(screen.getByText(/capture gate: error/i)).toBeTruthy();
      expect(screen.getByRole("alert").textContent).toContain("controlled failure");
      expect(screen.getByRole("button", { name: /run zero-frame inference/i })).toBeDisabled();
      fireEvent.click(screen.getByRole("button", { name: /close worker/i }));
      expect(screen.getByText("Generation: none")).toBeTruthy();
      expect(screen.getByText(/capture gate: closed/i)).toBeTruthy();
      expect(screen.getByRole("button", { name: /start worker/i })).not.toBeDisabled();
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });
});
