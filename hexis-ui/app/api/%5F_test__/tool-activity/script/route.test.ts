import { afterEach, describe, expect, it, vi } from "vitest";

import { POST } from "./route";

describe("POST /api/__test__/tool-activity/script", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("is unavailable unless the server explicitly enables the owned controlled runtime", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const response = await POST(new Request("http://localhost/api/__test__/tool-activity/script", { method: "POST", body: "{}" }));
    expect(response.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("forwards the selected scripted actions to the loopback provider", async () => {
    vi.stubEnv("BOB108_CONTROLLED_RUNTIME", "1");
    vi.stubEnv("BOB108_SCRIPTED_PROVIDER_URL", "http://127.0.0.1:44818");
    const fetchMock = vi.fn(async () => Response.json({ armed: true, action_count: 2 }));
    vi.stubGlobal("fetch", fetchMock);
    const body = { actions: [{ content: "controlled" }] };
    const response = await POST(new Request("http://localhost/api/__test__/tool-activity/script", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }));

    expect(response.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledWith(
      new URL("http://127.0.0.1:44818/__control/script"),
      expect.objectContaining({ method: "POST", body: JSON.stringify(body) }),
    );
  });
});
