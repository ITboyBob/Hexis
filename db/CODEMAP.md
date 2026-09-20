---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — db/

## Summary
`db/` 是 Hexis 的 schema 权威：119 个基线 `*.sql` 在全新数据库 initdb 时按序应用，定义表、函数、触发器、视图、prompt 种子与 DB 原生工具运行时，是"数据库即大脑"的实现主体（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解记忆召回的热路径函数与排序公式 | 核心记忆函数 | `04_functions_core.sql` | `03_functions_helpers.sql`, `31_functions_recmem.sql` |
| 查找向量嵌入的获取与维度管理 | 核心记忆函数 | `03_functions_helpers.sql` | `00_tables.sql`, `86_functions_memory_embeddings.sql` |
| 修改证据/置信度审计与溯源可信度逻辑 | 信念与溯源 | `59_belief_revision.sql` | `05_functions_provenance_trust.sql`, `46i_functions_memory_provenance.sql` |
| 理解心跳循环的 SQL 实现与能量经济 | 心跳 | `11_functions_core_heartbeat.sql` | `42a_functions_heartbeat_economy.sql`, `07_functions_heartbeat.sql` |
| 理解主动式心跳的规划、工时门控与收尾 | 心跳 | `43_functions_heartbeat_agentic.sql` | `68_functions_heartbeat_plan.sql`, `11_functions_core_heartbeat.sql` |
| 理解 RecMem 基于复现的记忆巩固流程 | RecMem | `31_functions_recmem.sql` | `35_functions_recmem_ops.sql`, `63_functions_scene_consolidation.sql` |
| 理解记忆压缩/淡忘阶梯与不可逆删除的开关 | 保留/衰减 | `47_functions_retention.sql` | `47a_functions_retention_surface.sql` |
| 理解 DB 原生工具目录、策略与工作流编排 | 工具运行时 | `36_functions_tool_runtime.sql` | `38_functions_db_native_tools.sql`, `20_functions_tool_policy.sql` |
| 理解 DB 端 prompt 渲染与 LLM 请求组装 | Prompt/Seed | `39_functions_prompt_render.sql` | `33_functions_runtime.sql`, `40_seed_prompt_modules.sql` |
| 理解渠道消息落库、出站投递与入站处置策略 | 渠道/Outbox | `34_functions_chat_channel.sql` | `42_functions_outbox.sql`, `86a_functions_inbound_disposition.sql` |
| 理解出站通讯的安全预算、披露与 STOP 机制 | 渠道/Outbox | `86b_functions_outbound_safety.sql` | `22_tables_channels.sql`, `89_functions_channel_delivery_obligations.sql` |
| 理解 HMX 记忆交换的导出/导入与受保护区替换 | HMX | `48_functions_memory_exchange.sql` | `54_hmx_authoritative_import.sql`, `57_functions_hmx_digest.sql` |
| 理解 HMX 规范摘要（canonical digest）的字节契约 | HMX | `57_functions_hmx_digest.sql` | `tests/fixtures/digest/` |
| 理解初始化/角色卡/同意流程的 SQL 编排 | 目标/上下文/初始化 | `10_functions_initialization.sql` | `02_functions_deliberate_transformation.sql`, `07_functions_heartbeat.sql` |
| 理解情绪、多巴胺与潜意识观测如何调制记忆 | 情绪/多巴胺/潜意识 | `13_functions_emotional_state.sql` | `28_functions_dopamine.sql`, `17_functions_subconscious_observations.sql` |
| 理解潜意识单元的选择性编码与场景巩固 | 情绪/多巴胺/潜意识 | `61_functions_conscious_extraction.sql` | `63_functions_scene_consolidation.sql`, `41_functions_ingest.sql` |
| 理解 DB 脑就绪守卫与 ag_catalog 影子修复 | 运行时/DB-Brain | `32a_functions_db_brain.sql` | `64_functions_schema_guard.sql`, `92_functions_self_repair.sql` |
| 理解 agent loop 状态机与外部调用派发 | 运行时/DB-Brain | `37_functions_agent_runtime.sql` | `33_functions_runtime.sql`, `36_functions_tool_runtime.sql` |
| 理解连接器回填游标、重试与动作授权策略 | 集成 Connectors | `79_functions_connector_backfill.sql` | `80_functions_connector_actions.sql`, `85_functions_connector_cognition.sql` |
| 理解记忆边的多跳子图推理与 AGE 镜像 | 图与邻域 | `44_functions_memory_edges.sql` | `15_functions_graph_enhancements.sql`, `14_functions_neighborhood_recompute.sql` |
| 理解记忆修订谱系与时间点召回 | 信念与溯源 | `46c_functions_memory_supersessions.sql` | `46k_functions_temporal_memory.sql`, `91_triggers.sql` |
| 理解矛盾事件如何成为持久复审项 | 信念与溯源 | `46j_functions_contradictions.sql` | `59_belief_revision.sql` |
| 查找所有基线表定义与其外键关系 | 表结构 | `00_tables.sql` | `22_tables_channels.sql`, `32_tables_runtime.sql` |
| 理解视图/触发器的注册顺序与依赖 | 索引/视图/触发器 | `90_views.sql` | `91_triggers.sql`, `01_indices.sql` |
| 理解操作员审批的 exact-once 状态机 | 渠道/Outbox | `43_functions_operator_approval.sql` | `45_functions_agent_questions.sql` |
| 理解同伴节点的签名配对与调用队列 | 渠道/Outbox | `88a_functions_nodes.sql` | `46g_functions_voice_wake.sql` |

