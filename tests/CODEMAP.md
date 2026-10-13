---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — tests/

## Summary
`tests/` 是 Hexis 的 pytest 测试总目录（约 330 个测试模块），以 asyncpg 连接真实 Postgres、按事务/临时库隔离，覆盖 db 层 SQL、core 服务、CLI、web API、插件与评测；子目录按被测子系统分组（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解测试如何拿到真实数据库连接与隔离 | 测试基建 | `conftest.py` | `utils.py` |
| 验证 Talk RTC 控制、初始化、关闭终态与工具来源 | Realtime Voice | `voice_realtime/test_rtc_session.py`, `voice_realtime/test_rtc_api.py`, `voice_realtime/test_rtc_bootstrap.py` | `db/test_voice_rtc_history.py`, `db/test_voice_turn_completion.py` |
| 理解 DB schema、迁移与 prompt 渲染的回归测试 | DB 层测试 | `db/CODEMAP.md` | `db/test_migrations.py`, `db/test_prompt_render.py` |
| 理解记忆召回/巩固（RecMem）在 SQL 端的验证 | DB 层测试 | `db/test_recmem.py` | `db/test_memory_edges.py`, `db/test_retrieval_eval.py` |
| 理解心跳状态机与能量经济的测试 | DB 层测试 | `db/test_heartbeat_state.py` | `db/test_heartbeat_economy.py`, `db/test_finalize_heartbeat.py` |
| 理解 HMX 记忆交换的往返与受保护替换测试 | DB 层测试 | `db/test_hmx_export.py` | `db/test_hmx_import.py`, `db/test_hmx_authoritative_import.py` |
| 理解 agent loop 与 LLM 交互的单测 | Core 测试 | `core/CODEMAP.md` | `core/test_agent_loop.py`, `core/test_llm.py` |
| 理解核心 API（记忆/工具/渠道）的集成测试 | Core 测试 | `core/test_core_api.py` | `core/test_cognitive_memory_extra.py`, `core/test_memory_tools.py` |
| 理解 CLI 子命令（up/init/migrate/voice 等）的端到端测试 | CLI 测试 | `cli/CODEMAP.md` | `cli/test_cli.py`, `cli/test_init_noninteractive.py` |
| 理解 HTTP API 与 dashboard 后端的测试 | Web 测试 | `web/CODEMAP.md` | `web/test_web.py`, `web/test_openai_compat.py` |
| 理解 worker 与后台服务（RLM/心跳/摄取）的测试 | Services 测试 | `services/CODEMAP.md` | `services/test_rlm_chat.py`, `services/test_heartbeat_agentic.py` |
| 理解已安装插件的工具测试 | Plugins 测试 | `plugins/CODEMAP.md` | `plugins/test_asana.py`, `plugins/test_youtube.py` |
| 理解公开记忆基准与用户模型轨迹评测 | Evals 测试 | `evals/CODEMAP.md` | `evals/test_memory_benchmark.py`, `evals/test_user_model_trajectories.py` |
| 查找 digest / prompt 渲染的黄金样本 fixture | 测试数据 | `fixtures/CODEMAP.md` | `fixtures/digest/`, `fixtures/prompt_render/` |

## Subdirectories
| Dir | Domain | Depends On | Purpose |
|---|---|---|---|
| `cli/` | CLI 测试 | `apps/hexis_cli.py` | 22 个模块，验证 CLI 子命令、初始化、安装/卸载、语音与隧道等终端行为。 |
| `core/` | Core 测试 | `core/*` | 109 个模块，验证记忆 API、agent loop、工具、渠道、LLM provider、HMX 等核心层。 |
| `db/` | DB 层测试 | `db/*.sql` | 129 个模块，直接针对 SQL schema/函数/迁移/触发器做回归与不变量验证。 |
| `evals/` | Evals 测试 | `evals/*` | 2 个模块，验证公开记忆基准 schema/评分与用户模型轨迹。 |
| `fixtures/` | 测试数据 | — | 黄金样本数据：HMX digest 与 prompt 渲染的期望输出。 |
| `plugins/` | Plugins 测试 | `plugins/installed/*` | 8 个模块，验证各已安装插件工具与插件校验。 |
| `services/` | Services 测试 | `services/*` | 51 个模块，验证 worker、RLM、心跳、摄取、连接器、保留面等服务层。 |
| `web/` | Web 测试 | `apps/hexis_api.py` | 9 个模块，验证 API 端点、附件、收件箱回复、OpenAI 兼容层与语音 API。 |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `temp_test_db`（模块级临时库 fixture） | `conftest.py` | L:41 |
| `db_pool`（asyncpg 连接池 fixture） | `conftest.py` | L:116 |
| `apply_repo_migrations`（迁移应用 fixture） | `conftest.py` | L:307 |
| `ensure_embedding_service`（嵌入服务 fixture） | `conftest.py` | L:327 |
| `configure_agent_for_tests` | `conftest.py` | L:156 |
| `get_test_identifier` | `utils.py` | L:9 |
| `_db_dsn` | `utils.py` | L:14 |
| `timed_db_call` | `utils.py` | L:29 |
| `embed_pending_memories` | `utils.py` | L:102 |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `conftest.py` | 测试基建 | ← `tests.utils` | 全局 fixture：重试连接、模块级临时库、连接池、嵌入维度同步、agent 测试配置与迁移应用。 |
| `utils.py` | 测试基建 | ← 环境变量 | 测试标识生成、DSN 解析、计时 DB 调用、批量嵌入待处理记忆等助手。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `conftest.py` | `utils.py` | 全部子目录测试模块 |
| `utils.py` | — | `conftest.py`, `db/*`, `core/*`, `services/*` |
