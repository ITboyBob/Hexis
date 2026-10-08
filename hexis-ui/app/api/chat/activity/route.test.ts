import { afterEach, describe, expect, it, vi } from "vitest";

import { GET } from "./route";

describe("GET /api/chat/activity", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("proxies the selected session and independent stream cursor to the Python API", async () => {
    const sessionId = "10800000-0000-4000-8000-000000000001";
    const cursor = JSON.stringify({ stream_a: 4, stream_b: 2 });
    const fetchMock = vi.fn(async () => Response.json({ events: [], cursor: {} }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("HEXIS_API_URL", "http://127.0.0.1:44817");

    const request = new Request(
      `http://localhost/api/chat/activity?session_id=${sessionId}&limit=50&cursor=${encodeURIComponent(cursor)}`,
    );
    const response = await GET(request);

    expect(response.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/api/chat/activity?");
    expect(url).toContain(`session_id=${sessionId}`);
    expect(new URL(url).searchParams.get("cursor")).toBe(cursor);
    expect(init.method).toBe("GET");
  });

  it("returns a useful error when the Python API cannot be reached", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("connection refused"); }));
    vi.stubEnv("HEXIS_API_URL", "http://127.0.0.1:44817");

    const response = await GET(new Request("http://localhost/api/chat/activity?session_id=valid"));
    expect(response.status).toBe(502);
    expect((await response.json()).error).toContain("connection refused");
  });
});
