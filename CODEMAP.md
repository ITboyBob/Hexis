---
mode: learning
commit: 7423622a
ignore: .git/, node_modules/, .next/, .venv/, __pycache__/, dist/, build/, caches, logs, lockfiles (uv.lock, bun.lock, package-lock.json), binaries, images/fonts, .env + 项目 .gitignore
generated_at: 2026-09-20
stats:
  total_files: 1172
  total_lines: 374711
  total_size: 19 MB
---

# CODEMAP — Hexis

## Summary
Hexis 是一个 Postgres 原生的认知架构：把任意 LLM 包装成具备持久记忆、身份与自主行为的 agent，核心命题是「数据库即大脑」——状态与逻辑都活在 PostgreSQL 中，Python 只是薄薄的便利层（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解 agent 如何从对话/心跳驱动 LLM 与工具 | Agent Runtime | `core/CODEMAP.md`, `services/CODEMAP.md` | `core/agent_loop.py`, `services/agent.py` |
| 理解记忆的写入、召回与 hydrate 全路径 | Memory | `core/CODEMAP.md`, `db/CODEMAP.md` | `core/cognitive_memory_api.py`, `db/04_functions_core.sql` |
| 理解 LLM provider 分派、鉴权与重试 | LLM Transport | `core/CODEMAP.md`, `core/auth/CODEMAP.md` | `core/llm.py`, `core/llm_config.py` |
| 理解工具系统的基类、注册与策略 | Tool System | `core/tools/CODEMAP.md` | `core/tools/base.py`, `core/tools/registry.py` |
| 理解心跳循环、能量经济与自主决策 | Heartbeat | `services/CODEMAP.md`, `db/CODEMAP.md` | `services/worker_service.py`, `db/11_functions_core_heartbeat.sql` |
| 理解 CLI 子命令与初始化向导 | CLI | `apps/CODEMAP.md` | `apps/hexis_cli.py`, `apps/hexis_init.py` |
| 理解 HTTP API 与 SSE 流式聊天 | API Server | `apps/CODEMAP.md` | `apps/hexis_api.py`, `services/chat.py` |
| 理解多渠道消息适配与主动外发 | Channels | `channels/CODEMAP.md`, `services/CODEMAP.md` | `channels/manager.py`, `services/outbound_safety.py` |
| 理解数据库 schema 与前向迁移机制 | Database | `db/CODEMAP.md`, `db/migrations/CODEMAP.md` | `core/migrations.py`, `db/00_tables.sql` |
| 理解 HMX 记忆交换与心智可移植性 | Memory Exchange | `core/CODEMAP.md`, `schemas/CODEMAP.md` | `core/memory_exchange.py`, `core/protected_replacement.py` |
| 理解内容摄入（fast/slow/RLM）流水线 | Ingestion | `services/ingest/CODEMAP.md`, `services/CODEMAP.md` | `services/ingest/pipeline.py`, `services/slow_ingest_rlm.py` |
| 理解技能与插件系统 | Extensibility | `skills/CODEMAP.md`, `plugins/CODEMAP.md` | `services/skill_runtime.py`, `core/tools/skills.py` |
| 理解 Web 面板（页面与 API 路由） | Web UI | `hexis-ui/CODEMAP.md`, `hexis-ui/app/CODEMAP.md` | `hexis-ui/lib/CODEMAP.md`, `hexis-ui/app/api/CODEMAP.md` |
| 理解 prompt 模板体系与 DB 端渲染 | Prompt | `services/prompts/CODEMAP.md`, `db/CODEMAP.md` | `services/prompt_resources.py`, `db/39_functions_prompt_render.sql` |
| 理解部署、容器与 CI | Deployment | `ops/CODEMAP.md` | `docker-compose.yml`, `ops/Dockerfile.worker` |
| 理解测试布局与数据库集成测试约定 | Testing | `tests/CODEMAP.md` | `tests/conftest.py`, `tests/utils.py` |
| 理解公开记忆基准与检索评测 | Evals | `evals/CODEMAP.md` | `evals/memory_benchmark/run.py`, `evals/retrieval/run.py` |
| 理解产品与哲学文档 | Docs | `docs/CODEMAP.md` | `docs/concepts/database-is-the-brain.md`, `docs/philosophy/PERSONHOOD.md` |
| 理解角色卡预设体系 | Characters | `characters/CODEMAP.md` | `characters/hexis.json`, `core/init_api.py` |

