import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Shell } from "./shell";

const route = vi.hoisted(() => ({ pathname: "/avatar-preview" }));
vi.mock("next/navigation", () => ({ usePathname: () => route.pathname }));

describe("Shell 对 Avatar 预览的精确隔离", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    route.pathname = "/avatar-preview";
  });

  it("预览路由不挂载Sidebar与状态请求，其他路由仍保留原导航", async () => {
    const fetchMock = vi.fn(async () => Response.json({ agent_name: "Agent" }));
    vi.stubGlobal("fetch", fetchMock);
    const view = render(<Shell><p>预览内容</p></Shell>);

    expect(screen.getByText("预览内容")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Primary navigation" })).not.toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(fetchMock).not.toHaveBeenCalled();

    route.pathname = "/settings";
    view.rerender(<Shell><p>设置内容</p></Shell>);
    expect(screen.getByRole("navigation", { name: "Primary navigation" })).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/status", { cache: "no-store" }));
  });

  it("预览子路由同样不渲染全局导航", () => {
    route.pathname = "/avatar-preview/diagnostics";
    render(<Shell><p>诊断内容</p></Shell>);
    expect(screen.getByText("诊断内容")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Primary navigation" })).not.toBeInTheDocument();
  });

  it("只匹配预览路由及其子路径", async () => {
    route.pathname = "/avatar-preview-other";
    const fetchMock = vi.fn(async () => Response.json({ agent_name: "Agent" }));
    vi.stubGlobal("fetch", fetchMock);
    render(<Shell><p>其他页面</p></Shell>);
    expect(screen.getByRole("navigation", { name: "Primary navigation" })).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/status", { cache: "no-store" }));
  });
});
