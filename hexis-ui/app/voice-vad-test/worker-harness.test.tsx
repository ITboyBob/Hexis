import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { SpeechDetector } from "@/lib/voice-realtime/speech-detector";
import WorkerHarness from "./worker-harness";

function detectorStub() {
  let ready = false;
  const detector = {
    start: vi.fn(() => { ready = true; }),
    noteServerReady: vi.fn(() => true),
    appendPcm: vi.fn<SpeechDetector["appendPcm"]>(() => [{ frameId: 0, generation: 0, startSample: 0, endSample: 512, startMonotonicMs: 0, endMonotonicMs: 32, samples: new Int16Array(512) }]),
    reset: vi.fn(async () => undefined),
    close: vi.fn(),
    get canOpenCapture() { return ready; },
    get failure(): SpeechDetector["failure"] { return null; },
    get generation() { return 0; },
    get pendingSampleCount() { return 0; },
  };
  return { detector };
}

describe("voice VAD Worker harness", () => {
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
