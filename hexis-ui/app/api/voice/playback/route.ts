import {
  errorMessage,
  hexisApiHeaders,
  jsonProxyResponse,
  resolveHexisApiUrl,
} from "@/lib/python-api";

export const runtime = "nodejs";

export async function POST(request: Request): Promise<Response> {
  try {
    const upstream = await fetch(resolveHexisApiUrl("/api/voice/playback"), {
      method: "POST",
      cache: "no-store",
      headers: {
        ...hexisApiHeaders(),
        "Content-Type": "application/json",
      },
      body: await request.text(),
    });
    return jsonProxyResponse(upstream, await upstream.text());
  } catch (error: unknown) {
    return Response.json(
      {
        error: errorMessage(
          error,
          "Playback evidence could not reach the Hexis API. Keep playback pending and retry.",
        ),
      },
      { status: 502 },
    );
  }
}
