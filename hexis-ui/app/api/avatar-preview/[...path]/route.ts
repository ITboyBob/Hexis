export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type Context = { params: Promise<{ path: string[] }> };
const evaluationPath = /^evaluations\/[0-9a-f-]{36}\/[0-9a-f-]{36}\/\d+$/i;

function error(message: string, status: number): Response {
  return Response.json({ message }, { status });
}

async function proxy(request: Request, context: Context): Promise<Response> {
  const { path } = await context.params;
  const endpoint = path.join("/");
  const allowed = request.method === "POST" ? endpoint === "evaluate"
    : endpoint === "status" || evaluationPath.test(endpoint);
  if (!allowed) return error("预览接口不存在", 404);

  const configured = process.env.HEXIS_AVATAR_PREVIEW_API_URL;
  if (!configured?.trim()) return error("未选择情绪预览服务；可继续离线模式，或配置预览服务地址", 503);
  let base: URL;
  try {
    base = new URL(configured);
    if (base.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(base.hostname)
      || base.username || base.password || base.search || base.hash) throw new Error("invalid loopback URL");
  } catch {
    return error("预览服务地址必须是显式的loopback HTTP地址，且不能包含用户信息、查询或片段", 503);
  }

  const url = new URL(endpoint, `${base.toString().replace(/\/+$/, "")}/`);
  try {
    const upstream = await fetch(url, {
      method: request.method,
      headers: { Accept: "application/json", ...(request.method === "POST" ? { "Content-Type": "application/json" } : {}) },
      body: request.method === "POST" ? await request.text() : undefined,
      signal: request.signal,
      cache: "no-store",
      redirect: "error",
    });
    const headers = new Headers();
    const contentType = upstream.headers.get("content-type");
    if (contentType) headers.set("content-type", contentType);
    headers.set("cache-control", "no-store");
    return new Response(upstream.body, { status: upstream.status, headers });
  } catch {
    return error("预览服务暂不可用；请检查所选地址和服务状态，再查询原输入的评估记录", 502);
  }
}

export const GET = proxy;
export const POST = proxy;
