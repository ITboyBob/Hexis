import { expect, test } from "@playwright/test";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

const referencePath = resolve(
  process.env.M4_REFERENCE_WAV
    || "/Users/itboybob/.cache/hexis/batch3-serial-voice-2026-10-05/cloud-output-2.wav",
);
const evidencePath = resolve(
  process.env.M4_DEVICE_EVIDENCE
    || "/Users/itboybob/.cache/hexis/m4-round1/devices-playwright.json",
);
const deviceCase = process.env.M4_DEVICE_CASE || "speaker";

type Wav = {
  sampleRate: number;
  channels: number;
  samples: Float32Array;
};

type CaptureResult = {
  status: "granted" | "denied" | "unavailable";
  reason?: string;
  settings?: unknown;
  sampleRate?: number;
  sampleCount?: number;
  peak?: number;
  rms?: number;
  samples?: number[];
};

function readWav(path: string): Wav {
  const buffer = readFileSync(path);
  if (buffer.toString("ascii", 0, 4) !== "RIFF" || buffer.toString("ascii", 8, 12) !== "WAVE") {
    throw new Error(`不是有效 WAV: ${path}`);
  }
  let offset = 12;
  let sampleRate = 0;
  let channels = 0;
  let bits = 0;
  let data: Buffer | null = null;
  while (offset + 8 <= buffer.length) {
    const id = buffer.toString("ascii", offset, offset + 4);
    const size = buffer.readUInt32LE(offset + 4);
    const start = offset + 8;
    if (id === "fmt ") {
      channels = buffer.readUInt16LE(start + 2);
      sampleRate = buffer.readUInt32LE(start + 4);
      bits = buffer.readUInt16LE(start + 14);
    } else if (id === "data") {
      data = buffer.subarray(start, start + size);
    }
    offset = start + size + (size % 2);
  }
  if (!data || !sampleRate || channels !== 1 || bits !== 16) {
    throw new Error(`仅支持 16-bit mono WAV: ${path}`);
  }
  const samples = new Float32Array(data.length / 2);
  for (let index = 0; index < samples.length; index += 1) {
    samples[index] = data.readInt16LE(index * 2) / 32768;
  }
  return { sampleRate, channels, samples };
}

function envelope(samples: Float32Array, sampleRate: number, stepMs = 10): Float32Array {
  const block = Math.max(1, Math.round((sampleRate * stepMs) / 1000));
  const result = new Float32Array(Math.ceil(samples.length / block));
  for (let index = 0; index < result.length; index += 1) {
    let peak = 0;
    const start = index * block;
    const end = Math.min(samples.length, start + block);
    for (let cursor = start; cursor < end; cursor += 1) {
      peak = Math.max(peak, Math.abs(samples[cursor]));
    }
    result[index] = peak;
  }
  return result;
}

function correlationPeak(capture: Float32Array, reference: Float32Array) {
  const shorter = capture.length <= reference.length ? capture : reference;
  const longer = capture.length <= reference.length ? reference : capture;
  let best = 0;
  let bestLag = 0;
  for (let lag = 0; lag + shorter.length <= longer.length; lag += 1) {
    let dot = 0;
    let leftNorm = 0;
    let rightNorm = 0;
    for (let index = 0; index < shorter.length; index += 1) {
      const left = shorter[index];
      const right = longer[lag + index];
      dot += left * right;
      leftNorm += left * left;
      rightNorm += right * right;
    }
    const denominator = Math.sqrt(leftNorm * rightNorm);
    const value = denominator > 0 ? dot / denominator : 0;
    if (value > best) {
      best = value;
      bestLag = lag;
    }
  }
  return { peak: best, lag_steps: bestLag };
}

test("真实getUserMedia可与本机播放形成相关峰", async ({ page }) => {
  test.setTimeout(90000);
  const reference = readWav(referencePath);
  const referenceHash = createHash("sha256").update(readFileSync(referencePath)).digest("hex");
  await page.context().grantPermissions(["microphone"]);
  await page.goto("/");

  const playbackStartedAt = Date.now();
  const playbackPromise = execFileAsync("afplay", [referencePath]);
  const capture: CaptureResult = await page.evaluate(async () => {
    if (!navigator.mediaDevices?.getUserMedia) {
      return { status: "unavailable", reason: "getUserMedia is unavailable" };
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
    } catch (error) {
      return {
        status: "denied",
        reason: error instanceof Error ? error.message : String(error),
      };
    }
    const track = stream.getAudioTracks()[0];
    const settings = track.getSettings();
    const context = new AudioContext();
    const source = context.createMediaStreamSource(stream);
    const processor = context.createScriptProcessor(4096, 1, 1);
    const silentGain = context.createGain();
    silentGain.gain.value = 0;
    const samples: number[] = [];
    const maximumSamples = Math.ceil(context.sampleRate * 7);
    processor.onaudioprocess = (event) => {
      if (samples.length >= maximumSamples) return;
      const input = event.inputBuffer.getChannelData(0);
      for (const value of input) {
        if (samples.length >= maximumSamples) break;
        samples.push(value);
      }
    };
    source.connect(processor);
    processor.connect(silentGain);
    silentGain.connect(context.destination);
    await new Promise((resolve) => setTimeout(resolve, 7000));
    processor.disconnect();
    source.disconnect();
    silentGain.disconnect();
    track.stop();
    await context.close();
    let peak = 0;
    let squaredSum = 0;
    for (const value of samples) {
      peak = Math.max(peak, Math.abs(value));
      squaredSum += value * value;
    }
    return {
      status: "granted",
      settings,
      sampleRate: context.sampleRate,
      sampleCount: samples.length,
      peak,
      rms: Math.sqrt(squaredSum / Math.max(1, samples.length)),
      samples,
    };
  });
  const playback = await playbackPromise;
  const playbackEndedAt = Date.now();

  const { samples: captureSampleValues, ...captureEvidence } = capture;
  const captureSamples = new Float32Array(captureSampleValues ?? []);
  const correlation = captureSamples.length
    ? correlationPeak(
        envelope(captureSamples, capture.sampleRate ?? 48000),
        envelope(reference.samples, reference.sampleRate),
      )
    : { peak: 0, lag_steps: 0 };
  const evidence = {
    schema: "voice-m4-device-probe-v1",
    case: deviceCase,
    status: "recorded",
    reference: {
      path: referencePath,
      sha256: referenceHash,
      sample_rate: reference.sampleRate,
      channels: reference.channels,
      duration_seconds: reference.samples.length / reference.sampleRate,
    },
    capture: captureEvidence,
    playback: {
      command: "afplay",
      started_at: playbackStartedAt,
      ended_at: playbackEndedAt,
      stdout: playback.stdout,
      stderr: playback.stderr,
    },
    correlation,
    unverified: {
      headset_output: "not_run",
      independent_dual_talk_source: "not_run",
    },
  };
  mkdirSync(dirname(evidencePath), { recursive: true });
  writeFileSync(evidencePath, JSON.stringify(evidence, null, 2), "utf8");

  expect(capture.status).toBe("granted");
  expect(capture.sampleCount ?? 0).toBeGreaterThan(0);
  expect(capture.peak ?? 0).toBeGreaterThan(0.01);
  expect(correlation.peak).toBeGreaterThan(0.15);
});
