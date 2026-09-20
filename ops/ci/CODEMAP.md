---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — ops/ci/

## Summary
`ops/ci/` 存放 CI 专用辅助脚本：一个确定性假嵌入服务，以及一个证明已有数据库迁移后数据存活的验证器（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解 CI 如何用无 GB 级模型的假嵌入服务 | 假嵌入服务 | `fake_embeddings.py` | `db/03_functions_helpers.sql` |
| 理解 CI 如何验证迁移不丢数据 | 迁移存活验证 | `migration_survivor.py` | `db/migrations/README.md` |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `fake_embeddings.py` | 假嵌入服务 | `db/03_functions_helpers.sql` | 确定性假嵌入服务器，实现 `GET /api/tags` 健康探针与 `POST /api/embed`（SHA-256 派生的稳定 768 维向量，无语义）。 |
| `migration_survivor.py` | 迁移存活验证 | `db/migrations/README.md` | 从已发布 `hexis-brain` 镜像数据库出发，插入持久状态、跑迁移 runner，验证数据仍在且前向迁移幂等。 |
