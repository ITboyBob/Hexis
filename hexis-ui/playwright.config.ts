import { defineConfig } from "@playwright/test";
import path from "node:path";
import os from "node:os";

const port = Number(process.env.HEXIS_UI_PORT || 3477);
const rtcRun = process.env.HEXIS_RTC_BROWSER_TEST === "1";
const fixturePort = Number(process.env.HEXIS_RTC_FIXTURE_PORT || port + 1);
const rtcArtifacts = path.join(process.env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache"), "hexis", "voice-rtc", "browser");

export default defineConfig({
  testDir: "./e2e",
  timeout: 120000,
  expect: { timeout: 20000 },
  ...(rtcRun ? { workers: 1, outputDir: path.join(rtcArtifacts, "results"), webServer: {
    command: `node node_modules/next/dist/bin/next dev --hostname 127.0.0.1 -p ${port}`,
    url: `http://127.0.0.1:${port}/chat`, reuseExistingServer: false, timeout: 120000,
    env: { HEXIS_API_URL: `http://127.0.0.1:${fixturePort}`, HEXIS_RTC_FIXTURE_PORT: String(fixturePort),
      HEXIS_UI_TEST_TARGET: "talk", HEXIS_UI_DIST_DIR: ".next/voice-rtc-test", HEXIS_VOICE_BARGE_IN_ENV: "" },
  } } : {}),
  projects: [
    { name: "chromium", testIgnore: "voice-rtc.spec.ts" },
    { name: "rtc-external-chrome", testMatch: "voice-rtc.spec.ts", use: {
      channel: "chrome", headless: false, permissions: ["microphone"],
      launchOptions: { args: ["--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required", "--mute-audio"] },
    } },
  ],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  reporter: [["list"], ["html", { open: "never", ...(rtcRun ? { outputFolder: path.join(rtcArtifacts, "report") } : {}) }]],
});
