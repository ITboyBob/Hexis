import {
  errorMessage,
  hexisApiHeaders,
  resolveHexisApiUrl,
  sseError,
  sseProxyResponse,
} from "@/lib/python-api";

export const runtime = "nodejs";

export async function GET(request: Request): Promise<Response> {
  const url = resolveHexisApiUrl(
    "/api/m5/tasks/events",
    new URL(request.url).search
  );
  const lastEventId = request.headers.get("last-event-id");
  try {
    const upstream = await fetch(url, {
      headers: hexisApiHeaders({
        Accept: "text/event-stream",
        ...(lastEventId ? { "Last-Event-ID": lastEventId } : {}),
      }),
      cache: "no-store",
      signal: request.signal,
    });
    return sseProxyResponse(upstream);
  } catch (error: unknown) {
    return sseError(
      `Failed to reach Hexis API: ${errorMessage(error, "API request failed")}`,
      503
    );
  }
}
