"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  SpeechDetector,
  type ReleasedPcmPrefix,
  type SpeechDetectorOptions,
} from "@/lib/voice-realtime/speech-detector";
import config from "@/lib/voice-realtime/speech-detection-config.json";

type DetectorPort = Pick<SpeechDetector, "start" | "noteServerReady" | "appendPcm" | "reset" | "close">
  & Pick<SpeechDetector, "canOpenCapture" | "failure" | "generation" | "pendingSampleCount">;
type DetectorSnapshot = Pick<DetectorPort, "canOpenCapture" | "failure" | "generation" | "pendingSampleCount">;

export type WorkerHarnessProps = {
  detectorFactory?: (onReleasedPcm: (prefix: ReleasedPcmPrefix) => void) => DetectorPort;
  wavDecoder?: (file: File) => Promise<DecodedWav>;
};

type DecodedWav = { samples: Int16Array; sampleRate: number; channels: number };
type ProbabilityFrame = {
  start_sample: number; end_sample: number; start_ms: number; end_ms: number;
  real_samples: number; padding_samples: number; probability: number;
};
type WavRun = {
  file_name: string; file_bytes: number; configuration_id: string; generation: number;
  sample_rate: number; source_channels: number; real_samples: number; padding_samples: number;
  frames: ProbabilityFrame[];
};
type PendingInference = {
  generation: number; startSample: number;
  resolve: (prefix: ReleasedPcmPrefix) => void; reject: (error: Error) => void;
};

export async function decodeLocalWav(file: File): Promise<DecodedWav> {
  const bytes = await file.arrayBuffer();
  const header = new Uint8Array(bytes, 0, Math.min(bytes.byteLength, 12));
  if (header.length < 12 || String.fromCharCode(...header.slice(0, 4)) !== "RIFF"
    || String.fromCharCode(...header.slice(8, 12)) !== "WAVE") {
    throw new Error("Selected file is not a RIFF WAV. Choose a valid local WAV file.");
  }
  // Offline decoding resamples to the configured model rate without playback or a microphone.
  const context = new OfflineAudioContext(1, 1, config.audio.sample_rate);
  const decoded = await context.decodeAudioData(bytes);
  if (decoded.sampleRate !== config.audio.sample_rate || decoded.length === 0) {
    throw new Error("WAV decoding returned no audio at the configured model sample rate.");
  }
  const mono = new Float32Array(decoded.length);
  for (let channel = 0; channel < decoded.numberOfChannels; channel += 1) {
    const input = decoded.getChannelData(channel);
    for (let index = 0; index < mono.length; index += 1) mono[index] += input[index] / decoded.numberOfChannels;
  }
  const samples = Int16Array.from(mono, (value) => {
    const bounded = Math.max(-1, Math.min(1, value));
    return Math.round(bounded * (bounded < 0 ? 32768 : 32767));
  });
  return { samples, sampleRate: decoded.sampleRate, channels: decoded.numberOfChannels };
}

const identity: Omit<SpeechDetectorOptions, "workerFactory" | "onReleasedPcm"> = {
  sessionId: "worker-harness-session",
  mediaSessionId: "worker-harness-media",
  captureInstanceId: "worker-harness-capture",
};

