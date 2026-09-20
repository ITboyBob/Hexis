---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — hexis-ui/

## Summary
Hexis 的 Next.js 15/16 面板：App Router 页面与 API 路由位于 `app/`，共享逻辑位于 `lib/`，通过 Prisma 直连 Postgres 或经 `lib/python-api.ts` 代理到 Python API 服务。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解面板如何区分「直连 DB」与「代理 Python API」两类路由 | API Proxy | `lib/python-api.ts` | `lib/prisma.ts` |
| 理解共享 LLM 调用与 JSON 抽取 | Shared LLM | `lib/llm.ts` | `lib/prisma.ts` |
| 理解实例/数据库 URL 解析 | Instance Registry | `lib/instance-registry.ts` | `lib/prisma.ts` |
| 理解角色卡目录抓取与 persona 合并 | Character Catalog | `lib/character-catalog.ts` | `app/init/page.tsx` |
| 理解前端消息展示块规范化 | Presentation | `lib/message-presentation.ts` | `app/chat/message-presentation.tsx` |
| 理解 PWA 安装/推送/Service Worker | PWA Client | `lib/pwa-client.ts` | `app/components/pwa/pwa-client.tsx` |
| 理解路由页与 API 路由的整体结构 | App Router | `app/` | `app/CODEMAP.md` |
| 理解初始化端到端流程的 e2e 覆盖 | E2E | `e2e/init-flow.spec.ts` | `app/init/page.tsx` |
| 理解 Vitest 测试环境与 Prisma 客户端可用性校验 | Test Setup | `test/setup.ts` | `lib/prisma.ts` |
| 理解 Prisma schema 与生成的客户端 | Prisma | `prisma/schema.prisma` | `lib/prisma.ts` |
| 理解构建/测试/开发脚本入口 | Build Config | `package.json` | `next.config.ts`, `vitest.config.ts`, `playwright.config.ts` |

## Subdirectories
| Dir | Domain | Depends On | Purpose |
|---|---|---|---|
| `app/` | App Router | `lib/` | 全部页面、API 路由、组件与 PWA 资源。 |
| `lib/` | Shared Logic | `—` | 跨页面复用的 DB/LLM/代理/目录/展示逻辑与单测。 |
| `e2e/` | E2E | `lib/db.ts` | Playwright 端到端流程（初始化）。 |
| `test/` | Test Setup | `lib/prisma.ts` | Vitest 全局 setup 与 Prisma 客户端可用性校验。 |
| `prisma/` | Prisma | `—` | Prisma schema 与生成配置。 |
| `public/` | Assets | `—` | 静态资源：图标、SVG、Service Worker 与离线页（不再逐项列举）。 |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `prisma` | `lib/prisma.ts` | L:11 |
| `normalizeJsonValue` | `lib/db.ts` | L:1 |
| `resolveHexisApiUrl` | `lib/python-api.ts` | L:11 |
| `hexisApiHeaders` | `lib/python-api.ts` | L:18 |
| `sseProxyResponse` | `lib/python-api.ts` | L:61 |
| `getConsciousLlmConfig` | `lib/llm.ts` | L:46 |
| `callLlm` | `lib/llm.ts` | L:81 |
| `resolveHexisDatabaseUrl` | `lib/instance-registry.ts` | L:123 |
| `searchCharacterCatalog` | `lib/character-catalog.ts` | L:136 |
| `normalizeMessagePresentation` | `lib/message-presentation.ts` | L:40 |
| `registerHexisServiceWorker` | `lib/pwa-client.ts` | L:50 |
| `composePersonhoodPrompt` | `lib/prompts.ts` | L:52 |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `eslint.config.mjs` | Build Config | ← `eslint-config-next` | ESLint 配置（Next core-web-vitals + TypeScript）。 |
| `next.config.ts` | Build Config | ← `—` | Next standalone 输出、Turbopack 根与角色目录图片远端域名。 |
| `package.json` | Build Config | ← `—` | 依赖与 dev/build/lint/test/test:e2e 脚本（dev 端口 3477）。 |
| `playwright.config.ts` | Build Config | ← `—` | Playwright 配置：`e2e/` 目录、baseURL 与端口。 |
| `postcss.config.mjs` | Build Config | ← `@tailwindcss/postcss` | Tailwind v4 PostCSS 插件。 |
| `tsconfig.json` | Build Config | ← `—` | TS 严格模式与 `@/*` 路径别名。 |
| `vitest.config.ts` | Build Config | ← `—` | Vitest：jsdom、setup 文件与 app/lib 测试包含规则。 |
| `README.md` | Docs | ← `—` | create-next-app 默认说明。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `next.config.ts` | — | — |
| `vitest.config.ts` | — | — |
| `playwright.config.ts` | — | — |
| `tsconfig.json` | — | — |
| `package.json` | — | — |
| `postcss.config.mjs` | — | — |
| `eslint.config.mjs` | — | — |
| `README.md` | — | — |
