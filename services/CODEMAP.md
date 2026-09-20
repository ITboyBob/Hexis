---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — services/

## Summary
`services/` 是构建在 `core/` 原语之上的编排与工作流层，覆盖对话、心跳、摄入、记忆维护、连接器认知与出站安全等“数据库持有状态、Python 仅做 I/O”的服务（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解一次对话回合如何组装系统提示并调用 agent | Agent Runtime | `chat.py` | `agent.py`, `skill_runtime.py` |
| 理解统一 agent runner 如何服务聊天与心跳 | Agent Runtime | `agent.py` | `core/agent_loop.py`, `chat.py` |
| 理解 agentic 心跳如何决策与收尾 | Heartbeat | `heartbeat_agentic.py` | `heartbeat_runner.py`, `heartbeat_prompt.py` |
| 理解心跳 worker 与维护 worker 如何消费事件 | Worker Orchestration | `worker_service.py` | `core/gateway.py`, `core/state.py` |
| 理解 worker 环境加载顺序 | Worker Orchestration | `worker_environment.py` | `worker_service.py` |
| 理解 RLM 循环、REPL 与记忆 syscall | RLM Runtime | `hexis_rlm.py` | `rlm_repl.py`, `rlm_memory_env.py` |
| 理解慢速/混合摄入的 RLM 路径 | RLM Runtime | `slow_ingest_rlm.py` | `ingest/`, `hexis_rlm.py` |
| 理解潜意识观察如何编码为持久记忆 | Memory Maintenance | `extraction.py` | `subconscious.py`, `summarization.py` |
| 理解记忆再巩固与信念传播 | Memory Maintenance | `reconsolidation.py` | `contradictions.py`, `belief_propagation.py` |
| 理解 RecMem 嵌入与路由维护步骤 | Memory Maintenance | `recmem.py` | `memory_embeddings.py`, `source_chunks.py` |
| 理解记忆衰减退场（fade）的用户面 | Memory Maintenance | `retention_surface.py` | `memory_supersessions.py` |
| 理解技能选择、绑定与目录 | Skill Runtime | `skill_runtime.py` | `skill_improvement.py` |
| 理解每回合工具面的审计快照 | Skill Runtime | `tool_surface_audit.py` | `capability_probe.py` |
| 理解连接器来源条目如何变成用户模型声明 | Connector Cognition | `connector_cognition.py` | `ambient_responsibilities.py` |
| 理解连接器安装意图与 UI 交接 | Connector Cognition | `connector_setup.py` | `connector_cognition.py` |
| 理解 Gmail/渠道回填如何转换 provider 消息 | Connector Cognition | `gmail_backfill.py` | `channel_backfill.py`, `gmail_actions.py` |
| 理解出站消息的联系人/注意力策略闸门 | Outbound Safety | `outbound_safety.py` | `channels/outbox.py` |
| 理解受保护工具调用的操作者审批 | Operator Governance | `operator_approval.py` | `operator_policy_corrections.py` |
| 理解 Slack 审批按钮的 Block Kit 呈现 | Operator Governance | `approval_slack_actions.py` | `operator_approval.py` |
| 理解渠道 worker 的注册与存活 | Channel Ops | `channel_worker.py` | `channels/` |
| 理解附件文本读取与语音转写 | Channel Ops | `attachments.py` | `voice_notes.py` |
| 理解入站语音笔记的 STT 注入 | Channel Ops | `voice_notes.py` | `speech.py`, `local_audio_analysis.py` |
| 理解 Web Push 订阅与投递 | Channel Ops | `web_push.py` | `channels/outbox.py` |
| 理解伴随节点的签名请求桥 | Channel Ops | `node_gateway.py` | `core/node_identity.py` |
| 理解澄清问题与异步提问生命周期 | Channel Ops | `agent_questions.py` | `core/tools/questions.py` |
| 理解入站消息处置策略 | Channel Ops | `inbound_disposition.py` | `channels/manager.py` |
| 理解提示模板如何从 prompts/ 加载 | Prompt Resources | `prompt_resources.py` | `prompts/` |
| 理解 council 审议的持久化与对抗式运行 | Deliberation | `deliberation.py` | `core/tools/council.py` |
| 理解自动化建议的刷新与采纳 | Automation | `automation_suggestions.py` | `skill_runtime.py` |
| 理解外部调用处理与心跳决策衔接 | External Calls | `external_calls.py` | `heartbeat_runner.py` |
| 理解能力探测如何度量工具面 | Worker Orchestration | `capability_probe.py` | `tool_surface_audit.py` |
| 理解 Twitter/X 连接器适配 | Connector Cognition | `twitter_x.py` | `core/auth/twitter_x.py` |
| 理解生活集成的 provider 客户端与验证 | Connector Cognition | `life_integrations.py` | `core/tools/life_integrations.py` |

