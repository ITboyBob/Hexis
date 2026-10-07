import {
  errorMessage,
  hexisApiHeaders,
  jsonProxyResponse,
  resolveHexisApiUrl,
} from "@/lib/python-api";

export const runtime = "nodejs";

export async function GET(request: Request): Promise<Response> {
  const url = resolveHexisApiUrl("/api/m5/tasks", new URL(request.url).search);
  try {
    const upstream = await fetch(url, {
      headers: hexisApiHeaders({ Accept: "application/json" }),
      cache: "no-store",
      signal: request.signal,
    });
    return jsonProxyResponse(upstream, await upstream.text());
  } catch (error: unknown) {
    return Response.json(
      { error: `Task state is temporarily unavailable: ${errorMessage(error, "API request failed")}` },
      { status: 503 }
    );
  }
}
