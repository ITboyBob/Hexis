---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — scripts/

## Summary
`scripts/` 存放一次性工具脚本：一个顾问式的 DB 脑审计（找出应迁入 Postgres 的 Python 逻辑），以及从 `services/prompts/*.md` 生成 `db/40_seed_prompt_modules.sql` 的种子烘焙器（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解哪些 Python 逻辑应下推为 SQL | DB 脑审计 | `db_brain_audit.py` | `core/`, `services/`, `channels/`, `apps/` |
| 理解 prompt 种子 SQL 如何从 markdown 重新生成 | Prompt 种子生成 | `gen_prompt_seed.py` | `db/40_seed_prompt_modules.sql`, `services/prompt_resources.py` |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `db_brain_audit.py` | DB 脑审计 | `core/`, `services/`, `channels/`, `apps/` | 顾问式审计：报告应迁移进 Postgres 的 Python 逻辑，Slice 0 只报告不失败（默认扫描根目录为 `core`/`services`/`channels`/`apps`）。 |
| `gen_prompt_seed.py` | Prompt 种子生成 | `services.prompt_resources` | 由 `services/prompts/*.md` 生成 `db/40_seed_prompt_modules.sql`，使 DB 拥有 prompt 文本。 |
