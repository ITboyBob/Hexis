import { NextResponse } from "next/server";

export const runtime = "nodejs";

export async function POST(request: Request): Promise<Response> {
  if (process.env.BOB108_CONTROLLED_RUNTIME !== "1") {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }
  const apiUrl = process.env.HEXIS_API_URL;
  if (!apiUrl) return NextResponse.json({ error: "The controlled API is not configured." }, { status: 503 });
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "A controlled barrier identity is required." }, { status: 400 });
  }
  try {
    const response = await fetch(new URL("/__test__/tool-activity/background/release", apiUrl), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
      signal: request.signal,
    });
    return new Response(await response.text(), {
      status: response.status,
      headers: { "Content-Type": response.headers.get("content-type") || "application/json" },
    });
  } catch (error: unknown) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "The controlled background release endpoint is unavailable." },
      { status: 502 },
    );
  }
}
