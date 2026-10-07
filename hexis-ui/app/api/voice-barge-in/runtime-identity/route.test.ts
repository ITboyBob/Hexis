import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET } from "./route";

const { queryRaw } = vi.hoisted(() => ({ queryRaw: vi.fn() }));
vi.mock("@/lib/prisma", () => ({ prisma: { $queryRaw: (...args: unknown[]) => queryRaw(...args) } }));

let cacheHome = "";
let envRecordPath = "";

beforeEach(async () => {
  vi.stubGlobal("fetch", vi.fn());
  cacheHome = await mkdtemp(path.join(os.tmpdir(), "hexis-runtime-identity-"));
  envRecordPath = path.join(cacheHome, "hexis", "voice-barge-in", "environments", "vb-test", "environment.json");
  await mkdir(path.dirname(envRecordPath), { recursive: true });
  await writeFile(envRecordPath, JSON.stringify({
    schema_version: 1,
    env_id: "vb-test",
    status: "ready",
    owned_resources: { project_name: "hexis-vb-test", talk_host_port: 43852 },
    db_identity: { talk: { database: "postgres", server_port: 5432 } },
  }));
  queryRaw.mockReset();
  vi.stubEnv("XDG_CACHE_HOME", cacheHome);
  vi.stubEnv("HEXIS_VOICE_BARGE_IN_ENV", envRecordPath);
  vi.stubEnv("HEXIS_DATABASE_URL", "postgresql://unit:secret@talk-db:5432/postgres");
  vi.stubEnv("HEXIS_API_URL", "http://hexis-api:43817");
});

afterEach(async () => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  await rm(cacheHome, { recursive: true, force: true });
});

describe("GET /api/voice-barge-in/runtime-identity", () => {
  it("is disabled without an explicitly selected E1 record", async () => {
    vi.stubEnv("HEXIS_VOICE_BARGE_IN_ENV", "");
    const response = await GET();
    expect(response.status).toBe(404);
    expect(queryRaw).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("reports the actual selected UI database and API health without credentials", async () => {
    queryRaw.mockResolvedValue([{ database: "postgres", port: 5432, address: "172.20.0.3" }]);
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({ status: "ok", checks: { db: true } }), { status: 200 }));
    const response = await GET();
    const body = await response.json();
    expect(response.status).toBe(200);
    expect(body).toMatchObject({
      env_id: "vb-test",
      database: { database: "postgres", server_port: 5432, url_host: "talk-db", url_port: 5432 },
      api: { status: "ok", checks: { db: true }, url_host: "hexis-api", url_port: 43817 },
    });
    expect(JSON.stringify(body)).not.toContain("secret");
  });

  it("rejects an E1 record outside the selected cache root", async () => {
    vi.stubEnv("HEXIS_VOICE_BARGE_IN_ENV", path.join(os.tmpdir(), "other", "environment.json"));
    const response = await GET();
    expect(response.status).toBe(403);
    expect(queryRaw).not.toHaveBeenCalled();
  });
});
