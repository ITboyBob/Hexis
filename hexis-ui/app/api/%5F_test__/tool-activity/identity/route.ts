import { NextResponse } from "next/server";

import { prisma } from "@/lib/prisma";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type IdentityRow = {
  database: string;
  address: string | null;
  port: number;
  system_identifier: string;
};

export async function GET(): Promise<Response> {
  if (process.env.BOB108_RUNTIME_DIAGNOSTICS !== "1") {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }
  try {
    const rows = await prisma.$queryRawUnsafe<IdentityRow[]>(
      "SELECT current_database() AS database, inet_server_addr()::text AS address, inet_server_port() AS port, (SELECT system_identifier FROM pg_control_system())::text AS system_identifier",
    );
    const identity = rows[0];
    if (!identity) return NextResponse.json({ error: "Database identity query returned no row." }, { status: 503 });
    if (
      identity.database !== process.env.BOB108_EXPECTED_DATABASE ||
      identity.system_identifier !== process.env.BOB108_EXPECTED_SYSTEM_IDENTIFIER
    ) {
      return NextResponse.json({ error: "UI database identity does not match the owned BOB-108 runtime." }, { status: 409 });
    }
    return NextResponse.json({ run_id: process.env.BOB108_RUN_ID, database_identity: identity });
  } catch (error: unknown) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Could not inspect the UI database identity." },
      { status: 503 },
    );
  }
}