## Subdirectories
| Dir | Domain | Depends On | Purpose |
|---|---|---|---|
| `core/` | Core Interfaces | `db/`, `services/` | 底层接口层：DB 连接、LLM 传输、工具框架、消息网关、HMX 记忆交换。 |
| `services/` | Service Orchestration | `core/` | 构建在 core 之上的编排与工作流：对话、心跳、摄入、记忆维护、连接器。 |
| `apps/` | Entrypoints | `core/`, `services/` | 全部进程入口：CLI、初始化向导、FastAPI 服务、MCP 服务器、worker、语音边车。 |
| `channels/` | Messaging Channels | `core/`, `services/` | Discord/Telegram/Slack/Signal/WhatsApp/iMessage/Matrix 适配层与出站消费。 |
| `db/` | Database Schema | `core/`, `services/` | schema 权威：119 个基线 SQL 与 251 个前向迁移，定义表、函数、触发器、视图与 prompt 种子。 |
| `hexis-ui/` | Web Dashboard | `core/`, `apps/` | Next.js 面板：App Router 页面、API 路由、Prisma 与共享逻辑。 |
| `tests/` | Testing | 全部源码目录 | pytest 测试总目录（约 330 模块），以真实 Postgres + 事务隔离覆盖各层。 |
| `evals/` | Evaluation | `core/`, `services/`, `db/` | 公开记忆基准与检索评测 harness。 |
| `skills/` | Skill System | `core/`, `services/` | 技能框架与 41 个随包 `SKILL.md` 定义。 |
| `plugins/` | Plugin System | `core/` | 插件框架与 7 个随包插件。 |
| `docs/` | Documentation | — | 面向用户/贡献者的文档树（start/guides/operations/reference/concepts/philosophy 等）。 |
| `ops/` | Deployment | `db/`, `core/` | Dockerfile、运行时 compose、DB 管理脚本与 CI 辅助。 |
| `characters/` | Character Presets | `core/` | 11 张 chara_card_v2 预设角色卡与头像。 |
| `schemas/` | Interop Contracts | `core/` | HMX v1.7 规范性 JSON Schema。 |
| `scripts/` | Utilities | `db/`, `services/` | 一次性工具脚本（DB 脑审计、prompt 种子生成）。 |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `CognitiveMemory` | `core/cognitive_memory_api.py` | L:241 |
| `AgentLoop` | `core/agent_loop.py` | L:336 |
| `chat_completion` | `core/llm.py` | L:1411 |
| `run_agent` | `services/agent.py` | L:835 |
| `chat_turn` | `services/chat.py` | L:537 |
| `HeartbeatWorker` | `services/worker_service.py` | L:418 |
| `MaintenanceWorker` | `services/worker_service.py` | L:803 |
| `main`（CLI 入口） | `apps/hexis_cli.py` | L:7262 |
| `app`（FastAPI） | `apps/hexis_api.py` | L:107 |
| `ChannelManager` | `channels/manager.py` | L:54 |
| `fast_recall` | `db/04_functions_core.sql` | L:91 |
| `run_heartbeat` | `db/11_functions_core_heartbeat.sql` | L:83 |
| `export_hmx` / `import_hmx` | `core/memory_exchange.py` | L:810 / L:2240 |
| `ToolHandler` | `core/tools/base.py` | L:377 |
| `load_skills` | `skills/loader.py` | L:191 |

## Files
| File | Domain | Function |
|---|---|---|
| `README.md` | Docs Hub | 项目定位、能力总览、安装/快速开始、架构与 CLI 速查。 |
| `CLAUDE.md` | Agent Instructions | 仓库结构、构建/测试命令、编码风格、安全与运维约定。 |
| `AGENTS.md` | Agent Instructions | agent 入场须知：经验线（Experience Bar）、凭据与 OSS 无鉴权的设计立场。 |
| `HEXIS_EXPERIENCE_BAR.md` | Agent Instructions | 每项用户可见改动必须通过的 8 条端到端体验标准与失败教训。 |
| `MISSION.md` / `MISSION_PROGRESS.md` | Project Planning | 项目使命与进度记录。 |
| `PLAN.md` / `ROADMAP.md` / `ToDo.md` | Project Planning | 长期实施计划、路线图与待办。 |
| `CONTRIBUTING.md` | Contributing | 贡献流程与约定。 |
| `why_i_suck_and_how_to_fix_it.md` | Agent Instructions | 关于抽象/高度失误导致体验问题的反思文。 |
| `pyproject.toml` | Build Config | 包元数据、依赖、可选依赖组与 console scripts。 |
| `pytest.ini` | Build Config | pytest 配置。 |
| `docker-compose.yml` | Deployment | 本地栈：Postgres、worker、API、UI、RabbitMQ 与浏览器服务。 |
| `mise.toml` | Build Config | 工具版本与任务编排。 |
| `uv.lock` | Build Config | Python 依赖锁定（已忽略，仅作参考）。 |
| `LICENSE` | Legal | MIT 许可。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `AGENTS.md` | `CLAUDE.md`, `HEXIS_EXPERIENCE_BAR.md` | 全部 agent 会话 |
| `CLAUDE.md` | `HEXIS_EXPERIENCE_BAR.md` | `AGENTS.md` |
| `pyproject.toml` | — | `docker-compose.yml`, `ops/Dockerfile.*` |
| `docker-compose.yml` | `pyproject.toml` | `ops/docker-compose.runtime.yml` |
