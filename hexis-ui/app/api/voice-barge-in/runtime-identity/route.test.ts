import { mkdtemp, mkdir, rm, writeFile, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET } from "./route";

const { queryRaw } = vi.hoisted(() => ({ queryRaw: vi.fn() }));
vi.mock("@/lib/prisma", () => ({ prisma: { $queryRaw: (...args: unknown[]) => queryRaw(...args) } }));

let cacheHome = "";
let envRecordPath = "";
const repositoryRoot = path.resolve(process.cwd(), "..");
let policy: Record<string, unknown>;
let browserApiUrl = "";

function healthyApi() {
  return { status: "ok", checks: { db: true }, voice_barge_in: {
    environment_id: "vb-test", database: "postgres", start_id: "start-test",
    acceptance_session_id: "11111111-1111-4111-8111-111111111111",
    acceptance_session_registered: true, session_execution_policy: policy,
    migration_verification: { status: "verified_read_only", mode: "committed_applied_only" },
    browser_api_url: browserApiUrl,
    ui_origin: "http://127.0.0.1:3482",
  } };
}

beforeEach(async () => {
  vi.stubGlobal("fetch", vi.fn());
  const profile = JSON.parse(await readFile(path.join(repositoryRoot, "config/voice-barge-in/run-profile.json"), "utf8"));
  policy = profile.session_execution_policy;
  browserApiUrl = profile.api_url.replace(/\/$/, "");
  cacheHome = await mkdtemp(path.join(os.tmpdir(), "hexis-runtime-identity-"));
  envRecordPath = path.join(cacheHome, "hexis", "voice-barge-in", "environments", "vb-test", "environment.json");
  await mkdir(path.dirname(envRecordPath), { recursive: true });
  await writeFile(envRecordPath, JSON.stringify({
    schema_version: 1,
    env_id: "vb-test",
    status: "ready",
    repository_root: repositoryRoot,
    owned_resources: { project_name: "hexis-vb-test", talk_host_port: 43852, api_process: { start_id: "start-test" } },
    db_identity: { talk: { database: "postgres", server_port: 5432 } },
  }));
  queryRaw.mockReset();
  vi.stubEnv("XDG_CACHE_HOME", cacheHome);
  vi.stubEnv("HEXIS_VOICE_BARGE_IN_ENV", envRecordPath);
  vi.stubEnv("HEXIS_DATABASE_URL", "postgresql://unit:secret@talk-db:5432/postgres");
  vi.stubEnv("HEXIS_API_URL", "http://hexis-api:43817");
  vi.stubEnv("HEXIS_BROWSER_API_URL", browserApiUrl);
  vi.stubEnv("HEXIS_UI_ORIGIN", "http://127.0.0.1:3482");
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
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify(healthyApi()), { status: 200 }));
    const response = await GET();
    const body = await response.json();
    expect(response.status).toBe(200);
    expect(body).toMatchObject({
      env_id: "vb-test",
      database: { database: "postgres", server_port: 5432, url_host: "talk-db", url_port: 5432 },
      api: { status: "ok", checks: { db: true }, url_host: "hexis-api", url_port: 43817 },
    });
    expect(JSON.stringify(body)).not.toContain("secret");
    expect(body.api.session_execution_policy).toEqual(policy);
    expect(body.api.acceptance_session_registered).toBe(true);
  });

  it.each(["policy", "registration", "environment", "start", "migration", "browser_url"])("rejects mismatched acceptance %s", async (field) => {
    queryRaw.mockResolvedValue([{ database: "postgres", port: 5432, address: "172.20.0.3" }]);
    const health = healthyApi();
    if (field === "policy") health.voice_barge_in.session_execution_policy = { ...policy, business_tools: "enabled" };
    if (field === "registration") health.voice_barge_in.acceptance_session_registered = false;
    if (field === "environment") health.voice_barge_in.environment_id = "other-env";
    if (field === "start") health.voice_barge_in.start_id = "other-start";
    if (field === "migration") health.voice_barge_in.migration_verification.status = "not_verified";
    if (field === "browser_url") health.voice_barge_in.browser_api_url = "http://127.0.0.1:9999";
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify(health), { status: 200 }));
    expect((await GET()).status).toBe(409);
  });

  it("rejects an E1 record outside the selected cache root", async () => {
    vi.stubEnv("HEXIS_VOICE_BARGE_IN_ENV", path.join(os.tmpdir(), "other", "environment.json"));
    const response = await GET();
    expect(response.status).toBe(403);
    expect(queryRaw).not.toHaveBeenCalled();
  });
});
