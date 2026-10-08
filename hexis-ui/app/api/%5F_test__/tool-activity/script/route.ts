import { NextResponse } from "next/server";

export const runtime = "nodejs";

export async function POST(request: Request): Promise<Response> {
  if (process.env.BOB108_CONTROLLED_RUNTIME !== "1") {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }
  const providerUrl = process.env.BOB108_SCRIPTED_PROVIDER_URL;
  if (!providerUrl) return NextResponse.json({ error: "Scripted provider is not configured." }, { status: 503 });
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "A JSON provider script is required." }, { status: 400 });
  }
  try {
    const response = await fetch(new URL("/__control/script", providerUrl), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
      signal: request.signal,
    });
    const payload = await response.text();
    return new Response(payload, {
      status: response.status,
      headers: { "Content-Type": response.headers.get("content-type") || "application/json" },
    });
  } catch (error: unknown) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Scripted provider is unavailable." },
      { status: 502 },
    );
  }
}
