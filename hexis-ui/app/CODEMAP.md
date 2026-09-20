---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — hexis-ui/app/

## Summary
Next.js App Router 根：承载所有页面路由（面板各功能页与初始化向导）、API 路由层（Prisma 直连或代理 Python API）、共享 UI 组件与 PWA 资源（inferred：`components/` 与 `hooks/` 目前由少数页面复用）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解仪表盘首页的状态/用量/心跳触发 | Dashboard Page | `page.tsx` | `hooks/use-gateway-events.ts` |
| 理解聊天页 SSE 流式渲染与收件箱决策 | Chat Page | `chat/page.tsx` | `chat/page.analysis.md`, `api/chat/route.ts` |
| 理解初始化向导的分阶段流程 | Init Page | `init/page.tsx` | `init/page.analysis.md`, `api/CODEMAP.md` |
| 理解集成/连接器状态与动作页 | Connections Page | `connections/page.tsx` | `connections/status.ts`, `app/api/integrations/status/route.ts` |
| 理解记忆浏览与三维图谱 | Memories Page | `memories/page.tsx` | `memories/memory-graph.tsx` |
| 理解记忆变更历史页 | Memory History Page | `memory-history/page.tsx` | `app/api/memories/history/route.ts` |
| 理解目标管理页 | Goals Page | `goals/page.tsx` | `app/api/goals/route.ts` |
| 理解职责（responsibilities）页 | Responsibilities Page | `responsibilities/page.tsx` | `app/api/responsibilities/route.ts` |
| 理解用户模型 claims 复核页 | User Model Page | `user-model/page.tsx` | `app/api/user-model/claims/route.ts` |
| 理解学习复核（skills proposals）页 | Learning Review Page | `learning-review/page.tsx` | `app/api/learning-review/route.ts` |
| 理解矛盾检测与决策页 | Contradictions Page | `contradictions/page.tsx` | `app/api/contradictions/route.ts` |
| 理解遗忘/保留（retention）页 | Forgetting Page | `forgetting/page.tsx` | `app/api/retention/route.ts` |
| 理解外发台账页 | Outbound Page | `outbound/page.tsx` | `app/api/outbound/route.ts` |
| 理解文档与归档柜（desk）页 | Documents Page | `documents/page.tsx` | `app/api/documents/route.ts`, `app/api/desk/route.ts` |
| 理解 RecMem 桌面页 | Desk Page | `desk/page.tsx` | `app/api/desk/route.ts` |
| 理解摄取（ingest）作业页 | Ingest Page | `ingest/page.tsx` | `ingest/jobs.ts`, `app/api/ingest/jobs/route.ts` |
| 理解设置页（工具、语音输入/输出） | Settings Page | `settings/page.tsx` | `app/api/settings/route.ts` |
| 理解页面布局、字体与全局样式装配 | App Shell | `layout.tsx` | `components/nav/shell.tsx`, `globals.css` |
| 理解 PWA manifest 与动态图标 | PWA Manifest | `manifest.ts` | `pwa-icon/[size]/route.tsx` |
| 理解 API 路由如何区分 Prisma 直连与 Python 代理 | API Routes | `api/CODEMAP.md` | `lib/prisma.ts`, `lib/python-api.ts` |
| 理解共享 UI 组件与导航侧栏 | Shared UI | `components/CODEMAP.md` | `components/nav/sidebar.tsx` |
| 理解运行时状态刷新钩子 | App Hooks | `hooks/use-gateway-events.ts` | `page.tsx` |

