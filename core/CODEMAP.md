---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — core/

## Summary
`core/` 是 Hexis 的底层接口层，承载数据库连接、LLM 传输、工具框架、消息网关与 HMX 记忆交换等“数据库即大脑”的基础原语（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解一次对话/心跳如何驱动统一 agent 循环 | Agent Runtime | `agent_loop.py` | `llm.py`, `usage.py` |
| 理解 LLM 调用如何按 provider 分派与重试 | LLM Transport | `llm.py` | `providers/`, `integration_reliability.py` |
| 理解数据库连接串与实例解析从何而来 | DB Connectivity | `agent_api.py` | `instance.py`, `config.py` |
| 理解记忆写入/召回/hydrate 的异步客户端 | Cognitive Memory | `cognitive_memory_api.py` | `memory_repo.py`, `memory_tools.py` |
| 理解 HMX 导出/导入与受保护区块替换策略 | Memory Exchange | `memory_exchange.py` | `protected_replacement.py`, `digest.py` |
| 理解事件的统一入口与消费模型 | Event Gateway | `gateway.py` | `rabbitmq_bridge.py`, `state.py` |
| 理解工具系统的基类、注册表与策略 | Tool System | `tools/` | `tools/base.py`, `tools/registry.py` |
| 理解各 provider 的 OAuth/令牌获取流程 | Provider Auth | `auth/` | `auth/store.py`, `auth/utils.py` |
| 理解非 SDK provider 的 HTTP 实现 | Provider Runtime | `providers/` | `llm.py` |
| 理解数据库 schema 与迁移执行 | DB Migrations | `migrations.py` | `schema.py` |
| 理解伴随节点的身份、能力与唤醒 | Companion Node | `node_daemon.py` | `node_identity.py`, `node_wake.py` |
| 理解对外网络暴露（tailnet）与语音边车 | Network Exposure | `tunnel.py` | `voice_sidecar.py` |