## Subdirectories
| Dir | Domain | Depends On | Purpose |
|---|---|---|---|
| `ingest/` | Ingestion Pipeline | `core/cognitive_memory_api.py` | 通用内容摄入流水线（读取、分节、LLM 抽取、入库） |
| `prompts/` | Prompt Templates | — | 31 个产品级提示模板（persona、心跳、RLM、RecMem 等） |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `run_agent` | `agent.py` | L:835 |
| `chat_turn` | `chat.py` | L:537 |
| `select_skills` | `skill_runtime.py` | L:441 |
| `HeartbeatWorker` | `worker_service.py` | L:418 |
| `MaintenanceWorker` | `worker_service.py` | L:803 |
| `preflight_tool_outbound_controls` | `outbound_safety.py` | L:89 |
| `run_slow_ingest_chunk` | `slow_ingest_rlm.py` | L:94 |
| `RLMMemoryEnv` | `rlm_memory_env.py` | L:58 |
| `HexisLocalREPL` | `rlm_repl.py` | L:139 |
| `ExternalCallProcessor` | `external_calls.py` | L:23 |
| `process_wake_utterance` | `node_gateway.py` | L:89 |
| `request_operator_tool_approval` | `operator_approval.py` | L:120 |
| `resolve_disposition` | `inbound_disposition.py` | L:87 |
| `probe_and_record` | `capability_probe.py` | L:152 |
| `run_skill_improvement_review_step` | `skill_improvement.py` | L:343 |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `__init__.py` | Package | ← — \| → — | 包文档，标记服务编排层 |
| `agent.py` | Agent Runtime | ← `core/agent_loop.py`, `core/tools/base.py`, `core/tools/config.py`, `prompt_resources.py`, `skill_runtime.py`, `tool_surface_audit.py` \| → 10 files | 聊天与心跳共用的统一 agent runner 与系统提示组装（大文件 1405 行） |
| `chat.py` | Agent Runtime | ← `core/agent_api.py`, `core/agent_loop.py`, `core/cognitive_memory_api.py`, `core/llm.py`, `core/tools/`, `agent.py`, `connector_setup.py` \| → 7 files | 对话回合的提示、历史与事件流编排（大文件 1203 行） |
| `heartbeat_agentic.py` | Heartbeat | ← `core/tools/config.py`, `agent.py`, `heartbeat_prompt.py` \| → 3 files | 用统一 AgentLoop 运行一轮心跳并收尾 |
| `heartbeat_runner.py` | Heartbeat | ← `core/state.py`, `external_calls.py` \| → — | 执行心跳决策 |
| `heartbeat_prompt.py` | Heartbeat | ← — \| → 3 files | 由数据库渲染的心跳决策提示 |
| `worker_service.py` | Worker Orchestration | ← `core/agent_api.py`, `core/gateway.py`, `core/rabbitmq_bridge.py`, `core/state.py`, 大量同层服务 \| → 7 files | 心跳与维护 worker 及全部维护步骤编排（大文件 1624 行） |
| `worker_environment.py` | Worker Orchestration | ← — \| → — | 在运行时导入前加载所选宿主 worker 环境 |
| `capability_probe.py` | Worker Orchestration | ← `core/tools/base.py` \| → — | 持续度量每个 worker 可用的工具面 |
| `hexis_rlm.py` | RLM Runtime | ← `core/llm.py`, `core/memory_repo.py`, `core/tools/repl_bridge.py`, `prompt_resources.py`, `rlm_memory_env.py`, `rlm_repl.py` \| → 3 files | 面向 Hexis 的递归语言模型循环引擎（大文件 758 行） |
| `rlm_repl.py` | RLM Runtime | ← `core/tools/repl_bridge.py`, `rlm_memory_env.py` \| → 4 files | 带记忆 syscall 与工具桥的本地 REPL 沙箱 |
| `rlm_memory_env.py` | RLM Runtime | ← `core/memory_repo.py` \| → 2 files | REPL 可调用的两阶段记忆检索与工作区预算 |
| `slow_ingest_rlm.py` | RLM Runtime | ← `core/cognitive_memory_api.py`, `core/memory_repo.py`, `hexis_rlm.py`, `ingest/`, `prompt_resources.py`, `rlm_memory_env.py`, `rlm_repl.py` \| → 3 files | 慢速与混合摄入的 RLM 循环 |
| `extraction.py` | Memory Maintenance | ← — \| → — | 潜意识观察 → 持久记忆的 conscious-episode 抽取 |
| `reconsolidation.py` | Memory Maintenance | ← `prompt_resources.py` \| → — | 世界观信念变化后对相关记忆的再巩固扫描 |
| `summarization.py` | Memory Maintenance | ← `prompt_resources.py` \| → — | 记忆巩固摘要 worker |
| `contradictions.py` | Memory Maintenance | ← `prompt_resources.py` \| → — | 持久记忆矛盾检测与摘要发布 |
| `belief_propagation.py` | Memory Maintenance | ← — \| → — | 信念更新流的低延迟监听器 |
| `memory_embeddings.py` | Memory Maintenance | ← — \| → — | 持久记忆嵌入维护步骤 |
| `memory_supersessions.py` | Memory Maintenance | ← — \| → — | 数据库所有的记忆取代谱系类型化访问 |
| `hmx_reembedding.py` | Memory Maintenance | ← — \| → — | 已接受 HMX 记忆嵌入的有界维护步骤 |
| `source_chunks.py` | Memory Maintenance | ← — \| → — | 文档分块的延迟嵌入 |
| `recmem.py` | Memory Maintenance | ← `prompt_resources.py` \| → — | RecMem 近线与巩固 worker |
| `retention_surface.py` | Memory Maintenance | ← — \| → — | 用户可控记忆衰减退场的薄服务包装 |
| `subconscious.py` | Memory Maintenance | ← `core/subconscious.py`, `prompt_resources.py` \| → — | 潜意识决策运行器 |
| `skill_runtime.py` | Skill Runtime | ← `core/tools/base.py` \| → 10 files | 技能优先的运行时：选择、绑定与目录（大文件 705 行） |
| `skill_improvement.py` | Skill Runtime | ← `prompt_resources.py`, `skill_runtime.py` \| → 3 files | 生成持久技能提案的按需后台评审（大文件 604 行） |
| `tool_surface_audit.py` | Skill Runtime | ← `core/tools/base.py` \| → — | 每回合工具面决策的 fail-open 不可变写入 |
| `connector_cognition.py` | Connector Cognition | ← `core/llm_batch.py` \| → — | 将连接器来源条目转为用户模型声明（大文件 931 行） |
| `connector_setup.py` | Connector Cognition | ← `core/tools/` \| → — | 传输中立的连接器安装意图与 UI 交接（大文件 830 行） |
| `ambient_responsibilities.py` | Connector Cognition | ← `core/auth/google_gmail.py`, `connector_cognition.py`, `gmail_backfill.py` \| → — | 环境责任 worker 的 provider I/O 与投递（大文件 885 行） |
| `gmail_backfill.py` | Connector Cognition | ← `core/auth/google_gmail.py`, `core/integration_reliability.py` \| → — | Gmail 连接器回填适配器 |
| `gmail_actions.py` | Connector Cognition | ← `core/auth/google_gmail.py`, `core/integration_reliability.py` \| → — | Gmail provider 副作用辅助 |
| `channel_backfill.py` | Connector Cognition | ← `core/integration_reliability.py`, `channel_worker.py` \| → — | 非 Gmail 连接器回填适配器（大文件 1027 行） |
| `twitter_x.py` | Connector Cognition | ← `core/auth/twitter_x.py`, `core/integration_reliability.py` \| → — | Twitter/X provider 适配器 |
| `life_integrations.py` | Connector Cognition | ← `core/integration_reliability.py` \| → — | 日常生活集成的 provider 客户端与验证安装（大文件 914 行） |
| `inbound_disposition.py` | Channel Ops | ← — \| → — | 数据库所有入站策略之上的传输/分类桥 |
| `outbound_safety.py` | Outbound Safety | ← `core/tools/base.py` \| → — | 数据库所有出站沟通策略的薄桥（大文件 698 行） |
| `operator_approval.py` | Operator Governance | ← `approval_slack_actions.py` \| → — | 精确受保护工具调用的持久操作者审批 |
| `operator_policy_corrections.py` | Operator Governance | ← — \| → — | 身份门控的操作者策略台账桥 |
| `approval_slack_actions.py` | Operator Governance | ← — \| → — | 渲染为 Slack Block Kit 按钮的可移植审批控件 |
| `channel_worker.py` | Channel Ops | ← `core/agent_api.py` \| → 6 files | 运行渠道适配器的无状态 worker 服务 |
| `attachments.py` | Channel Ops | ← — \| → — | 在附件到达的回合内读取其文本 |
| `voice_notes.py` | Channel Ops | ← `core/integration_reliability.py` \| → 7 files | 渠道入站语音笔记转写（大文件 731 行） |
| `speech.py` | Channel Ops | ← `core/integration_reliability.py`, `voice_notes.py` \| → — | 工具/API/talk 模式共享的本地语音合成 |
| `local_audio_analysis.py` | Channel Ops | ← — \| → — | 后台设备本地说话人分离与转写标注 |
| `web_push.py` | Channel Ops | ← `core/config.py`, `core/integration_reliability.py` \| → — | 已安装 Hexis 客户端的私有 Web Push 投递 |
| `node_gateway.py` | Channel Ops | ← `core/node_actions.py`, `core/node_identity.py` \| → — | 签名伴随节点的 WebSocket 网关与持久请求桥 |
| `agent_questions.py` | Channel Ops | ← — \| → — | 持久澄清问题的生命周期辅助 |
| `prompt_resources.py` | Prompt Resources | ← — \| → 6 files | 从 prompts/ 加载并解析产品提示模板 |
| `deliberation.py` | Deliberation | ← — \| → — | 持久的对抗式审议记录与运行（大文件 665 行） |
| `automation_suggestions.py` | Automation | ← — \| → — | 刷新并解析 consent-first 自动化建议 |
| `external_calls.py` | External Calls | ← `core/state.py`, `heartbeat_prompt.py`, `prompt_resources.py` \| → — | 外部调用处理与心跳决策衔接 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `agent.py` | `skill_runtime.py`, `tool_surface_audit.py` | `chat.py`, `heartbeat_agentic.py` |
| `chat.py` | `agent.py`, `connector_setup.py` | — |
| `heartbeat_agentic.py` | `agent.py`, `heartbeat_prompt.py` | `worker_service.py` |
| `heartbeat_runner.py` | `external_calls.py` | `worker_service.py` |
| `heartbeat_prompt.py` | — | `external_calls.py`, `heartbeat_agentic.py` |
| `worker_service.py` | 多个维护服务 | — |
| `hexis_rlm.py` | `rlm_memory_env.py`, `rlm_repl.py` | `slow_ingest_rlm.py` |
| `rlm_repl.py` | `rlm_memory_env.py` | `hexis_rlm.py`, `slow_ingest_rlm.py` |
| `rlm_memory_env.py` | — | `hexis_rlm.py`, `rlm_repl.py`, `slow_ingest_rlm.py` |
| `slow_ingest_rlm.py` | `hexis_rlm.py`, `ingest/`, `rlm_memory_env.py`, `rlm_repl.py` | — |
| `skill_runtime.py` | — | `agent.py`, `skill_improvement.py` |
| `skill_improvement.py` | `skill_runtime.py` | `worker_service.py` |
| `connector_cognition.py` | — | `ambient_responsibilities.py`, `worker_service.py` |
| `gmail_backfill.py` | — | `ambient_responsibilities.py`, `worker_service.py` |
| `channel_worker.py` | — | `channel_backfill.py` |
| `external_calls.py` | `heartbeat_prompt.py` | `heartbeat_runner.py`, `worker_service.py` |
| `approval_slack_actions.py` | — | `operator_approval.py` |
| `voice_notes.py` | — | `speech.py` |
| `prompt_resources.py` | — | `agent.py`, `chat.py`(间接), `recmem.py`, `reconsolidation.py`, `skill_improvement.py`, `subconscious.py`, `summarization.py`, `contradictions.py` |