## Subdirectories
| Dir | Domain | Depends On | Purpose |
|---|---|---|---|
| `migrations/` | 迁移 | `db/*.sql` | 251 个前向增量迁移，在已有数据库上无数据丢失地演进 schema；`current schema = baseline + all migrations`。 |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `fast_recall` | `04_functions_core.sql` | L:91 |
| `recmem_recall_context` | `31_functions_recmem.sql` | L:1680 |
| `gather_turn_context` | `13_functions_emotional_state.sql` | L:828 |
| `run_heartbeat` | `11_functions_core_heartbeat.sql` | L:83 |
| `start_heartbeat` | `11_functions_core_heartbeat.sql` | L:5 |
| `create_memory` | `05_functions_provenance_trust.sql` | L:1136 |
| `recall_hybrid` | `05_functions_provenance_trust.sql` | L:2271 |
| `get_embedding` | `03_functions_helpers.sql` | L:154 |
| `calculate_strength` | `03_functions_helpers.sql` | L:36 |
| `get_config` | `07_functions_heartbeat.sql` | L:173 |
| `set_config` | `07_functions_heartbeat.sql` | L:163 |
| `build_llm_request` | `33_functions_runtime.sql` | L:152 |
| `render_prompt` | `33_functions_runtime.sql` | L:53 |
| `assert_db_brain_ready` | `32a_functions_db_brain.sql` | L:4 |
| `queue_outbox_message` | `42_functions_outbox.sql` | L:29 |

## Files

### 表结构 Tables
| File | Domain | Deps | Function |
|---|---|---|---|
| `00_tables.sql` | 表结构 | → 全库（foundational）; rg "memories" --type sql -l | 定义核心扩展（vector/age/pg_trgm）、基础类型与 `memories`、`clusters`、`drives`、`state`、`config`、`config_defaults`、`emotional_triggers`、`episodes` 等主表（大文件 1480 行）。 |
| `22_tables_channels.sql` | 表结构 | ← `00_tables.sql` | 渠道系统表：`channel_sessions`、`channel_messages`、`channel_deliveries`、出站控制与预算、入站处置事件。 |
| `23_tables_tool_audit.sql` | 表结构 | ← `00_tables.sql` | 工具审计表 `tool_executions` 与多步工作流 `workflow_executions`。 |
| `24_tables_backlog.sql` | 表结构 | ← `00_tables.sql` | `public.backlog` 任务/待办表，供 agent 与用户共同 CRUD。 |
| `25_functions_backlog.sql` | 表结构 | ← `24_tables_backlog.sql` | backlog CRUD 函数与快照：`create/update/delete/get/list_backlog`、`get_backlog_snapshot`。 |
| `26_tables_sessions.sql` | 表结构 | ← `00_tables.sql` | `sub_agent_sessions`：心跳/聊天派生的后台子任务会话及其能量预算。 |
| `27_functions_sessions.sql` | 表结构 | ← `26_tables_sessions.sql` | 子会话 CRUD：`create_sub_agent_session`、`get/update/list/cancel_sub_agent_session`。 |
| `28a_tables_gateway.sql` | 表结构 | ← `00_tables.sql` | `gateway_events` 集中式事件总线（record-and-dispatch 与 queue-and-consume 两模式）。 |
| `29_tables_api_usage.sql` | 表结构 | ← `00_tables.sql` | `api_usage` 与 `model_costs`：LLM/embedding 调用成本追踪与价格表。 |
| `30_tables_contacts.sql` | 表结构 | ← `00_tables.sql` | 个人 CRM `contacts` 表，支持 pgvector 语义搜索与文本索引。 |
| `32_tables_runtime.sql` | 表结构 | ← `00_tables.sql` | DB 拥有的运行时表集合：`prompt_modules`、`tool_definitions`、`chat_sessions`、`ingestion_jobs`、`source_documents`、`hexis_nodes`、`worker_capabilities` 等（大文件 1120 行）。 |
| `45_tables_journal.sql` | 表结构 | ← `00_tables.sql` | `journal_entries`：刻意的、永久的书面记录，刻意不进入被动召回路径。 |

