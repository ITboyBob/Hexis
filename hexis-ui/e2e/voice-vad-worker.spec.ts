import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import assetManifest from "../lib/voice-realtime/vad-assets-manifest.json";
import speechConfig from "../lib/voice-realtime/speech-detection-config.json";

type WorkerRunRecord = {
  kind: "worker_ui_run";
  mode: "worker";
  target: "dev" | "standalone";
  status: "ready";
  ui_url: string;
  runtime_flag: "HEXIS_VAD_TEST_HARNESS=1";
  source_fingerprints: Record<string, string>;
};

const runPath = process.env.VOICE_BARGE_IN_WORKER_RUN;
const wavPath = process.env.VOICE_BARGE_IN_WORKER_WAV;
if (wavPath && !path.isAbsolute(wavPath)) throw new Error("VOICE_BARGE_IN_WORKER_WAV must name an explicitly selected absolute existing WAV path.");
if (!runPath || !path.isAbsolute(runPath)) {
  throw new Error("VOICE_BARGE_IN_WORKER_RUN must name an explicit absolute W1 JSON path; start a worker UI first.");
}

const workerRun = JSON.parse(readFileSync(runPath, "utf8")) as WorkerRunRecord;
if (
  workerRun.kind !== "worker_ui_run"
  || workerRun.mode !== "worker"
  || !["dev", "standalone"].includes(workerRun.target)
  || workerRun.status !== "ready"
  || workerRun.runtime_flag !== "HEXIS_VAD_TEST_HARNESS=1"
  || !workerRun.ui_url.startsWith("http://127.0.0.1:")
  || !workerRun.source_fingerprints
) {
  throw new Error("Selected W1 is not a ready loopback Worker test run with an explicit harness flag.");
}

const sourceRoot = path.resolve(process.cwd(), "..");
for (const [relativePath, expectedHash] of Object.entries(workerRun.source_fingerprints)) {
  const filePath = path.resolve(sourceRoot, relativePath);
  if (!filePath.startsWith(`${sourceRoot}${path.sep}`) || expectedHash === "missing") {
    throw new Error(`W1 source fingerprint path is unsafe or missing: ${relativePath}`);
  }
  const actualHash = createHash("sha256").update(readFileSync(filePath)).digest("hex");
  if (actualHash !== expectedHash) {
    throw new Error(`W1 source fingerprint changed after startup: ${relativePath}`);
  }
}

test.use({
  channel: "chrome",
  headless: false,
  baseURL: workerRun.ui_url,
});

