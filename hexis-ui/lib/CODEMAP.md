---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — hexis-ui/lib/

## Summary
面板的共享逻辑层：统一 Prisma 客户端与 JSON 参数归一化、Python API 代理头/URL/SSE 封装、LLM 调用、实例数据库解析、角色卡目录抓取、消息展示规范化、PWA 客户端与提示词加载。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解 Prisma 客户端如何绑定到当前实例数据库 | DB Access | `prisma.ts` | `instance-registry.ts` |
| 理解 JSON 值写入 Postgres 前的归一化 | DB Access | `db.ts` | `prisma.ts` |
| 理解如何解析 `HEXIS_DATABASE_URL`/实例名到连接串 | Instance Registry | `instance-registry.ts` | `prisma.ts` |
| 理解代理到 Python API 的 URL、鉴权头与错误格式 | API Proxy | `python-api.ts` | `app/api/**/route.ts` |
| 理解 SSE 透传与错误 SSE 的构造 | API Proxy | `python-api.ts` | `app/api/events/stream/route.ts` |
| 理解 Talk 原生 WebRTC、配置门控与控制事件 | Realtime Voice | `voice-realtime/client.ts`, `voice-realtime/rtc-contracts.ts` | `app/chat/talk-mode.tsx`, `voice-realtime/avatar-projection.ts` |
| 验证 Talk 建连期间关闭、异步任务取消及控制配置 | Realtime Voice | `voice-realtime/rtc-client.test.ts`, `voice-realtime/rtc-contracts.test.ts` | `app/chat/talk-rtc.test.tsx`, `e2e/voice-rtc.spec.ts` |
| 理解 conscious LLM 配置读取与统一调用 | Shared LLM | `llm.ts` | `app/api/init/import-card/route.ts` |
| 理解从 LLM 文本抽取 JSON | Shared LLM | `llm.ts` | `app/api/init/import-card/route.ts` |
| 理解 init 路由错误响应与 embedding 相关判定 | Init Errors | `init-errors.ts` | `app/api/init/**/route.ts` |
| 理解 init 阶段 LLM 端点解析与目录默认模型 | Init LLM | `init-llm.ts` | `app/api/init/llm/route.ts`, `app/api/init/models/route.ts` |
| 理解角色目录三源（chub/character_tavern/risu_realm）抓取与卡转换 | Character Catalog | `character-catalog.ts` | `app/api/init/character-catalog/route.ts` |
| 理解前端消息展示块规范化 | Presentation | `message-presentation.ts` | `app/chat/message-presentation.tsx` |
| 理解 PWA 安装提示、Service Worker 与推送订阅 | PWA Client | `pwa-client.ts` | `app/components/pwa/pwa-client.tsx` |
| 理解人格/对话提示词组装与可选提示词列举 | Prompts | `prompts.ts` | `lib/llm.ts` |
| 理解按模型汇总用量 | Usage Summary | `usage-summary.ts` | `app/api/usage/route.ts` |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `prisma` | `prisma.ts` | L:11 |
| `normalizeJsonValue` | `db.ts` | L:1 |
| `toJsonParam` | `db.ts` | L:15 |
| `resolveHexisDatabaseUrl` | `instance-registry.ts` | L:123 |
| `resolveHexisDatabase` | `instance-registry.ts` | L:87 |
| `hexisApiBaseUrl` | `python-api.ts` | L:3 |
| `resolveHexisApiUrl` | `python-api.ts` | L:11 |
| `hexisApiHeaders` | `python-api.ts` | L:18 |
| `jsonProxyResponse` | `python-api.ts` | L:31 |
| `sseProxyResponse` | `python-api.ts` | L:61 |
| `getConsciousLlmConfig` | `llm.ts` | L:46 |
| `callLlm` | `llm.ts` | L:81 |
| `extractJson` | `llm.ts` | L:374 |
| `searchCharacterCatalog` | `character-catalog.ts` | L:136 |
| `normalizeMessagePresentation` | `message-presentation.ts` | L:40 |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `character-catalog.ts` | Character Catalog | ← `—` \| → `app/api/init/character-catalog/route.ts` | 三个角色目录源的搜索/下载与卡片转换、persona 合并。 |
| `character-catalog.test.ts` | Character Catalog | ← `character-catalog.ts` | 角色目录解析与转换的单测。 |
| `db.ts` | DB Access | ← `—` \| → 46 files (foundational); rg "from \"@/lib/db\"" -l | JSON 值归一化与 JSON 参数封装。 |
| `init-errors.ts` | Init Errors | ← `—` \| → 10 files; rg "from \"@/lib/init-errors\"" -l | init 路由统一错误响应与 embedding 限额/不可用判定。 |
| `init-errors.test.ts` | Init Errors | ← `init-errors.ts` | init 错误判定单测。 |
| `init-llm.ts` | Init LLM | ← `—` \| → `app/api/init/llm/route.ts`, `app/api/init/models/route.ts` | init 阶段 LLM 端点解析与目录默认模型选择。 |
| `init-llm.test.ts` | Init LLM | ← `init-llm.ts` | 端点解析与默认模型单测。 |
| `instance-registry.ts` | Instance Registry | ← `—` \| → `prisma.ts` | 从环境/实例名解析数据库名与连接 URL。 |
| `instance-registry.test.ts` | Instance Registry | ← `instance-registry.ts` | 实例解析单测。 |
| `llm.ts` | Shared LLM | ← `prisma.ts`, `db.ts` \| → `app/api/init/import-card/route.ts` | 读取 conscious LLM 配置并统一调用（含 JSON 抽取）。 |
| `message-presentation.ts` | Presentation | ← `—` \| → `app/chat/message-presentation.tsx` | 前端展示块的类型与规范化。 |
| `message-presentation.test.ts` | Presentation | ← `message-presentation.ts` | 展示规范化单测。 |
| `prisma.ts` | DB Access | ← `instance-registry.ts` \| → 63 files (foundational); rg "from \"@/lib/prisma\"" -l | 单例 Prisma 客户端，绑定当前实例数据库 URL。 |
| `prompts.ts` | Prompts | ← `—` | 人格/对话提示词组装与可选提示词加载。 |
| `pwa-client.ts` | PWA Client | ← `—` \| → `app/components/pwa/*.tsx`, `app/chat/talk-mode.tsx` | PWA 安装提示、Service Worker 注册、推送订阅与设备 ID。 |
| `python-api.ts` | API Proxy | ← `—` \| → 30 files; rg "from \"@/lib/python-api\"" -l | 代理到 Python API 的基址、鉴权头、JSON 与 SSE 响应封装。 |
| `usage-summary.ts` | Usage Summary | ← `—` \| → `app/api/usage/route.ts` | 按模型汇总用量行。 |
| `usage-summary.test.ts` | Usage Summary | ← `usage-summary.ts` | 用量汇总单测。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `prisma.ts` | `instance-registry.ts` | `llm.ts` |
| `llm.ts` | `prisma.ts`, `db.ts` | — |
| `character-catalog.ts` | — | — |
| `db.ts` | — | `llm.ts` |
| `instance-registry.ts` | — | `prisma.ts` |
| `python-api.ts` | — | — |
| `init-errors.ts` | — | — |
| `init-llm.ts` | — | — |
| `message-presentation.ts` | — | — |
| `prompts.ts` | — | — |
| `pwa-client.ts` | — | — |
| `usage-summary.ts` | — | — |
| `character-catalog.test.ts` | `character-catalog.ts` | — |
| `init-errors.test.ts` | `init-errors.ts` | — |
| `init-llm.test.ts` | `init-llm.ts` | — |
| `instance-registry.test.ts` | `instance-registry.ts` | — |
| `message-presentation.test.ts` | `message-presentation.ts` | — |
| `usage-summary.test.ts` | `usage-summary.ts` | — |
