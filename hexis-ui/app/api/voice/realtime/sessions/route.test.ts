import { afterEach, describe, expect, it, vi } from "vitest";

import { POST } from "./route";

describe("/api/voice/realtime/sessions", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("proxies the chat session id to the Python realtime API", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          session_id: "session-1",
          media_session_id: "media-1",
          websocket_url: "ws://127.0.0.1:43817/api/voice/realtime/media-1",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const response = await POST(
      new Request("http://local/api/voice/realtime/sessions", {
        method: "POST",
        body: JSON.stringify({ session_id: "session-1" }),
      }),
    );

    expect(response.status).toBe(200);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:43817/api/voice/realtime/sessions",
    );
    expect(fetchMock.mock.calls[0][1].body).toBe('{"session_id":"session-1"}');
  });
});