## Subdirectories
| Dir | Domain | Depends On | Purpose |
|---|---|---|---|
| `tools/` | Tool System | `core/llm.py` | 可插拔工具框架与全部内置 ToolHandler 实现 |
| `auth/` | Provider Auth | `core/integration_reliability.py` | 各 LLM/连接器 provider 的 OAuth 与凭据存储 |
| `providers/` | Provider Runtime | `core/integration_reliability.py` | 非 SDK provider（Anthropic HTTP、Google Code Assist）的运行时实现 |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `CognitiveMemory` | `cognitive_memory_api.py` | L:241 |
| `chat_completion` | `llm.py` | L:1411 |
| `load_llm_config` | `llm_config.py` | L:124 |
| `AgentLoop` | `agent_loop.py` | L:336 |
| `db_dsn_from_env` | `agent_api.py` | L:16 |
| `IntegrationHttpError` | `integration_reliability.py` | L:39 |
| `Gateway` | `gateway.py` | L:89 |
| `MemoryRepo` | `memory_repo.py` | L:23 |
| `chat_json` | `llm_json.py` | L:46 |
| `MemoryType` | `cognitive_memory_api.py` | L:28 |
| `export_hmx` | `memory_exchange.py` | L:810 |
| `import_hmx` | `memory_exchange.py` | L:2240 |
| `normalize_llm_config` | `llm.py` | L:701 |
| `record_llm_usage` | `usage.py` | L:144 |
| `content_hash_v1` | `digest.py` | L:102 |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `agent_loop.py` | Agent Runtime | ← `llm.py`, `tools/base.py`, `usage.py`, `services/skill_runtime.py` \| → `services/agent.py` 等 18 文件 | 聊天与心跳共用的单一 agentic 工具调用循环（大文件 1612 行） |
| `agent_api.py` | DB Connectivity | ← — \| → 16 files (foundational); rg "db_dsn_from_env" --type py -l | 从环境解析实例 DSN、带重试建立 asyncpg 连接与状态查询 |
| `backup_restore.py` | Backup/Restore | ← `agent_api.py` \| → `apps/hexis_cli.py` | 实例感知、AGE 安全的 pg_dump 备份与恢复 |
| `browser.py` | Browser Launch | ← — \| → 11 files | 在含 WSL 的宿主上打开用户默认浏览器 |
| `capability_maturity.py` | Capability Maturity | ← — \| → — | 在回滚事务中跑真实能力演示并产出成熟度记分卡 |
| `cli_api.py` | CLI/Diagnostics API | ← `agent_api.py` \| → 7 files | 面向 CLI/UI 的状态、诊断与 dashboard 探测载荷（大文件 1558 行） |
| `cognitive_memory_api.py` | Cognitive Memory | ← — \| → 16 files | Postgres 认知记忆的异步客户端：remember/recall/hydrate（大文件 2103 行） |
| `config.py` | Configuration | ← — \| → `apps/hexis_cli.py`, `services/web_push.py` | 环境变量与配置文件叠加的 Hexis 配置读取 |
| `consent.py` | Consent | ← — \| → `apps/hexis_cli.py` | consent_log 与配置状态之上的薄异步访问层 |
| `digest.py` | HMX Canonical Digest | ← — \| → 12 files | HMX 规范 JSON 序列化与内容/受保护区块/审计摘要 |
| `execution_backends.py` | Execution Backends | ← — \| → `apps/cli_execution.py`, `tools/shell.py`, `tools/code_execution.py` | 数据库所有的本地/远程 shell、脚本与 Python 执行后端（大文件 1168 行） |
| `gateway.py` | Event Gateway | ← — \| → `apps/hexis_api.py`, `channels/conversation.py`, `services/worker_service.py`, `services/chat.py` | 聊天/心跳/cron/webhook/渠道事件的统一网关与消费者 |
| `hmx_files.py` | HMX File I/O | ← `memory_exchange.py` \| → `apps/cli_exchange.py` | HMX JSON/JSONL 文档的安全文件读写 |
| `hmx_interview.py` | HMX Onboarding | ← — \| → `apps/cli_exchange.py` | 运行 HMX 文档内嵌的导入引导访谈 |
| `host_services.py` | Host Services | ← — \| → `apps/hexis_cli.py` | 将无状态 worker 安装/控制为每用户宿主服务 |
| `init_api.py` | Init Flow | ← — \| → 5 files | CLI 与 UI 共享的初始化逻辑：角色卡、consent 流程 |
| `instance.py` | Instance Registry | ← — \| → `apps/hexis_cli.py` | 多实例注册表（~/.hexis/instances.json） |
| `instance_api.py` | Instance Management | ← `instance.py`, `schema.py`, `services/prompt_resources.py` \| → `apps/hexis_cli.py` | 实例创建、删除、克隆与导入的高层操作 |
| `integration_reliability.py` | Integration Reliability | ← — \| → 39 files (foundational); rg "IntegrationHttpError" --type py -l | 外部集成共享的超时/重试/限流/错误体可靠性原语 |
| `llm.py` | LLM Transport | ← `integration_reliability.py`, `providers/` \| → 12 files | 多 provider 的 LLM 完成与流式传输层（大文件 2186 行） |
| `llm_batch.py` | LLM Batching | ← `llm_json.py` \| → `services/connector_cognition.py`, `services/summarization.py` | 单次调用批量分类 N 条内容 |
| `llm_config.py` | LLM Config | ← `llm.py` \| → 28 files | 从 DB 解析并规范化各 provider 的 LLM 配置 |
| `llm_json.py` | LLM JSON | ← `llm.py` \| → 15 files | 结构化 JSON 输出的解析与 chat_json 辅助 |
| `memory_exchange.py` | Memory Exchange | ← — \| → 12 files | HMX v1.7 意图策略、信封构造与导出/导入流水线（大文件 2986 行） → see memory_exchange.analysis.md |
| `memory_repo.py` | Memory Repo | ← — \| → `services/slow_ingest_rlm.py`, `services/rlm_memory_env.py`, `services/hexis_rlm.py` | 供 RLM REPL syscall 使用的同步记忆访问 |
| `memory_tools.py` | Legacy Memory Tools | ← `cognitive_memory_api.py` \| → — | 仅保留 ApiMemoryToolHandler 的旧同步适配层 |
| `migrations.py` | DB Migrations | ← — \| → `apps/hexis_cli.py` | 前向 schema 迁移、校验和与版本状态 |
| `node_actions.py` | Companion Node | ← — \| → `services/node_gateway.py` | 伴随节点的唯一权威能力词表 |
| `node_daemon.py` | Companion Node | ← `node_actions.py`, `node_identity.py`, `node_life.py`, `node_wake.py` \| → `apps/cli_node.py` | 仅外呼的节点守护进程与本地强制能力 |
| `node_identity.py` | Companion Node | ← — \| → 7 files | 无头节点的 Ed25519 身份与本地策略 |
| `node_life.py` | Companion Node | ← `node_actions.py` \| → — | 在节点上执行固定结构化的日常生活能力 |
| `node_wake.py` | Companion Node | ← — \| → `apps/cli_node.py` | 显式本地唤醒词捕获 |
| `openai_codex_oauth.py` | Auth Shim | ← `auth/openai_codex.py` \| → — | 向后兼容 shim，重导出 Codex OAuth |
| `protected_replacement.py` | HMX Protected Replacement | ← `digest.py`, `memory_exchange.py`, `trust_anchors.py` \| → `apps/hexis_cli.py`, `apps/cli_exchange.py` | HMX 受保护区块替换协议与权威执行（大文件 1909 行） |
| `rabbitmq_bridge.py` | Message Bridge | ← — \| → `apps/hexis_api.py`, `services/worker_service.py` | 可选 RabbitMQ 事件桥接 |
| `schema.py` | DB Schema | ← — \| → — | 实例数据库创建、schema 文件应用与生命周期 |
| `state.py` | Heartbeat State | ← — \| → `services/worker_service.py`, `services/external_calls.py`, `services/heartbeat_runner.py` | 心跳/维护/计划任务的状态迁移入口 |
| `subconscious.py` | Subconscious | ← — \| → `services/subconscious.py` | 潜意识上下文读取与观察写入 |
| `sync_utils.py` | Sync Utils | ← — \| → `services/chat.py` | 将 awaitable 桥接为同步调用 |
| `trust_anchors.py` | HMX Trust | ← — \| → `apps/cli_exchange.py` | HMX 身份声明的 fail-closed 信任锚接口 |
| `tunnel.py` | Network Exposure | ← — \| → `apps/hexis_cli.py` | 私网 Tailscale Serve 生命周期与 OSS 暴露姿态 |
| `usage.py` | Usage Tracking | ← — \| → `apps/hexis_api.py`, `services/ingest/llm.py`, `services/worker_service.py`, `services/hexis_rlm.py` | 将每次 LLM/embedding 调用记入 api_usage |
| `voice_sidecar.py` | Voice Sidecar | ← — \| → `apps/hexis_cli.py` | 可选回环语音边车的自有生命周期 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `agent_api.py` | — | `cli_api.py`, `backup_restore.py` |
| `agent_loop.py` | `llm.py`, `usage.py` | — |
| `backup_restore.py` | `agent_api.py` | — |
| `cli_api.py` | `agent_api.py` | — |
| `cognitive_memory_api.py` | — | `memory_tools.py` |
| `digest.py` | — | `protected_replacement.py` |
| `hmx_files.py` | `memory_exchange.py` | — |
| `instance.py` | — | `instance_api.py` |
| `instance_api.py` | `instance.py`, `schema.py` | — |
| `llm.py` | `integration_reliability.py` | `agent_loop.py`, `llm_config.py`, `llm_json.py` |
| `llm_batch.py` | `llm_json.py` | — |
| `llm_config.py` | `llm.py` | — |
| `llm_json.py` | `llm.py` | `llm_batch.py` |
| `memory_exchange.py` | — | `hmx_files.py`, `protected_replacement.py` |
| `memory_tools.py` | `cognitive_memory_api.py` | — |
| `node_daemon.py` | `node_actions.py`, `node_identity.py`, `node_life.py`, `node_wake.py` | — |
| `node_life.py` | `node_actions.py` | `node_daemon.py` |
| `node_wake.py` | — | `node_daemon.py` |
| `protected_replacement.py` | `digest.py`, `memory_exchange.py`, `trust_anchors.py` | — |
| `schema.py` | — | `instance_api.py` |
| `trust_anchors.py` | — | `protected_replacement.py` |
