"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  SpeechDetector,
  type ReleasedPcmPrefix,
  type SpeechDetectorOptions,
} from "@/lib/voice-realtime/speech-detector";

type DetectorPort = Pick<SpeechDetector, "start" | "noteServerReady" | "appendPcm" | "reset" | "close">
  & Pick<SpeechDetector, "canOpenCapture" | "failure" | "generation" | "pendingSampleCount">;
type DetectorSnapshot = Pick<DetectorPort, "canOpenCapture" | "failure" | "generation" | "pendingSampleCount">;

export type WorkerHarnessProps = {
  detectorFactory?: (onReleasedPcm: (prefix: ReleasedPcmPrefix) => void) => DetectorPort;
};

const identity: Omit<SpeechDetectorOptions, "workerFactory" | "onReleasedPcm"> = {
  sessionId: "worker-harness-session",
  mediaSessionId: "worker-harness-media",
  captureInstanceId: "worker-harness-capture",
};

export default function WorkerHarness({ detectorFactory }: WorkerHarnessProps) {
  const detectorRef = useRef<DetectorPort | null>(null);
  const [snapshot, setSnapshot] = useState<DetectorSnapshot | null>(null);
  const [frameCursor, setFrameCursor] = useState(0);
  const [lastResult, setLastResult] = useState<string>("No frame has run.");
  const hasDetector = snapshot !== null;
  const failure = snapshot?.failure ?? null;

  const sampleDetector = useCallback(() => {
    const selected = detectorRef.current;
    setSnapshot(selected ? {
      canOpenCapture: selected.canOpenCapture,
      failure: selected.failure,
      generation: selected.generation,
      pendingSampleCount: selected.pendingSampleCount,
    } : null);
  }, []);

  useEffect(() => {
    if (!hasDetector) return;
    const timer = window.setInterval(sampleDetector, 100);
    return () => window.clearInterval(timer);
  }, [hasDetector, sampleDetector]);

  const start = () => {
    if (detectorRef.current) return;
    const onReleasedPcm = (prefix: ReleasedPcmPrefix) => {
      setLastResult(`Released ${prefix.samples.length} raw samples; probability ${prefix.probabilities.at(-1)?.toFixed(4) ?? "unknown"}.`);
      sampleDetector();
    };
    const detector = detectorFactory
      ? detectorFactory(onReleasedPcm)
      : new SpeechDetector({ ...identity, onReleasedPcm });
    detectorRef.current = detector;
    detector.start();
    const accepted = detector.noteServerReady({ ...identity, generation: detector.generation, bargeInSupported: true });
    if (!accepted) setLastResult("The simulated server-ready event was rejected by the selected detector identity.");
    sampleDetector();
  };

  const runZeroFrame = () => {
    const selected = detectorRef.current;
    if (!selected?.canOpenCapture) return;
    const startedAt = performance.now();
    selected.appendPcm(new Int16Array(512), frameCursor, startedAt, startedAt + 32);
    sampleDetector();
    setFrameCursor((value) => value + 512);
    setLastResult("Submitted one deterministic 512-sample silence frame; waiting for Worker inference.");
  };

  const resetWorker = async () => {
    const selected = detectorRef.current;
    if (!selected) return;
    const nextGeneration = selected.generation + 1;
    await selected.reset(nextGeneration, `worker-harness-reset-${nextGeneration}`);
    selected.noteServerReady({ ...identity, generation: nextGeneration, bargeInSupported: true });
    setFrameCursor(0);
    setLastResult("Worker model state reset; simulated server resume acknowledged.");
    sampleDetector();
  };

  const closeWorker = () => {
    detectorRef.current?.close();
    detectorRef.current = null;
    setFrameCursor(0);
    sampleDetector();
  };

  const gateStatus = failure ? "error" : snapshot?.canOpenCapture ? "open" : "closed";

  return (
    <main className="mx-auto flex max-w-3xl flex-col gap-5 p-6">
      <header>
        <h1 className="text-2xl font-semibold">Speech VAD Worker test</h1>
        <p className="mt-2 text-sm text-gray-600">
          This page runs a local Worker with deterministic silence frames. It does not open the microphone or connect to the API, database, or a provider.
        </p>
        <p className="mt-1 text-sm text-gray-600">Server readiness is simulated here; it does not verify the production WebSocket handshake.</p>
      </header>
      <section aria-label="Worker controls" className="flex flex-wrap gap-3">
        <button type="button" onClick={start} disabled={hasDetector}>
          Start Worker
        </button>
        <button type="button" onClick={runZeroFrame} disabled={!snapshot?.canOpenCapture}>
          Run zero-frame inference
        </button>
        <button type="button" onClick={() => void resetWorker()} disabled={!snapshot?.canOpenCapture}>
          Reset Worker
        </button>
        <button type="button" onClick={closeWorker} disabled={!hasDetector}>
          Close Worker
        </button>
      </section>
      <section aria-label="Worker status" aria-live="polite" className="rounded border p-4">
        <p>Capture gate: {gateStatus}</p>
        <p>Generation: {snapshot?.generation ?? "none"}</p>
        <p>Pending raw samples: {snapshot?.pendingSampleCount ?? 0}</p>
        {failure ? <p role="alert">{failure.code}: {failure.message}</p> : null}
        <p data-testid="worker-result">{lastResult}</p>
      </section>
    </main>
  );
}
