import { afterEach, describe, expect, it, vi } from "vitest";
import { GET, POST } from "./route";

const context = (path: string[]) => ({ params: Promise.resolve({ path }) });

describe("Avatar 预览服务代理", () => {
  afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });

  it("未配置时提示可继续离线使用", async () => {
    vi.stubEnv("HEXIS_AVATAR_PREVIEW_API_URL", "");
    const response = await POST(new Request("http://ui/api/avatar-preview/evaluate", { method: "POST", body: "{}" }), context(["evaluate"]));
    expect(response.status).toBe(503);
    expect(await response.json()).toMatchObject({ message: expect.stringContaining("离线") });
  });

  it.each(["https://127.0.0.1:9000", "http://example.com", "http://127.0.0.1.evil.invalid:9"]) (
    "拒绝非loopback配置 %s", async (url) => {
      vi.stubEnv("HEXIS_AVATAR_PREVIEW_API_URL", url);
      const response = await GET(new Request("http://ui/api/avatar-preview/status"), context(["status"]));
      expect(response.status).toBe(503);
    });

  it.each(["http://user:password@127.0.0.1:9911", "http://127.0.0.1:9911?x=1", "http://127.0.0.1:9911/#fragment"]) (
    "拒绝带隐式路由数据的配置 %s", async (url) => {
      vi.stubEnv("HEXIS_AVATAR_PREVIEW_API_URL", url);
      const response = await GET(new Request("http://ui/api/avatar-preview/status"), context(["status"]));
      expect(response.status).toBe(503);
    });

  it("仅转发白名单路径和浏览器消息，不转发 ambient key/header", async () => {
    vi.stubEnv("HEXIS_AVATAR_PREVIEW_API_URL", "http://127.0.0.1:9911");
    const fetcher = vi.fn().mockResolvedValue(Response.json({ status: "valid" }));
    vi.stubGlobal("fetch", fetcher);
    const body = JSON.stringify({ identity: {}, message: "hi", context_source: "fixture", api_key: "must-not-forward" });
    const request = new Request("http://ui/api/avatar-preview/evaluate", { method: "POST", body, headers: { authorization: "Bearer ambient" } });
    const response = await POST(request, context(["evaluate"]));
    expect(response.status).toBe(200);
    expect(fetcher.mock.calls[0][0].toString()).toBe("http://127.0.0.1:9911/evaluate");
    expect(fetcher.mock.calls[0][1].body).toBe(body);
    expect(fetcher.mock.calls[0][1].headers).toEqual({ "Content-Type": "application/json", Accept: "application/json" });
    expect(fetcher.mock.calls[0][1].redirect).toBe("error");
  });

  it("上游重定向不会把请求导出loopback", async () => {
    vi.stubEnv("HEXIS_AVATAR_PREVIEW_API_URL", "http://127.0.0.1:9911");
    const fetcher = vi.fn(async (_url: URL, options: RequestInit) => {
      expect(options.redirect).toBe("error");
      throw new TypeError("redirect mode error");
    });
    vi.stubGlobal("fetch", fetcher);
    const response = await GET(new Request("http://ui/api/avatar-preview/status"), context(["status"]));
    expect(response.status).toBe(502);
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it("拒绝不在接口白名单的路径", async () => {
    vi.stubEnv("HEXIS_AVATAR_PREVIEW_API_URL", "http://127.0.0.1:9911");
    const response = await GET(new Request("http://ui/api/avatar-preview/anything"), context(["anything"]));
    expect(response.status).toBe(404);
  });
});