export default function WorkerHarness({ detectorFactory, wavDecoder = decodeLocalWav }: WorkerHarnessProps) {
  const detectorRef = useRef<DetectorPort | null>(null);
  const pendingInferenceRef = useRef<PendingInference | null>(null);
  const runTokenRef = useRef(0);
  const [snapshot, setSnapshot] = useState<DetectorSnapshot | null>(null);
  const [frameCursor, setFrameCursor] = useState(0);
  const [lastResult, setLastResult] = useState<string>("No frame has run.");
  const [wavFile, setWavFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [wavStatus, setWavStatus] = useState("idle");
  const [wavError, setWavError] = useState<string | null>(null);
  const [wavRun, setWavRun] = useState<WavRun | null>(null);
  const hasDetector = snapshot !== null;
  const failure = snapshot?.failure ?? null;

  const sampleDetector = useCallback(() => {
    const selected = detectorRef.current;
    if (selected?.failure) pendingInferenceRef.current?.reject(new Error(selected.failure.message));
    setSnapshot(selected ? {
      canOpenCapture: selected.canOpenCapture,
      failure: selected.failure,
      generation: selected.generation,
      pendingSampleCount: selected.pendingSampleCount,
    } : null);
  }, []);

  useEffect(() => () => {
    runTokenRef.current += 1;
    pendingInferenceRef.current?.reject(new Error("Worker harness closed."));
    detectorRef.current?.close();
    detectorRef.current = null;
  }, []);

  useEffect(() => {
    if (!hasDetector) return;
    const timer = window.setInterval(sampleDetector, 100);
    return () => window.clearInterval(timer);
  }, [hasDetector, sampleDetector]);

  const start = () => {
    if (detectorRef.current) return;
    const onReleasedPcm = (prefix: ReleasedPcmPrefix) => {
      const pending = pendingInferenceRef.current;
      if (pending && pending.generation === prefix.generation && pending.startSample === prefix.startSample) {
        pending.resolve(prefix);
      }
      setLastResult(`Released ${prefix.samples.length} raw samples; probability ${prefix.probabilities.at(-1)?.toFixed(4) ?? "unknown"}.`);
      sampleDetector();
    };
    try {
      const detector = detectorFactory
        ? detectorFactory(onReleasedPcm)
        : new SpeechDetector({ ...identity, onReleasedPcm });
      detectorRef.current = detector;
      detector.start();
      const accepted = detector.noteServerReady({ ...identity, generation: detector.generation, bargeInSupported: true });
      if (!accepted) setLastResult("The simulated server-ready event was rejected by the selected detector identity.");
    } catch (error) {
      detectorRef.current?.close();
      detectorRef.current = null;
      setLastResult(`Worker startup failed: ${error instanceof Error ? error.message : String(error)}. Retry Start Worker.`);
    }
    sampleDetector();
  };

  const runZeroFrame = () => {
    const selected = detectorRef.current;
    if (!selected?.canOpenCapture) return;
    const startedAt = performance.now();
    const frameSamples = config.audio.model_frame_samples;
    selected.appendPcm(new Int16Array(frameSamples), frameCursor, startedAt, startedAt + frameSamples * 1000 / config.audio.sample_rate);
    sampleDetector();
    setFrameCursor((value) => value + frameSamples);
    setLastResult("Submitted one deterministic 512-sample silence frame; waiting for Worker inference.");
  };

  const resetWorker = async () => {
    const selected = detectorRef.current;
    if (!selected) return;
    setBusy(true);
    try {
      const nextGeneration = selected.generation + 1;
      await selected.reset(nextGeneration, `worker-harness-reset-${nextGeneration}`);
      if (detectorRef.current !== selected) return;
      selected.noteServerReady({ ...identity, generation: nextGeneration, bargeInSupported: true });
      setFrameCursor(0);
      setLastResult("Worker model state reset; simulated server resume acknowledged.");
    } catch (error) {
      if (detectorRef.current === selected) setLastResult(`Worker reset failed: ${error instanceof Error ? error.message : String(error)}. Close and restart Worker.`);
    } finally {
      if (detectorRef.current === selected) { setBusy(false); sampleDetector(); }
    }
  };

  const runWav = async () => {
    const selected = detectorRef.current;
    if (!wavFile || !selected?.canOpenCapture || busy) return;
    const token = ++runTokenRef.current;
    setBusy(true);
    setWavError(null);
    setWavRun(null);
    setWavStatus("decoding");
    try {
      const decoded = await wavDecoder(wavFile);
      if (token !== runTokenRef.current) return;
      if (decoded.sampleRate !== config.audio.sample_rate || decoded.samples.length === 0) {
        throw new Error("Decoded WAV must contain PCM at the configured model sample rate.");
      }
      setWavStatus("resetting");
      const nextGeneration = selected.generation + 1;
      await selected.reset(nextGeneration, `worker-harness-wav-${token}`);
      if (token !== runTokenRef.current) return;
      if (!selected.noteServerReady({ ...identity, generation: nextGeneration, bargeInSupported: true }) || !selected.canOpenCapture) {
        throw new Error("Worker did not reopen after WAV model reset.");
      }
      const frameSamples = config.audio.model_frame_samples;
      const run: WavRun = {
        file_name: wavFile.name, file_bytes: wavFile.size, configuration_id: config.configuration_id,
        generation: selected.generation, sample_rate: decoded.sampleRate, source_channels: decoded.channels,
        real_samples: decoded.samples.length, padding_samples: (frameSamples - decoded.samples.length % frameSamples) % frameSamples,
        frames: [],
      };
      setWavStatus("running");
      setWavRun(run);
      const timelineStart = performance.now();
      for (let startSample = 0; startSample < decoded.samples.length; startSample += frameSamples) {
        if (token !== runTokenRef.current) return;
        const realSamples = Math.min(frameSamples, decoded.samples.length - startSample);
        const pcm = new Int16Array(frameSamples);
        pcm.set(decoded.samples.subarray(startSample, startSample + realSamples));
        // One unresolved frame at a time preserves the production backlog budget.
        const prefix = await new Promise<ReleasedPcmPrefix>((resolve, reject) => {
          const timer = window.setTimeout(() => pending.reject(new Error("Worker inference timed out. Close and restart Worker.")), config.worker.initialization_timeout_ms);
          const cleanup = () => { window.clearTimeout(timer); pendingInferenceRef.current = null; };
          const pending: PendingInference = {
            generation: selected.generation, startSample,
            resolve: (value) => { cleanup(); resolve(value); },
            reject: (error) => { cleanup(); reject(error); },
          };
          pendingInferenceRef.current = pending;
          try {
            selected.appendPcm(pcm, startSample, timelineStart + startSample * 1000 / decoded.sampleRate,
              timelineStart + (startSample + frameSamples) * 1000 / decoded.sampleRate);
          } catch (error) { pending.reject(error instanceof Error ? error : new Error(String(error))); }
        });
        if (token !== runTokenRef.current) return;
        if (prefix.endSample !== startSample + frameSamples || prefix.probabilities.length !== 1) {
          throw new Error("Worker returned an unexpected frame range or probability count.");
        }
        run.frames.push({
          start_sample: startSample, end_sample: prefix.endSample,
          start_ms: startSample * 1000 / decoded.sampleRate, end_ms: prefix.endSample * 1000 / decoded.sampleRate,
          real_samples: realSamples, padding_samples: frameSamples - realSamples, probability: prefix.probabilities[0],
        });
        setWavRun({ ...run, frames: [...run.frames] });
      }
      setFrameCursor(decoded.samples.length + run.padding_samples);
      setWavStatus("completed");
      setLastResult(`WAV completed: ${run.frames.length} actual Worker probabilities; ${run.padding_samples} explicit final padding samples.`);
    } catch (error) {
      if (token === runTokenRef.current) {
        setWavStatus("failed");
        setWavError(`${error instanceof Error ? error.message : String(error)} Choose a valid WAV or close and restart Worker, then retry.`);
      }
    } finally {
      if (token === runTokenRef.current) { setBusy(false); sampleDetector(); }
    }
  };

  const closeWorker = () => {
    runTokenRef.current += 1;
    pendingInferenceRef.current?.reject(new Error("Worker closed during WAV inference."));
    detectorRef.current?.close();
    detectorRef.current = null;
    setFrameCursor(0);
    setBusy(false);
    if (busy) setWavStatus("cancelled");
    sampleDetector();
  };

  const gateStatus = failure ? "error" : snapshot?.canOpenCapture ? "open" : "closed";

  return (
    <main className="mx-auto flex max-w-3xl flex-col gap-5 p-6">
      <header>
        <h1 className="text-2xl font-semibold">Speech VAD Worker test</h1>
        <p className="mt-2 text-sm text-gray-600">
          This page runs a local Worker with deterministic silence frames or an explicitly selected local WAV. It does not open the microphone or connect to the API, database, or a provider.
        </p>
        <p className="mt-1 text-sm text-gray-600">Server readiness is simulated here; it does not verify the production WebSocket handshake.</p>
      </header>
      <section aria-label="Worker controls" className="flex flex-wrap gap-3">
        <button type="button" onClick={start} disabled={hasDetector}>
          Start Worker
        </button>
        <button type="button" onClick={runZeroFrame} disabled={!snapshot?.canOpenCapture || busy}>
          Run zero-frame inference
        </button>
        <button type="button" onClick={() => void resetWorker()} disabled={!snapshot?.canOpenCapture || busy}>
          Reset Worker
        </button>
        <button type="button" onClick={closeWorker} disabled={!hasDetector}>
          Close Worker
        </button>
      </section>
      <section aria-label="Local WAV inference" className="rounded border p-4">
        <label htmlFor="worker-local-wav">Local WAV file</label>
        <input id="worker-local-wav" type="file" accept=".wav,audio/wav,audio/x-wav" disabled={busy}
          onChange={(event) => { setWavFile(event.target.files?.[0] ?? null); setWavRun(null); setWavStatus("idle"); setWavError(null); }} />
        <p>Selected file: {wavFile?.name ?? "none"}. Decode to mono {config.audio.sample_rate} Hz PCM16; reset model before each run.</p>
        <button type="button" onClick={() => void runWav()} disabled={!wavFile || !snapshot?.canOpenCapture || busy}>Run selected WAV</button>
        <p data-testid="wav-run-status">WAV run: {wavStatus}; probabilities: {wavRun?.frames.length ?? 0}</p>
        {wavError ? <p role="alert">{wavError}</p> : null}
        <pre data-testid="wav-probability-trajectory" className="max-h-96 overflow-auto text-xs">{JSON.stringify(wavRun, null, 2)}</pre>
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
