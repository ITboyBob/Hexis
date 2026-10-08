import { afterEach, describe, expect, it, vi } from "vitest";

import { POST as start } from "./start/route";
import { POST as status } from "./status/route";
import { POST as release } from "./release/route";

const routes = [
  ["start", start],
  ["status", status],
  ["release", release],
] as const;

describe("controlled M5 barrier proxy routes", () => {
  afterEach(() => vi.unstubAllGlobals());

  it.each(routes)("hides %s outside the controlled runtime", async (_name, handler) => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const response = await handler(new Request("http://localhost", { method: "POST", body: "{}" }));
    expect(response.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    ["start", start, "/background/start"],
    ["status", status, "/background/status"],
    ["release", release, "/background/release"],
  ] as const)("forwards %s to its loopback API route", async (_name, handler, suffix) => {
    vi.stubEnv("BOB108_CONTROLLED_RUNTIME", "1");
    vi.stubEnv("HEXIS_API_URL", "http://127.0.0.1:44819");
    const fetchMock = vi.fn(async () => Response.json({ forwarded: true }));
    vi.stubGlobal("fetch", fetchMock);
    const body = { barrier_id: "controlled-barrier" };
    const response = await handler(new Request("http://localhost", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }));
    expect(response.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledWith(
      new URL(`http://127.0.0.1:44819/__test__/tool-activity${suffix}`),
      expect.objectContaining({ method: "POST", body: JSON.stringify(body) }),
    );
  });
});
