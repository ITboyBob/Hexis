// @vitest-environment node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { DevAppPageRouteMatcherProvider } from "next/dist/server/route-matcher-providers/dev/dev-app-page-route-matcher-provider";
import { DevAppRouteRouteMatcherProvider } from "next/dist/server/route-matcher-providers/dev/dev-app-route-route-matcher-provider";

const appDir = fileURLToPath(new URL("../app", import.meta.url));

function routeFiles(directory: string): string[] {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const filename = path.join(directory, entry.name);
    return entry.isDirectory() ? routeFiles(filename) : [filename];
  });
}

describe("受控测试页面的 Next 路由注册", () => {
  it("将转义目录注册为保持原 URL 的页面与 API", async () => {
    const reader = { read: () => routeFiles(appDir) };
    const pages = new DevAppPageRouteMatcherProvider(appDir, ["ts", "tsx"], reader, false);
    const routes = new DevAppRouteRouteMatcherProvider(appDir, ["ts", "tsx"], reader, false);
    const pageMatchers = await pages.matchers();
    const apiMatchers = await routes.matchers();
    expect(pageMatchers.some((matcher) => matcher.match("/__test__/tool-activity"))).toBe(true);
    expect(apiMatchers.some((matcher) => matcher.match("/api/__test__/tool-activity/identity"))).toBe(true);
    expect(apiMatchers.some((matcher) => matcher.match("/api/__test__/tool-activity/script"))).toBe(true);
    expect(apiMatchers.some((matcher) => matcher.match("/api/__test__/tool-activity/voice-recall"))).toBe(true);
    expect(apiMatchers.some((matcher) => matcher.match("/api/__test__/tool-activity/background/start"))).toBe(true);
    expect(apiMatchers.some((matcher) => matcher.match("/api/__test__/tool-activity/background/status"))).toBe(true);
    expect(apiMatchers.some((matcher) => matcher.match("/api/__test__/tool-activity/background/release"))).toBe(true);
  });

  it("拒绝未转义的私有目录，防止处理函数单测掩盖路由 404", async () => {
    const reader = { read: () => [path.join(appDir, "api/__test__/tool-activity/identity/route.ts")] };
    const routes = new DevAppRouteRouteMatcherProvider(appDir, ["ts", "tsx"], reader, false);
    expect(await routes.matchers()).toHaveLength(0);
  });
});
