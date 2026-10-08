import { afterEach, describe, expect, it, vi } from "vitest";

import { POST } from "./route";

describe("POST /api/__test__/tool-activity/voice-recall", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("is hidden outside the controlled runtime", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const response = await POST(new Request("http://localhost/api/__test__/tool-activity/voice-recall", { method: "POST", body: "{}" }));
    expect(response.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("forwards a selected owned session to the loopback API harness", async () => {
    vi.stubEnv("BOB108_CONTROLLED_RUNTIME", "1");
    vi.stubEnv("HEXIS_API_URL", "http://127.0.0.1:44819");
    const fetchMock = vi.fn(async () => Response.json({ session_id: "s1", call_id: "c1", request_event_id: "e1", result_event_id: "e2" }));
    vi.stubGlobal("fetch", fetchMock);
    const body = { session_id: "s1" };

    const response = await POST(new Request("http://localhost/api/__test__/tool-activity/voice-recall", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }));

    expect(response.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledWith(
      new URL("http://127.0.0.1:44819/__test__/tool-activity/voice-recall"),
      expect.objectContaining({ method: "POST", body: JSON.stringify(body) }),
    );
  });
});
