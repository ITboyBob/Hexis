---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — hexis-ui/app/components/

## Summary
面板共享 UI 组件：`nav/` 提供导航外壳与侧栏，`pwa/` 提供 Service Worker 注册与推送设置，`ui/` 是一组无业务的基础控件（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解页面外壳与导航装配 | Navigation | `nav/shell.tsx` | `nav/sidebar.tsx`, `../layout.tsx` |
| 理解侧栏导航项与路由分组 | Navigation | `nav/sidebar.tsx` | `nav/shell.tsx` |
| 理解 PWA 客户端注册与安装提示 | PWA | `pwa/pwa-client.tsx` | `lib/pwa-client.ts` |
| 理解推送订阅设置界面 | PWA | `pwa/pwa-settings.tsx` | `../api/pwa/push/subscriptions/route.ts` |
| 查找基础 UI 控件（按钮/卡片/徽章/进度条等） | Base UI | `ui/button.tsx` | `ui/card.tsx`, `ui/badge.tsx`, `ui/page-header.tsx` |

## Subdirectories
| Dir | Domain | Depends On | Purpose |
|---|---|---|---|
| `nav/` | Navigation | `lib/pwa-client.ts` | 根布局外壳与侧栏导航。 |
| `pwa/` | PWA | `lib/pwa-client.ts` | PWA 客户端注册与推送设置。 |
| `ui/` | Base UI | `—` | 无业务基础控件。 |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `Shell` | `nav/shell.tsx` | L:8 |
| `Sidebar` | `nav/sidebar.tsx` | L:60 |
| `PwaClient` | `pwa/pwa-client.tsx` | L:19 |
| `PwaSettings` | `pwa/pwa-settings.tsx` | L:27 |
| `Badge` | `ui/badge.tsx` | — |
| `Button` | `ui/button.tsx` | — |
| `Card` | `ui/card.tsx` | — |
| `Input` | `ui/input.tsx` | — |
| `PageHeader` | `ui/page-header.tsx` | — |
| `ProgressBar` | `ui/progress-bar.tsx` | — |
| `Spinner` | `ui/spinner.tsx` | — |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `nav/shell.tsx` | Navigation | ← `pwa/pwa-client.tsx` | 页面外壳：装配导航侧栏与内容区。 |
| `nav/sidebar.tsx` | Navigation | ← — | 侧栏导航项与路由分组。 |
| `pwa/pwa-client.tsx` | PWA | ← `lib/pwa-client.ts` | 注册 Service Worker、安装提示与推送订阅引导。 |
| `pwa/pwa-settings.tsx` | PWA | ← `lib/pwa-client.ts` | 推送订阅设置界面。 |
| `ui/badge.tsx` | Base UI | ← — | 徽章控件。 |
| `ui/button.tsx` | Base UI | ← — | 按钮控件。 |
| `ui/card.tsx` | Base UI | ← — | 卡片容器。 |
| `ui/input.tsx` | Base UI | ← — | 输入框控件。 |
| `ui/page-header.tsx` | Base UI | ← — | 页面标题头。 |
| `ui/progress-bar.tsx` | Base UI | ← — | 进度条。 |
| `ui/spinner.tsx` | Base UI | ← — | 加载指示器。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `nav/shell.tsx` | `pwa/pwa-client.tsx` | `../layout.tsx` |
| `pwa/pwa-client.tsx` | — | `nav/shell.tsx` |
| `pwa/pwa-settings.tsx` | — | — |
| `ui/*` | — | 各页面路由 |
