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
let apiStartPath = "";
let apiStart: Record<string, unknown>;

function healthyApi() {
  return { status: "ok", checks: { db: true }, voice_barge_in: {
    environment_id: "vb-test", database: "postgres", start_id: "start-test",
    acceptance_session_id: "11111111-1111-4111-8111-111111111111",
    acceptance_session_registered: true, session_execution_policy: policy,
    migration_verification: { status: "verified_read_only", mode: "committed_applied_only" },
    browser_api_url: browserApiUrl,
    api_url_source: String(apiStart.api_url_source),
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
  apiStartPath = path.join(cacheHome, "hexis", "voice-barge-in", "api-runs", "api-start.json");
  apiStart = { environment_id: "vb-test", start_id: "start-test", status: "started", pid: 123,
    browser_api_url: browserApiUrl, api_url_source: "C6_api_url", ui_origin: "http://127.0.0.1:3482",
    endpoints: { api_host: "127.0.0.1", api_port: Number(new URL(browserApiUrl).port) } };
  await mkdir(path.dirname(apiStartPath), { recursive: true });
  await writeFile(apiStartPath, JSON.stringify(apiStart));
  await mkdir(path.dirname(envRecordPath), { recursive: true });
  await writeFile(envRecordPath, JSON.stringify({
    schema_version: 1,
    env_id: "vb-test",
    status: "ready",
    repository_root: repositoryRoot,
    owned_resources: { project_name: "hexis-vb-test", talk_host_port: 43852, api_process: {
      start_id: "start-test", pid: 123, status: "healthy", api_url: browserApiUrl, start_record_path: apiStartPath } },
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

  it("reads an owned nested cache and the explicit startup-selected API port", async () => {
    browserApiUrl = "http://127.0.0.1:43877";
    const nested = path.join(cacheHome, "hexis", "worktree-merge", "bob105", "voice-barge-in");
    const record = JSON.parse(await readFile(envRecordPath, "utf8"));
    envRecordPath = path.join(nested, "environments", "vb-test", "environment.json");
    apiStartPath = path.join(nested, "api-runs", "api-start.json");
    await mkdir(path.dirname(envRecordPath), { recursive: true });
    await mkdir(path.dirname(apiStartPath), { recursive: true });
    apiStart = { ...apiStart, browser_api_url: browserApiUrl, api_url_source: "start_api_cli",
      endpoints: { api_host: "127.0.0.1", api_port: 43877 } };
    record.owned_resources.api_process.api_url = browserApiUrl;
    record.owned_resources.api_process.start_record_path = apiStartPath;
    record.db_identity.talk.server_address = "172.20.0.3/32";
    await writeFile(envRecordPath, JSON.stringify(record));
    await writeFile(apiStartPath, JSON.stringify(apiStart));
    vi.stubEnv("HEXIS_VOICE_BARGE_IN_ENV", envRecordPath);
    vi.stubEnv("HEXIS_API_URL", browserApiUrl);
    vi.stubEnv("HEXIS_BROWSER_API_URL", browserApiUrl);
    vi.stubEnv("HEXIS_DATABASE_URL", "postgresql://new%3Arole:secret@127.0.0.1:43852/postgres");
    queryRaw.mockResolvedValue([{ database: "postgres", port: 5432, address: "172.20.0.3" }]);
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify(healthyApi()), { status: 200 }));
    const response = await GET();
    expect(response.status).toBe(200);
    expect(fetch).toHaveBeenCalledWith(new URL("http://127.0.0.1:43877/health"), expect.any(Object));
    expect(await response.json()).toMatchObject({ database: { url_port: 43852 }, api: { url_port: 43877 } });
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

  it.each(["source", "port", "host", "pid", "outside_start", "upstream_port"])("rejects unbound startup %s before database or API access", async (field) => {
    if (field === "source") apiStart.api_url_source = "ambient_env";
    if (field === "port") apiStart.endpoints = { api_host: "127.0.0.1", api_port: 9999 };
    if (field === "host") apiStart.endpoints = { api_host: "0.0.0.0", api_port: 43817 };
    if (field === "pid") apiStart.pid = 999;
    if (field === "upstream_port") vi.stubEnv("HEXIS_API_URL", "http://127.0.0.1:9999");
    if (field === "outside_start") {
      const record = JSON.parse(await readFile(envRecordPath, "utf8"));
      record.owned_resources.api_process.start_record_path = path.join(cacheHome, "unbound-api-start.json");
      await writeFile(envRecordPath, JSON.stringify(record));
    }
    await writeFile(apiStartPath, JSON.stringify(apiStart));
    expect((await GET()).status).toBe(409);
    expect(queryRaw).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("rejects a live database server address different from the selected cloned database", async () => {
    const record = JSON.parse(await readFile(envRecordPath, "utf8"));
    record.db_identity.talk.server_address = "172.20.0.3/32";
    await writeFile(envRecordPath, JSON.stringify(record));
    queryRaw.mockResolvedValue([{ database: "postgres", port: 5432, address: "172.20.0.4" }]);
    expect((await GET()).status).toBe(409);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("rejects an E1 record outside the selected cache root", async () => {
    vi.stubEnv("HEXIS_VOICE_BARGE_IN_ENV", path.join(os.tmpdir(), "other", "environment.json"));
    const response = await GET();
    expect(response.status).toBe(403);
    expect(queryRaw).not.toHaveBeenCalled();
  });
});