test.describe("real Worker/WASM standalone component", () => {
  test("records actual probabilities for an explicitly selected existing WAV", async ({ page }, testInfo) => {
    test.skip(!wavPath, "Select an existing local WAV explicitly with VOICE_BARGE_IN_WORKER_WAV.");
    const sourceBytes = readFileSync(wavPath!);
    const unexpectedRequests: string[] = [];
    const pageErrors: string[] = [];
    const localOrigin = new URL(workerRun.ui_url).origin;
    page.on("pageerror", (error) => pageErrors.push(error.message));
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.origin !== localOrigin || url.pathname.startsWith("/api/")) unexpectedRequests.push(request.url());
    });
    await page.goto(workerRun.ui_url);
    await page.getByRole("button", { name: "Start Worker" }).click();
    await expect(page.getByText("Capture gate: open")).toBeVisible({ timeout: 30000 });
    await page.getByLabel("Local WAV file").setInputFiles(wavPath!);
    await page.getByRole("button", { name: "Run selected WAV" }).click();
    await expect(page.getByTestId("wav-run-status")).toContainText("completed", { timeout: 90000 });
    const result = JSON.parse((await page.getByTestId("wav-probability-trajectory").textContent())!);
    expect(result.file_name).toBe(path.basename(wavPath!));
    expect(result.file_bytes).toBe(sourceBytes.length);
    expect(result.sample_rate).toBe(speechConfig.audio.sample_rate);
    expect(result.real_samples).toBeGreaterThan(0);
    const frameSamples = speechConfig.audio.model_frame_samples;
    expect(result.frames.length).toBe(Math.ceil(result.real_samples / frameSamples));
    for (const [index, frame] of result.frames.entries()) {
      expect(frame.start_sample).toBe(index * frameSamples);
      expect(frame.end_sample).toBe((index + 1) * frameSamples);
      expect(Number.isFinite(frame.probability)).toBe(true);
      expect(frame.probability).toBeGreaterThanOrEqual(0);
      expect(frame.probability).toBeLessThanOrEqual(1);
    }
    expect(pageErrors).toEqual([]);
    expect(unexpectedRequests).toEqual([]);
    await testInfo.attach("existing-wav-worker-probabilities", {
      contentType: "application/json",
      body: Buffer.from(JSON.stringify({ source_path: wavPath, source_sha256: createHash("sha256").update(sourceBytes).digest("hex"), ...result }, null, 2)),
    });
    await page.getByRole("button", { name: "Close Worker" }).click();
    await expect(page.getByText("Capture gate: closed")).toBeVisible();
  });

  test("loads the pinned v6/ORT assets and runs a deterministic frame", async ({ page }) => {
    const assetResponses = new Map<string, number>();
    const pageErrors: string[] = [];
    const unexpectedRequests: string[] = [];
    const localOrigin = new URL(workerRun.ui_url).origin;
    page.on("response", (response) => {
      if (response.url().includes("/voice-realtime/vad-assets/")) {
        assetResponses.set(new URL(response.url()).pathname, response.status());
      }
    });
    page.on("pageerror", (error) => pageErrors.push(error.message));
    page.on("request", (request) => {
      const requestUrl = new URL(request.url());
      if (requestUrl.origin !== localOrigin || requestUrl.pathname.startsWith("/api/")) {
        unexpectedRequests.push(request.url());
      }
    });

    await page.goto(workerRun.ui_url);
    await expect(page.getByRole("heading", { name: "Speech VAD Worker test" })).toBeVisible();
    await expect(page.getByText(/does not open the microphone or connect to the API, database, or a provider/i)).toBeVisible();
    await expect(page.getByText("Capture gate: closed")).toBeVisible();

    await page.getByRole("button", { name: "Start Worker" }).click();
    await expect(page.getByText("Capture gate: open")).toBeVisible({ timeout: 30000 });
    await page.getByRole("button", { name: "Run zero-frame inference" }).click();
    await expect(page.getByTestId("worker-result")).toContainText(/probability 0\.[0-9]{4}/, { timeout: 30000 });

    expect(assetResponses.get("/voice-realtime/vad-assets/models/silero_vad_v6.onnx")).toBe(200);
    expect(assetResponses.get("/voice-realtime/vad-assets/ort-1.22.0/ort-wasm-simd-threaded.mjs")).toBe(200);
    expect(assetResponses.get("/voice-realtime/vad-assets/ort-1.22.0/ort-wasm-simd-threaded.wasm")).toBe(200);
    expect(pageErrors).toEqual([]);
    expect(unexpectedRequests).toEqual([]);
  });

  test("serves the pinned ORT wrapper through a separate static request", async ({ request }) => {
    const wrapper = assetManifest.assets.find((asset) => asset.id === "ort.wasm.min.mjs")!;
    const response = await request.get(new URL(wrapper.url_path, workerRun.ui_url).href);
    expect(response.status()).toBe(200);
    const bytes = await response.body();
    expect(bytes.length).toBe(wrapper.bytes);
    expect(createHash("sha256").update(bytes).digest("hex")).toBe(wrapper.sha256);
  });

  test("reset waits for the simulated server resume and close releases the Worker", async ({ page }) => {
    await page.goto(workerRun.ui_url);
    await page.getByRole("button", { name: "Start Worker" }).click();
    await expect(page.getByText("Capture gate: open")).toBeVisible({ timeout: 30000 });

    await page.getByRole("button", { name: "Reset Worker" }).click();
    await expect(page.getByText("Worker model state reset; simulated server resume acknowledged.")).toBeVisible();
    await expect(page.getByText("Generation: 1")).toBeVisible();
    await expect(page.getByText("Capture gate: open")).toBeVisible();

    await page.getByRole("button", { name: "Close Worker" }).click();
    await expect(page.getByText("Capture gate: closed")).toBeVisible();
    await expect(page.getByRole("button", { name: "Start Worker" })).toBeEnabled();
  });
});
