import { afterEach, describe, expect, it, vi } from "vitest";

const { queryRaw } = vi.hoisted(() => ({ queryRaw: vi.fn() }));
vi.mock("@/lib/prisma", () => ({ prisma: { $queryRawUnsafe: queryRaw } }));

import { prisma } from "@/lib/prisma";
import { GET } from "./route";

describe("GET /api/__test__/tool-activity/identity", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.clearAllMocks();
  });

  it("is hidden outside the controlled runtime", async () => {
    const response = await GET();
    expect(response.status).toBe(404);
    expect(prisma.$queryRawUnsafe).not.toHaveBeenCalled();
  });

  it("reports only the current Prisma connection identity and checks the run binding", async () => {
    vi.stubEnv("BOB108_RUNTIME_DIAGNOSTICS", "1");
    vi.stubEnv("BOB108_EXPECTED_DATABASE", "bob108_browser");
    vi.stubEnv("BOB108_EXPECTED_SYSTEM_IDENTIFIER", "server-1");
    vi.stubEnv("BOB108_RUN_ID", "control-01");
    queryRaw.mockResolvedValue([{ database: "bob108_browser", address: "172.27.0.3", port: 5432, system_identifier: "server-1" }]);

    const response = await GET();
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      run_id: "control-01",
      database_identity: { database: "bob108_browser", address: "172.27.0.3", port: 5432, system_identifier: "server-1" },
    });
  });

  it("rejects an unexpected Prisma database identity", async () => {
    vi.stubEnv("BOB108_RUNTIME_DIAGNOSTICS", "1");
    vi.stubEnv("BOB108_EXPECTED_DATABASE", "bob108_browser");
    vi.stubEnv("BOB108_EXPECTED_SYSTEM_IDENTIFIER", "server-1");
    queryRaw.mockResolvedValue([{ database: "hexis_memory", address: "127.0.0.1", port: 5432, system_identifier: "server-1" }]);

    const response = await GET();
    expect(response.status).toBe(409);
  });
});
