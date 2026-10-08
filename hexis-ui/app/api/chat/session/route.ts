import { NextResponse } from "next/server";

import { normalizeJsonValue } from "@/lib/db";
import { prisma } from "@/lib/prisma";

export const runtime = "nodejs";

type Row = { session: unknown };

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Failed to create chat session";
}

export async function POST(): Promise<Response> {
  try {
    if (process.env.HEXIS_VOICE_BARGE_IN_ENV) {
      if (!process.env.HEXIS_API_URL) throw new Error("The owned M6 API upstream was not explicitly selected.");
      const response = await fetch(new URL("/api/voice-barge-in/chat-session", process.env.HEXIS_API_URL), {
        method: "POST", cache: "no-store", signal: AbortSignal.timeout(5000),
      });
      const payload = asRecord(await response.json());
      if (!response.ok) return NextResponse.json({ error: payload.detail || payload.error || "Could not register the owned M6 acceptance session." }, { status: response.status });
      if (typeof payload.session_id !== "string" || typeof payload.api_start_id !== "string") {
        throw new Error("The selected M6 API returned no registered session identity.");
      }
      return NextResponse.json(payload);
    }
    const rows = await prisma.$queryRawUnsafe<Row[]>(
      "SELECT get_or_create_chat_session(NULL::uuid, $1::text, NULL::text, $2::jsonb) AS session",
      "web",
      JSON.stringify({
        source: "web",
        created_by: "user",
        created_at: new Date().toISOString(),
      })
    );
    const session = asRecord(normalizeJsonValue(rows[0]?.session));
    const sessionId = typeof session.session_id === "string" ? session.session_id : null;
    if (!sessionId) {
      return NextResponse.json({ error: "database did not return a session id" }, { status: 500 });
    }
    return NextResponse.json({ ...session, session_id: sessionId });
  } catch (error: unknown) {
    console.error("Chat session create API error:", error);
    return NextResponse.json({ error: errorMessage(error) }, { status: 500 });
  }
}
