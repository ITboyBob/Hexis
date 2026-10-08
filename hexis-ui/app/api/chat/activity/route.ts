import { NextResponse } from "next/server";

import { errorMessage, hexisApiHeaders, jsonProxyResponse, resolveHexisApiUrl } from "@/lib/python-api";

export const runtime = "nodejs";

export async function GET(request: Request): Promise<Response> {
  const search = new URL(request.url).search;
  const url = resolveHexisApiUrl("/api/chat/activity", search);
  try {
    const upstream = await fetch(url, {
      method: "GET",
      headers: hexisApiHeaders({ Accept: "application/json" }),
      signal: request.signal,
      cache: "no-store",
    });
    const body = await upstream.text();
    return jsonProxyResponse(upstream, body);
  } catch (error: unknown) {
    return NextResponse.json(
      { error: `Failed to reach Hexis API at ${url}: ${errorMessage(error, "Unknown error")}` },
      { status: 502 },
    );
  }
}