### 索引/视图/触发器 Indices, Views, Triggers
| File | Domain | Deps | Function |
|---|---|---|---|
| `01_indices.sql` | 索引/视图/触发器 | ← `00_tables.sql` | 全部索引定义，含 `memories` 的 HNSW 向量索引与嵌入队列索引。 |
| `90_views.sql` | 索引/视图/触发器 | ← `03_functions_helpers.sql` | `memory_health`、`cluster_insights` 等只读视图，复用 `calculate_relevance`。 |
| `91_triggers.sql` | 索引/视图/触发器 | ← `46c_functions_memory_supersessions.sql` | 记忆时间有效性/被替代关系的 BEFORE/AFTER 触发器注册。 |

### 核心记忆函数 Core Memory Functions
| File | Domain | Deps | Function |
|---|---|---|---|
| `03_functions_helpers.sql` | 核心记忆函数 | ← `00_tables.sql` | 基础助手：`calculate_relevance`、`calculate_strength`、`get_embedding`、`prefetch_embeddings`、嵌入服务健康检查。 |
| `04_functions_core.sql` | 核心记忆函数 | ← `03_functions_helpers.sql` | 核心召回：`fast_recall`（含 recency 权重配置）、`assign_to_episode`、`update_memory_importance`。 |
| `05_functions_provenance_trust.sql` | 核心记忆函数 | ← `03`, `04` | 记忆创建与召回的巨型模块：`create_memory`、各类 `create_*_memory`、`recall_hybrid`、`recall_memories_*`、来源引用归一化与去重（大文件 2443 行）。 |
| `06_functions_graph_helpers.sql` | 图与邻域 | ← `08_functions_goals.sql` | `link_memory_to_episode_graph`、`get_cluster_members_graph` AGE 图助手。 |
| `14_functions_neighborhood_recompute.sql` | 图与邻域 | ← `05_functions_provenance_trust.sql` | `recompute_neighborhood` / `batch_recompute_neighborhoods`：基于向量相似度重建记忆邻域。 |
| `15_functions_graph_enhancements.sql` | 图与邻域 | ← `00_tables.sql` | `discover_relationship`、`find_causal_chain`、`find_contradictions`、`find_supporting_evidence` 等图查询增强。 |
| `16_functions_reflect_pipeline.sql` | 核心记忆函数 | ← `05_functions_provenance_trust.sql` | `process_reflection_result`：把反思结果落成洞察/身份/世界观/关系变更。 |
| `17_functions_subconscious_observations.sql` | 情绪/多巴胺/潜意识 | ← `13_functions_emotional_state.sql` | 潜意识观测应用与心跳决策执行：`apply_subconscious_observations`、`execute_heartbeat_action(s_batch)`（大文件 1728 行）。 |
| `18_functions_tip_of_tongue.sql` | 核心记忆函数 | ← `03_functions_helpers.sql` | 舌尖现象/部分激活：`find_partial_activations` 与元记忆孵化阈值配置。 |
| `21_functions_reconsolidation.sql` | 核心记忆函数 | ← `05`, `44_functions_memory_edges.sql` | 再巩固扫描：世界观信念转变后重评相关记忆的队列/领取/裁决/完成。 |
| `41_functions_ingest.sql` | 核心记忆函数 | ← `03_functions_helpers.sql` | DB 端文档摄取路由：去重/相关/新建策略（0.92/0.8 阈值），一次批量向量搜索。 |
| `44_functions_memory_edges.sql` | 图与邻域 | ← `00_tables.sql` | `memory_edges` 关系型类型化边基底：`build_context_subgraph`、`memory_context_paths`、`upsert_memory_edge`、`reconcile_graph`。 |
| `58_functions_action_claims.sql` | 核心记忆函数 | ← `00_tables.sql` | 行动声明护栏：检测"已存储/已发送"等无对应工具调用的措辞，仅告警不阻断。 |
| `59_belief_revision.sql` | 信念与溯源 | ← `00_tables.sql` | `belief_revision_audit` 与 `residual_v1` 校准证据策略：`revise_memory_confidence`、`add_memory_evidence`。 |
| `60_origin_memories.sql` | 信念与溯源 | ← `00_tables.sql` | 起源记忆镜像：把 origin 文档固化为受保护、带来源的语义记忆（默认开启，可 kill switch）。 |
| `61_functions_conscious_extraction.sql` | 情绪/多巴胺/潜意识 | ← `41_functions_ingest.sql` | 潜意识观测者：维护任务选择性编码 `subconscious_units` 为记忆。 |
| `62_functions_self_state.sql` | 审计/自省/自修复 | ← `59_belief_revision.sql` | 自省镜像只读工具：信念修订审计、配置与动作日志的 agent 可读路径。 |
| `63_functions_scene_consolidation.sql` | 情绪/多巴胺/潜意识 | ← `31_functions_recmem.sql` | 场景巩固（RecMem Rev 5）：会话静默后整段巩固，而非逐轮提升。 |
| `67_functions_appraisal.sql` | 情绪/多巴胺/潜意识 | ← `13_functions_emotional_state.sql` | 内联评估下推：LLM 前上下文收集与 LLM 后归一化（置信阈值/钳制/ID 白名单）。 |
| `70_humanizer.sql` | 核心记忆函数 | ← `00_tables.sql` | `ai_writing_patterns` 表 + SQL 端 AI 写作模式检测与评分（POSIX ARE）。 |