## Subdirectories
| Dir | Domain | Depends On | Purpose |
|---|---|---|---|
| `api/` | API Routes | `lib/prisma.ts`, `lib/python-api.ts` | 82 个 Next.js Route Handlers（init/chat/ingest/memories/goals/settings/voice/PWA 等），分别直连 Postgres 或代理 Python API → 见 `api/CODEMAP.md`。 |
| `chat/` | Chat Page | `lib/pwa-client.ts`, `app/api/chat/route.ts` | 聊天主页面 `page.tsx` 及其附件卡、消息展示、TalkMode、语音录制子组件。 |
| `components/` | Shared UI | `lib/pwa-client.ts` | 导航外壳/侧栏、PWA 客户端与设置、基础 UI → 见 `components/CODEMAP.md`。 |
| `connections/` | Connections Page | `app/api/integrations/status/route.ts` | 集成连接器状态页与 `status.ts` 汇总逻辑。 |
| `contradictions/` | Contradictions Page | `app/api/contradictions/route.ts` | 矛盾检测列表与决策页。 |
| `desk/` | Desk Page | `app/api/desk/route.ts` | RecMem 桌面（置顶/清空/打开）页。 |
| `documents/` | Documents Page | `app/api/documents/route.ts` | 文档列表与详情浏览页。 |
| `forgetting/` | Forgetting Page | `app/api/retention/route.ts` | 遗忘/保留策略状态与决策页。 |
| `goals/` | Goals Page | `app/api/goals/route.ts` | 目标创建/更新/完成页。 |
| `hooks/` | App Hooks | `—` | 仅含 `use-gateway-events.ts`，轮询刷新运行时状态而不持有 DB SSE 连接。 |
| `ingest/` | Ingest Page | `app/api/ingest/jobs/route.ts` | 摄取页与 `jobs.ts` 作业归一化/合并逻辑。 |
| `init/` | Init Page | `app/api/init/**`, `lib/character-catalog.ts` | 初始化向导页与 consent 交换、OAuth 控件子组件。 |
| `learning-review/` | Learning Review Page | `app/api/learning-review/route.ts` | 技能/学习提案复核页。 |
| `memories/` | Memories Page | `app/api/memories/route.ts` | 记忆检索页与 `memory-graph.tsx` Three.js 图谱。 |
| `memory-history/` | Memory History Page | `app/api/memories/history/route.ts` | 记忆变更历史时间线页。 |
| `outbound/` | Outbound Page | `app/api/outbound/route.ts` | 外发消息台账与控制页。 |
| `pwa-icon/` | PWA Manifest | `—` | `[size]/route.tsx` 用 next/og 动态生成 180/192/512 图标。 |
| `responsibilities/` | Responsibilities Page | `app/api/responsibilities/route.ts` | 环境职责列表、详情与动作页。 |
| `settings/` | Settings Page | `app/api/settings/route.ts` | 设置页（工具开关、语音输入/输出、搜索 provider）。 |
| `user-model/` | User Model Page | `app/api/user-model/claims/route.ts` | 用户模型 claims 查询与复核页。 |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `Shell` | `components/nav/shell.tsx` | L:8 |
| `Sidebar` | `components/nav/sidebar.tsx` | L:60 |
| `useGatewayEvents` | `hooks/use-gateway-events.ts` | L:6 |
| `PwaClient` | `components/pwa/pwa-client.tsx` | L:19 |
| `PwaSettings` | `components/pwa/pwa-settings.tsx` | L:27 |
| `manifest` | `manifest.ts` | L:8 |
| `MemoryGraph` | `memories/memory-graph.tsx` | L:84 |
| `normalizeIngestJob` | `ingest/jobs.ts` | L:43 |
| `summarizeConnectors` | `connections/status.ts` | L:134 |
| `ChatPage` | `chat/page.tsx` | L:970 |
| `Home` | `init/page.tsx` | L:518 |
| `MessagePresentationView` | `chat/message-presentation.tsx` | L:85 |
| `AttachmentCard` | `chat/attachment-card.tsx` | L:156 |
| `OAuthControl` | `init/oauth-control.tsx` | L:93 |
| `ConsentExchangeView` | `init/consent-exchange.tsx` | L:16 |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `favicon.ico` | Static Asset | ← `—` | 站点图标（二进制，不展开）。 |
| `globals.css` | App Shell | ← `—` | 全局 CSS 变量与 Tailwind 基础样式，配色与 CLI 主题对齐。 |
| `layout.tsx` | App Shell | ← `components/nav/shell.tsx`, `components/pwa/pwa-client.tsx` | 根布局：字体变量、元数据与导航/PWA 外壳。 |
| `manifest.ts` | PWA Manifest | ← `—` | PWA manifest（display_override、edge_side_panel、图标指向动态路由）。 |
| `manifest.test.ts` | PWA Manifest | ← `manifest.ts` | manifest 字段单测。 |
| `page.tsx` | Dashboard Page | ← `components/ui/*`, `hooks/use-gateway-events.ts` | 仪表盘首页：状态、用量与手动心跳触发。 |
| `page.test.tsx` | Dashboard Page | ← `page.tsx` | 首页渲染单测。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `layout.tsx` | `components/nav/shell.tsx`, `components/pwa/pwa-client.tsx` | — |
| `page.tsx` | `components/ui/*`, `hooks/use-gateway-events.ts` | — |
| `manifest.test.ts` | `manifest.ts` | — |
| `page.test.tsx` | `page.tsx` | — |
| `globals.css` | — | `layout.tsx` |
| `manifest.ts` | — | `manifest.test.ts` |
| `favicon.ico` | — | — |
