import { afterEach, describe, expect, it, vi } from "vitest";

import { POST } from "./route";

describe("/api/voice/playback", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("proxies playback evidence to the Python API without changing the body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ accepted: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const response = await POST(
      new Request("http://local/api/voice/playback", {
        method: "POST",
        body: JSON.stringify({ action: "progress", producer_seq: 4 }),
      }),
    );

    expect(response.status).toBe(200);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:43817/api/voice/playback",
    );
    expect(fetchMock.mock.calls[0][1].body).toBe(
      '{"action":"progress","producer_seq":4}',
    );
  });
});