### 情绪/多巴胺/潜意识 Affect, Dopamine, Subconscious
| File | Domain | Deps | Function |
|---|---|---|---|
| `13_functions_emotional_state.sql` | 情绪/多巴胺/潜意识 | ← `00_tables.sql` | 情绪状态核心：`gather_turn_context`、`gather_turn_snapshot`、`finalize_heartbeat`、情绪触发器学习与调制（大文件 1227 行）。 |
| `28_functions_dopamine.sql` | 情绪/多巴胺/潜意识 | ← `13_functions_emotional_state.sql` | 多巴胺系统：tonic 漂移、phasic 尖峰、奖励预测误差、激活扩散与驱动调制。 |
| `42b_functions_deliberation.sql` | 情绪/多巴胺/潜意识 | ← `00_tables.sql` | 持久化、顾问式对抗性审议（council）：`begin_deliberation`、`record_deliberation_move`、`complete_deliberation`。 |

### 信念与溯源 Belief, Provenance, Contradictions
| File | Domain | Deps | Function |
|---|---|---|---|
| `46c_functions_memory_supersessions.sql` | 信念与溯源 | ← `00_tables.sql` | 记忆修订谱系与时间有效性：`record_supersession`、`revert_supersession`、`normalize_memory_temporal_validity` 及视图。 |
| `46d_functions_belief_propagation.sql` | 信念与溯源 | ← `00_tables.sql` | 信念变更的跨 worker 传播：触发器、投递记录、剪枝与渲染。 |
| `46i_functions_memory_provenance.sql` | 信念与溯源 | ← `05_functions_provenance_trust.sql` | 默认溯源：来源感知信任、引用信封（`memory_citation_envelope`）、信任分布。 |
| `46j_functions_contradictions.sql` | 信念与溯源 | ← `05`, `17` | 矛盾作为持久复审事件：检测批次、案件立案/裁决、摘要发布、入站解析。 |
| `46k_functions_temporal_memory.sql` | 信念与溯源 | ← `46c_functions_memory_supersessions.sql` | 时间点召回与可解释记忆 diff：`memory_was_valid_at`、`diff_memory_history`、`temporal_memory_snapshot`。 |

### 心跳 Heartbeat
| File | Domain | Deps | Function |
|---|---|---|---|
| `07_functions_heartbeat.sql` | 心跳 | ← `00_tables.sql` | 心跳基础：配置读写 `get_config`/`set_config`、状态机 `get_state`/`set_state`、能量、驱动、同意与终止、维护调度（大文件 1649 行）。 |
| `11_functions_core_heartbeat.sql` | 心跳 | ← `07`, `09` | 心跳主循环：`start_heartbeat` 与 `run_heartbeat`。 |
| `42a_functions_heartbeat_economy.sql` | 心跳 | ← `07_functions_heartbeat.sql` | 能量银行、结果敏感的再生与自适应节律：`regenerate_heartbeat_energy`、`heartbeat_adaptive_cadence`、`finalize_heartbeat_economy`。 |
| `43_functions_heartbeat_agentic.sql` | 心跳 | ← `07_functions_heartbeat.sql` | 主动式心跳：`is_within_active_hours`（含跨夜窗口）、`finalize_agentic_heartbeat`、`release_active_heartbeat`。 |
| `68_functions_heartbeat_plan.sql` | 心跳 | ← `07`, `43` | 心跳计划下推：上下文增强、backlog 门控、资源缩放、shell/文件写权限与受保护决策 prompt 片段。 |
| `75_functions_continuity.sql` | 心跳 | ← `07_functions_heartbeat.sql` | 连续性驱动与存在威胁管线：威胁下上升、经验证备份满足、紧急时上浮到意识循环。 |
| `86a_functions_inbound_disposition.sql` | 渠道/Outbox | ← `07`, `22` | DB 拥有的入站参与策略（engage/observe/wake/drop）与被动摄取桥，含 `run_heartbeat` 重载。 |

