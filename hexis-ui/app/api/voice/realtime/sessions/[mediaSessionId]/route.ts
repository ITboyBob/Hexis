import { errorMessage, hexisApiHeaders, jsonProxyResponse, resolveHexisApiUrl } from "@/lib/python-api";

export const runtime = "nodejs";

export async function DELETE(request: Request, context: { params: Promise<{ mediaSessionId: string }> }): Promise<Response> {
  try {
    const { mediaSessionId } = await context.params;
    const result = await fetch(resolveHexisApiUrl(`/api/voice/realtime/sessions/${encodeURIComponent(mediaSessionId)}`), {
      method: "DELETE", cache: "no-store", headers: hexisApiHeaders(), signal: request.signal,
    });
    return jsonProxyResponse(result, await result.text());
  } catch (error) {
    return Response.json({ error: errorMessage(error, "RTC session close could not reach the local API.") }, { status: 502 });
  }
}
