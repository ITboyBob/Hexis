---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — ops/

## Summary
`ops/` 存放 Hexis 的运维产物：四个 Dockerfile（db/worker/channels/ui）、运行时 compose、数据库管理/等待脚本，以及 CI 用的假嵌入服务与迁移存活验证器（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解 DB 镜像如何安装 pgvector/AGE/pgtap 等扩展 | 容器镜像 | `Dockerfile.db` | `docker-compose.runtime.yml` |
| 理解 worker/channels/ui 镜像的构建与固定摘要 | 容器镜像 | `Dockerfile.worker` | `Dockerfile.channels`, `Dockerfile.ui` |
| 理解运行时 compose 如何选择预构建镜像与镜像标签 | 运行时编排 | `docker-compose.runtime.yml` | `Dockerfile.worker` |
| 理解数据库管理命令（启动/重置/备份等） | 运维脚本 | `db-manage.sh` | `wait-for-db.sh` |
| 理解 CI 如何用假嵌入服务替代真实模型 | CI 辅助 | `ci/fake_embeddings.py` | `Dockerfile.db` |
| 理解 CI 如何证明已有数据库迁移后数据存活 | CI 辅助 | `ci/migration_survivor.py` | `db/migrations/README.md` |

## Subdirectories
| Dir | Domain | Depends On | Purpose |
|---|---|---|---|
| `ci/` | CI 辅助 | `db/*.sql` | CI 专用脚本：确定性假嵌入服务与迁移数据存活验证器。 |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `Dockerfile.db` | 容器镜像 | — | `postgres:16-bullseye` 基础镜像，安装构建依赖与 `postgresql-16-pgtap` 等扩展。 |
| `Dockerfile.worker` | 容器镜像 | — | worker 镜像，以固定摘要的 `uv` 与 Python 依赖图构建。 |
| `Dockerfile.channels` | 容器镜像 | — | 渠道适配器镜像，与 worker 共享依赖固定策略。 |
| `Dockerfile.ui` | 容器镜像 | — | dashboard UI 镜像，node 构建阶段含 Prisma openssl 检测。 |
| `docker-compose.runtime.yml` | 运行时编排 | `Dockerfile.*` | pip 安装版 Hexis 的运行时 compose，使用 GHCR 预构建镜像；`HEXIS_IMAGE_TAG` 由 CLI 设为包版本。 |
| `db-manage.sh` | 运维脚本 | `docker-compose.runtime.yml` | 数据库管理脚本（启动/重置/备份等）。 |
| `wait-for-db.sh` | 运维脚本 | — | 等待数据库就绪的启动门控脚本。 |
| `__init__.py` | 包初始化 | — | ops 包标记。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `docker-compose.runtime.yml` | `Dockerfile.*` | `db-manage.sh` |
| `db-manage.sh` | `docker-compose.runtime.yml` | — |
| `ci/fake_embeddings.py` | `Dockerfile.db`（服务形态） | CI 工作流 |
| `ci/migration_survivor.py` | `Dockerfile.db` | CI 工作流 |
