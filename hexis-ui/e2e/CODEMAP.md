---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — hexis-ui/e2e/

## Summary
Playwright 端到端测试目录：目前只有一条覆盖初始化向导全流程的 spec，会拉起独立数据库与 Next dev server 后驱动真实 UI。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解初始化流程 e2e 如何准备独立数据库与启动 dev server | E2E Init Flow | `init-flow.spec.ts` | `lib/db.ts`, `app/init/page.tsx` |
| 理解 e2e 如何应用 schema 并等待 Postgres/embedding 就绪 | E2E Init Flow | `init-flow.spec.ts` | `playwright.config.ts` |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `init-flow.spec.ts` | E2E Init Flow | ← `lib/db.ts` | 初始化向导端到端流程：建临时库、应用 schema、启动 dev server 并驱动 UI。 |
