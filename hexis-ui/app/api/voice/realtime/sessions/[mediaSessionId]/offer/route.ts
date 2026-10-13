import { errorMessage, hexisApiHeaders, resolveHexisApiUrl } from "@/lib/python-api";

export const runtime = "nodejs";

export async function POST(request: Request, context: { params: Promise<{ mediaSessionId: string }> }): Promise<Response> {
  try {
    const { mediaSessionId } = await context.params;
    const result = await fetch(resolveHexisApiUrl(`/api/voice/realtime/sessions/${encodeURIComponent(mediaSessionId)}/offer`), {
      method: "POST", cache: "no-store", headers: hexisApiHeaders({ "Content-Type": request.headers.get("Content-Type") || "application/sdp" }),
      body: await request.text(), signal: request.signal,
    });
    return new Response(await result.text(), { status: result.status,
      headers: { "Content-Type": result.headers.get("Content-Type") || "application/sdp" } });
  } catch (error) {
    return Response.json({ error: errorMessage(error, "RTC signaling could not reach the local API. Retry Talk.") }, { status: 502 });
  }
}
