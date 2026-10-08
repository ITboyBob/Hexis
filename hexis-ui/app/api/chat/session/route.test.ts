import { afterEach, describe, expect, it, vi } from "vitest";

import { POST } from "./route";
import { prisma } from "@/lib/prisma";

vi.mock("@/lib/prisma", () => ({
  prisma: {
    $queryRawUnsafe: vi.fn(),
  },
}));

const query = vi.mocked(prisma.$queryRawUnsafe);

describe("/api/chat/session", () => {
  afterEach(() => {
    query.mockReset();
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("creates a web-owned chat session in the database", async () => {
    query.mockResolvedValueOnce([
      {
        session: {
          session_id: "11111111-1111-4111-8111-111111111111",
          surface: "web",
          status: "active",
        },
      },
    ]);

    const response = await POST();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body).toMatchObject({
      session_id: "11111111-1111-4111-8111-111111111111",
      surface: "web",
      status: "active",
    });
    expect(query).toHaveBeenCalledWith(
      "SELECT get_or_create_chat_session(NULL::uuid, $1::text, NULL::text, $2::jsonb) AS session",
      "web",
      expect.any(String)
    );
    expect(JSON.parse(query.mock.calls[0][2] as string)).toMatchObject({
      source: "web",
      created_by: "user",
    });
  });

  it("fails loudly when the database does not return a session id", async () => {
    query.mockResolvedValueOnce([{ session: { surface: "web" } }]);

    const response = await POST();
    const body = await response.json();

    expect(response.status).toBe(500);
    expect(body.error).toBe("database did not return a session id");
  });

  it("registers the selected M6 server-owned session without using the ambient DB session creator", async () => {
    vi.stubEnv("HEXIS_VOICE_BARGE_IN_ENV", "/selected/e1.json");
    vi.stubEnv("HEXIS_API_URL", "http://127.0.0.1:43817");
    const payload = { session_id: "11111111-1111-4111-8111-111111111111", api_start_id: "start-m6", session_execution_policy: { policy_id: "m6-c5-realtime-only-v1" } };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 })));
    const response = await POST();
    expect(await response.json()).toEqual(payload);
    expect(fetch).toHaveBeenCalledWith(new URL("http://127.0.0.1:43817/api/voice-barge-in/chat-session"), expect.objectContaining({ method: "POST" }));
    expect(query).not.toHaveBeenCalled();
  });

  it("shows a selected M6 session registration refusal without falling back to an old session", async () => {
    vi.stubEnv("HEXIS_VOICE_BARGE_IN_ENV", "/selected/e1.json");
    vi.stubEnv("HEXIS_API_URL", "http://127.0.0.1:43817");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Owned session registration denied" }), { status: 403 })));
    const response = await POST();
    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({ error: "Owned session registration denied" });
    expect(query).not.toHaveBeenCalled();
  });
});
