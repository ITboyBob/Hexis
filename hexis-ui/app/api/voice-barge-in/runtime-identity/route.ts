import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import path from "node:path";
import { NextResponse } from "next/server";
import { prisma } from "@/lib/prisma";

export const dynamic = "force-dynamic";

type EnvironmentRecord = {
  env_id?: string;
  status?: string;
  owned_resources?: { talk_host_port?: number };
  db_identity?: { talk?: { database?: string; server_port?: number } };
};

class ForbiddenEnvironmentPathError extends Error {}

function cacheRoot(): string {
  const cacheHome = process.env.XDG_CACHE_HOME || path.join(homedir(), ".cache");
  return path.resolve(cacheHome, "hexis");
}

function parseUrl(value: string | undefined, name: string): URL {
  if (!value) throw new Error(`${name} was not explicitly selected`);
  const parsed = new URL(value);
  if (!parsed.hostname || !["postgres:", "postgresql:"].includes(parsed.protocol) && name.includes("DATABASE")) {
    throw new Error(`${name} is not a valid PostgreSQL URL`);
  }
  if (name === "HEXIS_API_URL" && !["http:", "https:"].includes(parsed.protocol)) {
    throw new Error("HEXIS_API_URL must use HTTP or HTTPS");
  }
  return parsed;
}

async function readSelectedEnvironment(): Promise<{ path: string; record: EnvironmentRecord }> {
  const selectedPath = process.env.HEXIS_VOICE_BARGE_IN_ENV;
  if (!selectedPath) throw new Error("no E1 environment was explicitly selected");
  const resolvedPath = path.resolve(selectedPath);
  const relative = path.relative(cacheRoot(), resolvedPath);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new ForbiddenEnvironmentPathError("selected E1 record is outside the Hexis cache directory");
  }
  const parts = relative.split(path.sep);
  if (parts.length !== 4 || parts[0] !== "voice-barge-in" || parts[1] !== "environments" || parts[3] !== "environment.json") {
    throw new Error("selected E1 path does not match the explicit environment-record layout");
  }
  const record = JSON.parse(await readFile(resolvedPath, "utf8")) as EnvironmentRecord;
  if (record.env_id !== parts[2] || !/^[a-z0-9][a-z0-9-]{0,47}$/.test(record.env_id)) {
    throw new Error("selected E1 environment identity does not match its path");
  }
  if (record.status !== "ready" || record.owned_resources?.talk_host_port === undefined || !record.db_identity?.talk) {
    throw new Error("selected E1 talk database has not passed its identity check");
  }
  return { path: resolvedPath, record };
}

export async function GET() {
  if (!process.env.HEXIS_VOICE_BARGE_IN_ENV) {
    return NextResponse.json({ error: "Runtime identity is disabled until an E1 environment is explicitly selected." }, { status: 404 });
  }

  try {
    const { record } = await readSelectedEnvironment();
    const databaseUrlSource = process.env.HEXIS_DATABASE_URL ? "HEXIS_DATABASE_URL" : "DATABASE_URL";
    const databaseUrl = parseUrl(process.env.HEXIS_DATABASE_URL || process.env.DATABASE_URL, databaseUrlSource);
    const apiUrl = parseUrl(process.env.HEXIS_API_URL, "HEXIS_API_URL");
    const databaseName = decodeURIComponent(databaseUrl.pathname.replace(/^\//, ""));
    const expectedTalk = record.db_identity?.talk;
    const urlPort = Number(databaseUrl.port || (databaseUrl.protocol === "postgres:" || databaseUrl.protocol === "postgresql:" ? "5432" : ""));
    const expectedUrlPort = databaseUrl.hostname === "talk-db" ? 5432 : record.owned_resources?.talk_host_port;
    if (databaseName !== expectedTalk?.database || urlPort !== expectedUrlPort) {
      return NextResponse.json({ error: "Selected UI database URL does not match the owned talk database in E1." }, { status: 409 });
    }

    const databaseRows = await prisma.$queryRaw<Array<{ database: string; port: number; address: string | null }>>`
      SELECT current_database() AS database, inet_server_port() AS port, inet_server_addr()::text AS address
    `;
    const actualDatabase = databaseRows[0];
    if (!actualDatabase || actualDatabase.database !== expectedTalk?.database || Number(actualDatabase.port) !== Number(expectedTalk.server_port)) {
      return NextResponse.json({ error: "The UI's live database identity does not match the selected E1 talk database." }, { status: 409 });
    }

    const healthUrl = new URL("/health", apiUrl);
    const healthResponse = await fetch(healthUrl, { cache: "no-store", signal: AbortSignal.timeout(5000) });
    const health = await healthResponse.json().catch(() => null) as { status?: string; checks?: { db?: boolean } } | null;
    if (!healthResponse.ok || health?.status !== "ok" || health.checks?.db !== true) {
      return NextResponse.json({ error: "The selected API upstream is not healthy or has no database connection." }, { status: 503 });
    }

    return NextResponse.json({
      env_id: record.env_id,
      database: {
        database: actualDatabase.database,
        server_port: Number(actualDatabase.port),
        server_address: actualDatabase.address,
        url_source: databaseUrlSource,
        url_host: databaseUrl.hostname,
        url_port: urlPort,
      },
      api: {
        status: health.status,
        checks: health.checks,
        url_host: apiUrl.hostname,
        url_port: Number(apiUrl.port || (apiUrl.protocol === "https:" ? 443 : 80)),
      },
    });
  } catch (error) {
    if (error instanceof ForbiddenEnvironmentPathError) {
      return NextResponse.json({ error: error.message }, { status: 403 });
    }
    return NextResponse.json({ error: "Could not verify the selected runtime identity. Check the E1 record, explicit URLs, database, and API health." }, { status: 503 });
  }
}