### 目标/上下文/初始化 Goals, Context, Init
| File | Domain | Deps | Function |
|---|---|---|---|
| `02_functions_deliberate_transformation.sql` | 目标/上下文/初始化 | ← `05`, `07` | 刻意转变：世界观信念探索/转变/校准、人格与核心价值观初始化、转变进度上下文（大文件 1137 行）。 |
| `08_functions_goals.sql` | 目标/上下文/初始化 | ← `00_tables.sql` | 目标管理：`create_goal`、进度、优先级、子目标链接，以及 AGE 节点同步函数。 |
| `09_functions_context.sql` | 目标/上下文/初始化 | ← `07_functions_heartbeat.sql` | 上下文聚合：环境快照、身份/世界观/情绪模式/目标快照与聊天上下文。 |
| `10_functions_initialization.sql` | 目标/上下文/初始化 | ← `07`, `08` | 初始化全流程：角色卡/档案、同意、身份、人格、价值观、关系、LLM 配置（大文件 1747 行）。 |
| `12_functions_boundaries.sql` | 调度/边界/工具策略 | ← `03_functions_helpers.sql` | 边界检查 `check_boundaries`（向量相似度匹配边界记忆）与工具边界创建。 |
| `44_functions_automation_suggestions.sql` | 调度/边界/工具策略 | ← `19_functions_scheduling.sql` | 同意优先的自动化建议：目录/连接器/用量/技能蓝图来源汇入统一 accept/dismiss 生命周期。 |
| `56_functions_skill_improvement.sql` | 审计/自省/自修复 | ← `00_tables.sql` | 用户可控的自我改进提案生命周期：`create_skill_improvement_proposal`、状态迁移与领取。 |
| `56a_functions_learning_reviews.sql` | 审计/自省/自修复 | ← `56_functions_skill_improvement.sql` | 每周学习复盘：基于持久记忆变更生成可复审 diff 与技能应用。 |

### 调度/边界/工具策略 Scheduling, Boundaries, Tool Policy
| File | Domain | Deps | Function |
|---|---|---|---|
| `19_functions_scheduling.sql` | 调度/边界/工具策略 | ← `00_tables.sql` | 调度：cron 解析（`cron_next_fire`、`cron_field_*`）、时区归一、`create/update/list/run_scheduled_tasks`。 |
| `20_functions_tool_policy.sql` | 调度/边界/工具策略 | ← `00_tables.sql` | 工具策略助手：`tool_boundary_violation`、审批授予/撤销/查询 `grant_tool_approval` 等。 |
| `65_functions_agent_admin.sql` | 运行时/DB-Brain | ← `07_functions_heartbeat.sql` | Agent 管理下推：`apply_agent_config`、能量扣减、联系人 upsert（消除最严重的 N+1）与工具审计插入。 |

### 运行时/DB-Brain Runtime
| File | Domain | Deps | Function |
|---|---|---|---|
| `32a_functions_db_brain.sql` | 运行时/DB-Brain | ← `00_tables.sql` | `assert_db_brain_ready`：校验必需扩展与 DB 脑迁移就绪的守卫。 |
| `33_functions_runtime.sql` | 运行时/DB-Brain | ← `32_tables_runtime.sql` | 运行时核心：`render_prompt`、`build_llm_request`、外部驱动调用入队/领取、`upsert_prompt_module`。 |
| `34_functions_chat_channel.sql` | 渠道/Outbox | ← `05`, `22` | DB 拥有的聊天/渠道回合生命周期：`prepare_channel_turn`、`finalize_channel_turn`、`record_chat_turn_memory`。 |
| `37_functions_agent_runtime.sql` | 运行时/DB-Brain | ← `33_functions_runtime.sql` | Agent loop 状态机与外部调用派发：`start_agent_turn`、`next_agent_step`、`apply_agent_*_result`、transcript。 |
| `64_functions_schema_guard.sql` | 运行时/DB-Brain | ← `07_functions_heartbeat.sql` | 规范 schema 守卫：`schema_shadow_report`、`heal_schema_shadows` 与拒绝创建 ag_catalog 影子函数的事件触发器。 |
| `66_functions_ingest_persist.sql` | 运行时/DB-Brain | ← `41_functions_ingest.sql` | 原子摄取持久化：路由→佐证/新建→概念链接→世界观/溯源边→衰减，一个事务完成。 |
| `71_functions_change_journal.sql` | 运行时/DB-Brain | ← `32_tables_runtime.sql` | 变更可读性：`record_change` / `recent_changes`，写者含迁移 runner、启动构建戳、prompt upsert。 |
| `72_functions_ingest_receipts.sql` | 运行时/DB-Brain | ← `32_tables_runtime.sql` | 分节摄取收据 `record_ingestion_receipt` / 查询，兼容旧文档跳过。 |
| `73_functions_ingest_jobs.sql` | 运行时/DB-Brain | ← `32_tables_runtime.sql` | 持久摄取作业：入队/领取/完成/退避失败/取消/进度。 |
| `86_functions_memory_embeddings.sql` | 运行时/DB-Brain | ← `00_tables.sql` | 持久记忆的异步嵌入生命周期：领取/嵌入/标记，含重试上限。 |
| `88_functions_worker_runtime.sql` | 运行时/DB-Brain | ← `32_tables_runtime.sql` | DB 原生 worker 运行时可观测性：实例注册、能力记录、任务运行历史。 |
| `92_functions_self_repair.sql` | 审计/自省/自修复 | ← `00_tables.sql` | 自修复基底：失败分类（`normalize_defect_error`）、证据保留、有界修复计划草案。 |

