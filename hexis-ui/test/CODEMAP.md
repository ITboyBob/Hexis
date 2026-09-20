---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — hexis-ui/test/

## Summary
Vitest 全局测试支撑目录：提供 jsdom 环境下的 jest-dom 匹配器与每个用例后的清理，并校验 Prisma 客户端已生成且可导入。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解 Vitest 全局 setup 如何挂载 jest-dom 与 cleanup | Test Setup | `setup.ts` | `vitest.config.ts` |
| 理解 Prisma 客户端可用性校验用例 | Test Setup | `prisma-client.test.ts` | `lib/prisma.ts` |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `prisma-client.test.ts` | Test Setup | ← `lib/prisma.ts` | 校验 Prisma 客户端已生成且可导入，失败时给出生成命令提示。 |
| `setup.ts` | Test Setup | ← `—` | 注册 `@testing-library/jest-dom/vitest` 并在每个用例后 cleanup。 |
