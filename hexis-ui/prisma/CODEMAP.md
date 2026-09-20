---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — hexis-ui/prisma/

## Summary
Prisma schema 目录：声明 PostgreSQL datasource、生成客户端目标平台与面板直接读取的 `Config` 模型（inferred：面板多数表由 Python 侧迁移管理，此处仅暴露 config 映射）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解 Prisma 生成配置与二进制目标平台 | Prisma Schema | `schema.prisma` | `package.json`, `next.config.ts` |
| 理解面板可直接访问的 `Config` 模型与表映射 | Prisma Schema | `schema.prisma` | `lib/prisma.ts` |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `schema.prisma` | Prisma Schema | ← `—` \| → `lib/prisma.ts` | 定义 PostgreSQL datasource、client 生成目标与 `config` 表模型。 |