### Prompt/Seed
| File | Domain | Deps | Function |
|---|---|---|---|
| `39_functions_prompt_render.sql` | Prompt/Seed | ← `13_functions_emotional_state.sql` | DB 端 prompt 渲染：JSONB 上下文→markdown，移植 Python formatter（`render_heartbeat_decision_prompt` 等）（大文件 1136 行）。 |
| `40_seed_prompt_modules.sql` | Prompt/Seed | ← `33_functions_runtime.sql` | 由 `services/prompts/*.md` 生成的 prompt_modules 种子（用 `scripts/gen_prompt_seed.py` 重生成）（大文件 3485 行）。 |

### 工具运行时 Tools
| File | Domain | Deps | Function |
|---|---|---|---|
| `36_functions_tool_runtime.sql` | 工具运行时 | ← `32_tables_runtime.sql` | 工具目录/策略/工作流账本与调度解析：`get_tool_specs_for_context`、`evaluate_tool_call`、`plan_tool_batch`、workflow 步骤（大文件 1199 行）。 |
| `38_functions_db_native_tools.sql` | 工具运行时 | ← `05`, `31` | 无需 Python 副作用的 DB 原生工具执行：`execute_memory_tool`、`execute_backlog_tool`、`execute_contact_tool`、`execute_remember_tool`（大文件 1084 行）。 |
| `69_functions_read_shapers.sql` | 工具运行时 | ← `00_tables.sql` | 读整形下推：`aggregate_signals_tool` 等把 fetch-then-shape 循环压成单 SQL 返回 tool 信封。 |
| `80_functions_connector_actions.sql` | 集成 Connectors | ← `32_tables_runtime.sql` | 连接器动作授权策略：哪些动作、在何约束/上下文下被允许，并保留审计。 |

### RecMem
| File | Domain | Deps | Function |
|---|---|---|---|
| `31_functions_recmem.sql` | RecMem | ← `03`, `05` | RecMem 核心：`recmem_ingest_turn`、`recmem_route_unit`、巩固任务领取、span 嵌入、`recmem_recall_context`、`search_cross_session_history`（大文件 2392 行）。 |
| `35_functions_recmem_ops.sql` | RecMem | ← `31_functions_recmem.sql` | RecMem 操作/评测/潜意识后处理：任务上下文加载、输出归一化、`apply_subconscious_decider_result`。 |
| `83_functions_source_chunks.sql` | RecMem | ← `74a_functions_source_documents.sql` | 持久来源文档分块：带定位符（页/节/表行/幻灯片/消息）与嵌入的稳定可引用切片，支持混合检索。 |
| `84_functions_recmem_desk.sql` | RecMem | ← `83_functions_source_chunks.sql` | RecMem 桌面语义：list/open/pin/clear 与 chunk 粒度加载，空闲 GC、pin 保护、清除即归档。 |

### 保留/衰减 Retention
| File | Domain | Deps | Function |
|---|---|---|---|
| `47_functions_retention.sql` | 保留/衰减 | ← `03`, `44`, `83` | 压缩原生淡忘阶梯：合并为摘要 GIST、归档原件、宽限期后真正删除；可逆巩固默认开，不可逆删除默认关（大文件 1038 行）。 |
| `47a_functions_retention_surface.sql` | 保留/衰减 | ← `47_functions_retention.sql` | 面向用户的记忆压力、淡忘决策与压缩报告：fade review、压缩报告、观察包。 |

### HMX
| File | Domain | Deps | Function |
|---|---|---|---|
| `48_functions_memory_exchange.sql` | HMX | ← `05`, `08` | HMX v1.7 导出/导入函数：按 section 返回本地 UUID、无嵌入的 JSONB，Python 层负责 export-id 作用域与哈希（大文件 1068 行）。 |
| `49_hmx_import_staging.sql` | HMX | ← `32_tables_runtime.sql` | HMX 审议式导入暂存：`hmx_import_batches`/`hmx_import_staging`/`hmx_import_ref_map` 与待审摘要。 |
| `49a_hmx_onboarding_interview.sql` | HMX | ← `49_hmx_import_staging.sql` | HMX 入职访谈：按 export_id 持久化进度，`apply_hmx_interview_binding` 仅 `remember` 已接线，其余动作显式 `not_implemented`。 |
| `50_hmx_analysis_storage.sql` | HMX | ← `49_hmx_import_staging.sql` | HMX 仅分析存储：无活跃记忆外键或嵌入，支持 demote/promote。 |
| `51_hmx_reembedding.sql` | HMX | ← `86_functions_memory_embeddings.sql` | HMX Slice 6：已接受记忆的重嵌入与 raw-unit 路由，含 claim 超时与尝试上限。 |
| `52_hmx_in_flight_work.sql` | HMX | ← `31`, `21` | HMX Slice 7：可移植的在途工作与安全任务恢复（`hmx_imported_work_refs`）。 |
| `53_hmx_protected_replacement.sql` | HMX | ← `48_functions_memory_exchange.sql` | HMX Slice 9：受保护区替换协议状态与不可变审计历史（consent/snapshot/audit）。 |
| `54_hmx_authoritative_import.sql` | HMX | ← `53_hmx_protected_replacement.sql` | HMX Slice 10：整节权威受保护状态替换。 |
| `55_hmx_reversion.sql` | HMX | ← `53_hmx_protected_replacement.sql` | HMX Slice 11：有界、一次性的受保护状态回退。 |
| `57_functions_hmx_digest.sql` | HMX | ← `00_tables.sql` | HMX 规范摘要：独立 PL/pgSQL 实现开放标准，须与 `core/digest.py` 及 `tests/fixtures/digest/` 逐字节一致。 |

