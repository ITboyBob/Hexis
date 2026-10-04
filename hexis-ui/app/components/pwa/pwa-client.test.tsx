import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PwaClient } from "./pwa-client";

const route = vi.hoisted(() => ({ pathname: "/avatar-preview" }));
const pwa = vi.hoisted(() => ({
  register: vi.fn(async () => undefined),
  deviceId: vi.fn(() => "device"),
  promptAvailable: vi.fn(() => false),
}));
vi.mock("next/navigation", () => ({ usePathname: () => route.pathname }));
vi.mock("@/lib/pwa-client", () => ({
  displayMode: () => "browser",
  enablePushNotifications: vi.fn(),
  installPromptAvailable: pwa.promptAvailable,
  isInstalled: () => false,
  pwaDeviceId: pwa.deviceId,
  promptInstall: vi.fn(),
  registerHexisServiceWorker: pwa.register,
  rememberInstallPrompt: vi.fn(),
}));

describe("PWA 全局副作用对 Avatar 预览的路由隔离", () => {
  const originalSecureContext = Object.getOwnPropertyDescriptor(window, "isSecureContext");
  const originalServiceWorker = Object.getOwnPropertyDescriptor(navigator, "serviceWorker");

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    if (originalSecureContext) Object.defineProperty(window, "isSecureContext", originalSecureContext);
    else Reflect.deleteProperty(window, "isSecureContext");
    if (originalServiceWorker) Object.defineProperty(navigator, "serviceWorker", originalServiceWorker);
    else Reflect.deleteProperty(navigator, "serviceWorker");
    route.pathname = "/avatar-preview";
    pwa.register.mockClear();
    pwa.deviceId.mockClear();
    pwa.promptAvailable.mockReset().mockReturnValue(false);
  });

  it("预览及子路由不注册service worker、不发送presence，普通页面仍照常运行", async () => {
    const fetchMock = vi.fn(async () => Response.json({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
    Object.defineProperty(navigator, "serviceWorker", { configurable: true, value: {} });
    const view = render(<PwaClient />);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(pwa.deviceId).not.toHaveBeenCalled();
    expect(pwa.register).not.toHaveBeenCalled();

    route.pathname = "/avatar-preview/diagnostics";
    view.rerender(<PwaClient />);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(pwa.deviceId).not.toHaveBeenCalled();

    route.pathname = "/settings";
    view.rerender(<PwaClient />);
    expect(pwa.deviceId).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith("/api/pwa/presence", expect.objectContaining({ method: "POST" }));
    expect(pwa.register).toHaveBeenCalledTimes(1);
  });

  it("从普通页面进入预览会清理定时器/listener并隐藏已有安装提示", () => {
    route.pathname = "/settings";
    const fetchMock = vi.fn(async () => Response.json({ ok: true }));
    const intervalSpy = vi.spyOn(window, "setInterval");
    const clearIntervalSpy = vi.spyOn(window, "clearInterval");
    vi.stubGlobal("fetch", fetchMock);
    pwa.promptAvailable.mockReturnValue(true);
    const view = render(<PwaClient />);
    expect(view.getByLabelText("Install Hexis app")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    route.pathname = "/avatar-preview";
    view.rerender(<PwaClient />);
    expect(view.queryByLabelText("Install Hexis app")).not.toBeInTheDocument();
    expect(clearIntervalSpy).toHaveBeenCalledWith(intervalSpy.mock.results[0].value);
    const count = fetchMock.mock.calls.length;
    window.dispatchEvent(new Event("pagehide"));
    document.dispatchEvent(new Event("visibilitychange"));
    expect(fetchMock).toHaveBeenCalledTimes(count);
  });
});
