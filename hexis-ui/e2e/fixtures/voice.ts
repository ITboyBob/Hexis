import { spawn, type ChildProcess } from "node:child_process";
import { request } from "node:http";
import { resolve } from "node:path";

const root = resolve(process.cwd(), "..");
const apiHealthUrl = "http://127.0.0.1:43817/health";
const uiHealthUrl = "http://127.0.0.1:3477/";

export type VoiceServiceHandle = {
  apiProcess: ChildProcess | null;
  uiProcess: ChildProcess | null;
  close: () => Promise<void>;
};

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

  if (!apiAlreadyRunning) {
    apiProcess = spawn(".venv/bin/python", ["-m", "apps.hexis_api"], {
      cwd: root,
      env: {
        ...process.env,
        HEXIS_BROWSER_API_URL: "http://127.0.0.1:43817",
        HEXIS_UI_ORIGIN: "http://127.0.0.1:3477",
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
    close: async () => {
      await stopProcess(apiProcess);
      await stopProcess(uiProcess);
    },
  };
}