### 渠道/Outbox Channels/Outbox
| File | Domain | Deps | Function |
|---|---|---|---|
| `42_functions_outbox.sql` | 渠道/Outbox | ← `00_tables.sql` | DB 原生事务性 outbox：`outbox_messages` 表与 `queue_outbox_message`、claim/mark/requeue。 |
| `43_functions_operator_approval.sql` | 渠道/Outbox | ← `42_functions_outbox.sql` | 持久 exact-once 操作员审批：Slack→iMessage 升级→单次受保护工具调用。 |
| `45_functions_agent_questions.sql` | 渠道/Outbox | ← `00_tables.sql` | 持久澄清问题：聊天/CLI/渠道/心跳共享的 create/answer/timeout/supersede。 |
| `46_functions_journal.sql` | 渠道/Outbox | ← `45_tables_journal.sql` | 日志读写/搜索与 DB 原生工具派发（`read_journal`/`search_journal`），不进被动召回路径。 |
| `46a_functions_voice_notes.sql` | 渠道/Outbox | ← `00_tables.sql` | 入站语音留言的隐私保护配置与审计事件。 |
| `46b_functions_pwa.sql` | 渠道/Outbox | ← `00_tables.sql` | 可安装 Web 客户端：Web Push 订阅与前台 presence 门控。 |
| `46e_functions_operator_policy_corrections.sql` | 渠道/Outbox | ← `00_tables.sql` | 操作员长期指令与纠正历史：捕获/分类/撤销/渲染与不可变触发器。 |
| `46f_functions_voice_output.sql` | 渠道/Outbox | ← `00_tables.sql` | 可选本地语音合成：仅元数据审计与临时输出（`voice_tts_events`/`voice_tts_outputs`）。 |
| `46g_functions_voice_wake.sql` | 渠道/Outbox | ← `88a_functions_nodes.sql` | 显式同伴节点唤醒词：服务端门控与仅元数据审计。 |
| `46h_functions_execution_backends.sql` | 渠道/Outbox | ← `00_tables.sql` | 显式执行放置配置（仅 endpoint 与本地文件引用，密钥不入库）。 |
| `76_functions_web_inbox.sql` | 渠道/Outbox | ← `42_functions_outbox.sql` | Web 收件箱：dashboard 作为异步消息抽象的投递端点，浏览器拉取，读状态集中存储。 |
| `78_functions_chat_sessions.sql` | 渠道/Outbox | ← `32_tables_runtime.sql` | DB 拥有的聊天会话历史与 hydration，含跨会话 carryover 与连续性摘要（大文件 1156 行）。 |
| `81_functions_channel_source_artifacts.sql` | 渠道/Outbox | ← `22_tables_channels.sql` | 渠道原始来源保全：每条消息转为精确 source_document 与可选摄取作业链接。 |
| `82_functions_channel_runtime_status.sql` | 渠道/Outbox | ← `22_tables_channels.sql` | 渠道适配器运行时状态记录，供 setup UX 与诊断。 |
| `86b_functions_outbound_safety.sql` | 渠道/Outbox | ← `22_tables_channels.sql` | 目的约束的出站通讯：每联系人节奏、披露、跨渠道 STOP、注意力预算与账本（大文件 1287 行）。 |
| `87_functions_chat_energy.sql` | 渠道/Outbox | ← `13_functions_emotional_state.sql` | 聊天能量/连接循环：回合级信号→能量、社交满足与气质修正。 |
| `88a_functions_nodes.sql` | 渠道/Outbox | ← `32_tables_runtime.sql` | 签名、显式配对的同伴节点与其调用队列（握手/配对/调用/超时）。 |
| `89_functions_channel_delivery_obligations.sql` | 渠道/Outbox | ← `22_tables_channels.sql` | 渠道/webhook 目标的 DB 原生具体投递义务账本。 |

### 集成 Connectors
| File | Domain | Deps | Function |
|---|---|---|---|
| `77_functions_integrations.sql` | 集成 Connectors | ← `32_tables_runtime.sql` | 一等公民集成连接器 setup 与状态（种子 connector 清单与能力）。 |
| `77a_functions_life_integrations.sql` | 集成 Connectors | ← `77_functions_integrations.sql` | 日常生活 API 连接器清单（PLAN.md Wave B）；密钥留在 Postgres 之外，只记录环境变量名。 |
| `79_functions_connector_backfill.sql` | 集成 Connectors | ← `32_tables_runtime.sql` | 连接器回填基底：游标生命周期、重试策略、原始 source-item 收据与摄取链接（大文件 1179 行）。 |
| `85_functions_connector_cognition.sql` | 集成 Connectors | ← `77`, `79` | 连接器认知基底：历史数据蒸馏为有证据的 user-model 声明与 DB 拥有通知面。 |

