import {
  errorMessage,
  hexisApiHeaders,
  jsonProxyResponse,
  resolveHexisApiUrl,
} from "@/lib/python-api";

export const runtime = "nodejs";

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> }
): Promise<Response> {
  const { id } = await context.params;
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return Response.json({ error: "Selection confirmation must be JSON." }, { status: 400 });
  }
  try {
    const upstream = await fetch(
      resolveHexisApiUrl(`/api/m5/selections/${encodeURIComponent(id)}/confirm`),
      {
        method: "POST",
        headers: hexisApiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(body),
        cache: "no-store",
        signal: request.signal,
      }
    );
    return jsonProxyResponse(upstream, await upstream.text());
  } catch (error: unknown) {
    return Response.json(
      { error: `Selection confirmation failed: ${errorMessage(error, "API request failed")}` },
      { status: 503 }
    );
  }
}
