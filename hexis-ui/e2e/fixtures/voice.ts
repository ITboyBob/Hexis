import { spawn, type ChildProcess } from "node:child_process";
import { request } from "node:http";
import { resolve } from "node:path";
import type { Page } from "@playwright/test";

const root = resolve(process.cwd(), "..");
const apiHealthUrl = "http://127.0.0.1:43817/health";
const uiHealthUrl = "http://127.0.0.1:3477/";

export type VoiceServiceHandle = {
  apiProcess: ChildProcess | null;
  uiProcess: ChildProcess | null;
  apiLogs: string[];
  uiLogs: string[];
  close: () => Promise<void>;
};

export async function installVoiceWavFeed(
  page: Page,
  wavs: Record<string, string>,
): Promise<void> {
  await page.context().grantPermissions(["microphone"]);
  await page.addInitScript((encodedWavs: Record<string, string>) => {
    let audioContext: AudioContext | null = null;
    let destination: MediaStreamAudioDestinationNode | null = null;
    const target = window as Window & {
      __voiceWavFeed?: {
        play: (name: string) => Promise<void>;
        trackState: () => { id: string; enabled: boolean; readyState: string } | null;
      };
    };
    target.__voiceWavFeed = {
      trackState() {
        const track = destination?.stream.getAudioTracks()[0];
        return track ? { id: track.id, enabled: track.enabled, readyState: track.readyState } : null;
      },
      async play(name: string): Promise<void> {
        if (!audioContext || !destination) throw new Error("Talk microphone stream is not ready");
        const encoded = encodedWavs[name];
        if (!encoded) throw new Error(`Unknown voice fixture: ${name}`);
        const bytes = Uint8Array.from(atob(encoded), (character) => character.charCodeAt(0));
        const buffer = await audioContext.decodeAudioData(bytes.buffer);
        const source = audioContext.createBufferSource();
        source.buffer = buffer;
        source.connect(destination);
        await new Promise<void>((resolvePlayback, rejectPlayback) => {
          source.onended = () => resolvePlayback();
          try {
            source.start();
          } catch (error) {
            rejectPlayback(error);
          }
        });
      },
    };
    navigator.mediaDevices.getUserMedia = async () => {
      audioContext = new AudioContext();
      await audioContext.resume();
      destination = audioContext.createMediaStreamDestination();
      return destination.stream;
    };
  }, wavs);
}

function collectLogs(process: ChildProcess | null, target: string[]): void {
  const append = (chunk: Buffer) => {
    target.push(chunk.toString("utf8"));
    while (target.reduce((size, value) => size + value.length, 0) > 2_000_000) {
      target.shift();
    }
  };
  process?.stdout?.on("data", append);
  process?.stderr?.on("data", append);
}

function waitForHttp(url: string, timeoutMs = 60_000): Promise<void> {
  const startedAt = Date.now();
  return new Promise((resolvePromise, rejectPromise) => {
    const attempt = () => {
      const req = request(url, { method: "GET", timeout: 1000 }, (response) => {
        response.resume();
        if (response.statusCode && response.statusCode < 500) {
          resolvePromise();
          return;
        }
        retry();
      });
      req.on("error", retry);
      req.on("timeout", () => {
        req.destroy();
        retry();
      });
      req.end();
    };
    const retry = () => {
      if (Date.now() - startedAt >= timeoutMs) {
        rejectPromise(new Error(`Timed out waiting for ${url}`));
        return;
      }
      setTimeout(attempt, 500);
    };
    attempt();
  });
}

export async function prewarmVoiceUI(uiUrl: string): Promise<void> {
  const origin = new URL(uiUrl);
  if (origin.protocol !== "http:" || origin.hostname !== "127.0.0.1") {
    throw new Error("Voice UI prewarm requires an explicitly selected local URL.");
  }
  const nil = "00000000-0000-4000-8000-000000000000";
  for (const path of [
    "/api/status", "/api/outbox", "/api/pwa/presence", "/api/chat/session",
    "/api/voice/realtime/sessions", "/api/voice/playback",
    `/api/m5/tasks?origin_session_id=${nil}`,
    `/api/m5/tasks/events?origin_session_id=${nil}`,
    `/api/m5/selections/${nil}/confirm`, `/api/m5/selections/${nil}/cancel`,
    "/manifest.webmanifest", "/pwa-icon/192", "/pwa-icon/512", "/chat",
  ]) {
    await waitForHttp(new URL(path, origin).toString());
  }
}

async function isReachable(url: string): Promise<boolean> {
  try {
    await waitForHttp(url, 1_000);
    return true;
  } catch {
    return false;
  }
}

function stopProcess(process: ChildProcess | null): Promise<void> {
  return new Promise((resolvePromise) => {
    if (!process || process.pid === undefined) {
      resolvePromise();
      return;
    }
    try {
      process.kill("SIGTERM");
    } catch {
      // The process may already have exited.
    }
    process.once("exit", () => resolvePromise());
    setTimeout(() => {
      try {
        if (process.pid !== undefined) process.kill("SIGKILL");
      } catch {
        // Ignore a race with normal exit.
      }
      resolvePromise();
    }, 5_000);
  });
}

export async function startVoiceServices(): Promise<VoiceServiceHandle> {
  const apiAlreadyRunning = await isReachable(apiHealthUrl);
  const uiAlreadyRunning = await isReachable(uiHealthUrl);
  let apiProcess: ChildProcess | null = null;
  let uiProcess: ChildProcess | null = null;
  const apiLogs: string[] = [];
  const uiLogs: string[] = [];

  if (!apiAlreadyRunning) {
    apiProcess = spawn(".venv/bin/python", ["-m", "apps.hexis_api"], {
      cwd: root,
      env: {
        ...process.env,
        HEXIS_BROWSER_API_URL: "http://127.0.0.1:43817",
        HEXIS_UI_ORIGIN: "http://127.0.0.1:3477",
        HEXIS_M4_VOICE_CONFIG:
          "/Users/itboybob/.cache/hexis/bob101-controlled-evidence/config.template.json",
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
  }
  if (!uiAlreadyRunning) {
    uiProcess = spawn("npm", ["run", "dev"], {
      cwd: process.cwd(),
      env: {
        ...process.env,
        HEXIS_API_URL: "http://127.0.0.1:43817",
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
  }
  collectLogs(apiProcess, apiLogs);
  collectLogs(uiProcess, uiLogs);

  try {
    await waitForHttp(apiHealthUrl);
    await waitForHttp(uiHealthUrl);
  } catch (error) {
    await stopProcess(apiProcess);
    await stopProcess(uiProcess);
    throw error;
  }

  return {
    apiProcess,
    uiProcess,
    apiLogs,
    uiLogs,
    close: async () => {
      await stopProcess(apiProcess);
      await stopProcess(uiProcess);
    },
  };
}