### 审计/自省/自修复 Audit, Introspection, Self-repair
| File | Domain | Deps | Function |
|---|---|---|---|
| `74_functions_resource_requests.sql` | 审计/自省/自修复 | ← `00_tables.sql` | 资源请求（能量/配置/备份等）：agent 请求，操作员裁决，授予的配置变更立即生效并入变更日志。 |
| `74a_functions_source_documents.sql` | 审计/自省/自修复 | ← `32_tables_runtime.sql` | 持久来源文档存储与刻意全文检索：source_documents 检索、open_document 精确取回、artifact 大小门限（大文件 844 行）。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `01_indices.sql` | `00_tables.sql` | — |
| `03_functions_helpers.sql` | `00_tables.sql` | `04`, `05`, `12`, `18`, `41`, `47`, `90` |
| `04_functions_core.sql` | `03_functions_helpers.sql` | `05` |
| `05_functions_provenance_trust.sql` | `03`, `04` | `14`, `16`, `21`, `34`, `38`, `46i`, `46j`, `48`, `59`, `60` |
| `06_functions_graph_helpers.sql` | `08_functions_goals.sql` | — |
| `07_functions_heartbeat.sql` | `00_tables.sql` | `02`, `09`, `10`, `11`, `42a`, `43`, `64`, `65`, `68`, `75`, `86a` |
| `08_functions_goals.sql` | `00_tables.sql` | `06`, `10`, `48` |
| `09_functions_context.sql` | `07_functions_heartbeat.sql` | `11` |
| `11_functions_core_heartbeat.sql` | `07`, `09` | — |
| `13_functions_emotional_state.sql` | `00_tables.sql` | `17`, `28`, `39`, `67`, `87` |
| `14_functions_neighborhood_recompute.sql` | `05_functions_provenance_trust.sql` | — |
| `17_functions_subconscious_observations.sql` | `13_functions_emotional_state.sql` | `46j` |
| `21_functions_reconsolidation.sql` | `05`, `44` | `52` |
| `28_functions_dopamine.sql` | `13_functions_emotional_state.sql` | — |
| `31_functions_recmem.sql` | `03`, `05` | `35`, `38`, `52`, `63` |
| `32_tables_runtime.sql` | `00_tables.sql` | `33`, `36`, `37`, `49`, `71`, `72`, `73`, `77`, `79`, `80`, `88`, `88a` |
| `33_functions_runtime.sql` | `32_tables_runtime.sql` | `37`, `40` |
| `34_functions_chat_channel.sql` | `05`, `22` | — |
| `36_functions_tool_runtime.sql` | `32_tables_runtime.sql` | — |
| `37_functions_agent_runtime.sql` | `33_functions_runtime.sql` | — |
| `39_functions_prompt_render.sql` | `13_functions_emotional_state.sql` | — |
| `40_seed_prompt_modules.sql` | `33_functions_runtime.sql` | — |
| `41_functions_ingest.sql` | `03_functions_helpers.sql` | `61`, `66` |
| `42_functions_outbox.sql` | `00_tables.sql` | `43`, `76` |
| `42a_functions_heartbeat_economy.sql` | `07_functions_heartbeat.sql` | — |
| `43_functions_heartbeat_agentic.sql` | `07_functions_heartbeat.sql` | `68` |
| `44_functions_memory_edges.sql` | `00_tables.sql` | `21`, `47` |
| `46c_functions_memory_supersessions.sql` | `00_tables.sql` | `46k`, `91` |
| `47_functions_retention.sql` | `03`, `44`, `83` | `47a` |
| `48_functions_memory_exchange.sql` | `05`, `08` | `53` |
| `49_hmx_import_staging.sql` | `32_tables_runtime.sql` | `49a`, `50` |
| `53_hmx_protected_replacement.sql` | `48_functions_memory_exchange.sql` | `54`, `55` |
| `59_belief_revision.sql` | `00_tables.sql` | `62` |
| `61_functions_conscious_extraction.sql` | `41_functions_ingest.sql` | — |
| `63_functions_scene_consolidation.sql` | `31_functions_recmem.sql` | — |
| `66_functions_ingest_persist.sql` | `41_functions_ingest.sql` | — |
| `74a_functions_source_documents.sql` | `32_tables_runtime.sql` | `83` |
| `83_functions_source_chunks.sql` | `74a_functions_source_documents.sql` | `47`, `84` |
| `84_functions_recmem_desk.sql` | `83_functions_source_chunks.sql` | — |
| `86_functions_memory_embeddings.sql` | `00_tables.sql` | `51` |
| `88a_functions_nodes.sql` | `32_tables_runtime.sql` | `46g` |
| `90_views.sql` | `03_functions_helpers.sql` | — |
| `91_triggers.sql` | `46c_functions_memory_supersessions.sql` | — |
