---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — tests/db/

## Summary
`tests/db/` 是 129 个针对 DB 层（`db/*.sql` schema、函数、触发器、迁移）的回归与不变量测试模块，通过真实 Postgres 直接验证 SQL 行为（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解 schema 基线与迁移 runner 的非破坏性证明 | 表/迁移/Schema | `test_migrations.py` | `test_db.py`, `test_runtime_foundation.py` |
| 理解 DB 脑就绪守卫与 schema 影子治理 | 表/迁移/Schema | `test_db_brain_ready.py` | `test_schema_guard.py`, `test_worker_code_schema_skew.py` |
| 理解记忆召回、强度与嵌入不变量 | 记忆核心 | `test_memory_with_embeddings.py` | `test_memory_strength.py`, `test_embedding_index_invariant.py` |
| 理解 RecMem 巩固/桌面/场景与潜意识编码 | RecMem/巩固 | `test_recmem.py` | `test_recmem_desk.py`, `test_scene_consolidation.py` |
| 理解心跳状态机与能量经济 | 心跳/能量/目标 | `test_heartbeat_state.py` | `test_heartbeat_economy.py`, `test_finalize_heartbeat.py` |
| 理解信念修订、矛盾与时间点召回 | 信念/溯源/矛盾 | `test_belief_revision.py` | `test_contradiction_events.py`, `test_temporal_memory_history.py` |
| 理解保留/淡忘与来源文档/分块检索 | 保留/来源文档 | `test_retention_consolidation.py` | `test_source_chunks.py`, `test_source_chunk_search.py` |
| 理解 HMX 导出/导入/受保护替换 | HMX | `test_hmx_export.py` | `test_hmx_import.py`, `test_hmx_protected_replacement.py` |
| 理解渠道、outbox 与入站/出站安全 | 渠道/Outbox | `test_outbox.py` | `test_outbound_safety.py`, `test_inbound_disposition.py` |
| 理解工具运行时、调度与配置同意 | 工具/调度/配置 | `test_tool_runtime.py` | `test_db_native_tools.py`, `test_config_consent.py` |
| 理解连接器回填、动作授权与认知 | 连接器/集成 | `test_connector_backfill.py` | `test_connector_action_policy.py`, `test_cognition_v2.py` |
| 理解情绪/多巴胺/关系节奏调制 | 情绪/多巴胺 | `test_affective_state.py` | `test_dopamine.py`, `test_relationship_pacing.py` |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `temp_test_db` fixture | `../conftest.py` | L:41 |
| `db_pool` fixture | `../conftest.py` | L:116 |
| `apply_repo_migrations` fixture | `../conftest.py` | L:307 |

## Files
> 129 个模块，按被测子系统分组；每组内逐文件列出。

### 表/迁移/Schema
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_db.py` | 表/迁移/Schema | `db/*.sql` | 基线 schema 基础可用性冒烟测试。 |
| `test_migrations.py` | 表/迁移/Schema | `core.migrations` | 迁移 runner 与 HMX Slice 0 的非破坏性演进证明。 |
| `test_db_brain_ready.py` | 表/迁移/Schema | `db/32a` | `assert_db_brain_ready` 守卫。 |
| `test_runtime_foundation.py` | 表/迁移/Schema | `db/32` | 运行时表与 prompt_modules 基础。 |
| `test_schema_guard.py` | 表/迁移/Schema | `db/64` | schema 影子报告与修复。 |
| `test_init_parity.py` | 表/迁移/Schema | `db/10` | 初始化路径一致性。 |
| `test_portable_brain_contract.py` | 表/迁移/Schema | `db/48` | 可移植脑契约。 |
| `test_embedding_index_invariant.py` | 表/迁移/Schema | `db/01` | 向量索引与嵌入状态不变量。 |
| `test_worker_code_schema_skew.py` | 表/迁移/Schema | `db/88` | worker 代码/schema 偏移检测。 |
| `test_stress_fixes.py` | 表/迁移/Schema | `db/*` | 压力修复回归。 |

### 记忆核心
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_memory_with_embeddings.py` | 记忆核心 | `db/05` | 带嵌入记忆创建/检索。 |
| `test_memory_importance.py` | 记忆核心 | `db/04` | 重要性更新。 |
| `test_memory_strength.py` | 记忆核心 | `db/03` | 记忆强度衰减。 |
| `test_memory_edges.py` | 记忆核心 | `db/44` | 类型化记忆边与子图。 |
| `test_memory_triage.py` | 记忆核心 | `db/05` | 记忆分诊。 |
| `test_memory_sensitivity.py` | 记忆核心 | `db/05` | 记忆敏感度。 |
| `test_retrieval_eval.py` | 记忆核心 | `db/05` | 召回质量评测。 |
| `test_working_memory_promotion.py` | 记忆核心 | `db/05` | 工作记忆提升。 |
| `test_delete_memory_fully.py` | 记忆核心 | `db/47` | 完整删除记忆。 |
| `test_sources_trust.py` | 记忆核心 | `db/05` | 来源可信度。 |
| `test_provenance_default.py` | 记忆核心 | `db/46i` | 默认溯源。 |
| `test_cross_session_search.py` | 记忆核心 | `db/31` | 跨会话检索。 |
| `test_narrative_context.py` | 记忆核心 | `db/07` | 叙事上下文。 |
| `test_human_scale_memory.py` | 记忆核心 | `db/05` | 人本尺度记忆。 |
| `test_ingest_routing.py` | 记忆核心 | `db/41` | 摄取路由决策。 |

### RecMem/巩固
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_recmem.py` | RecMem/巩固 | `db/31` | RecMem 摄取/路由/巩固。 |
| `test_recmem_ops.py` | RecMem/巩固 | `db/35` | RecMem 操作与后处理。 |
| `test_recmem_desk.py` | RecMem/巩固 | `db/84` | RecMem 桌面语义。 |
| `test_scene_consolidation.py` | RecMem/巩固 | `db/63` | 场景巩固。 |
| `test_conscious_extraction.py` | RecMem/巩固 | `db/61` | 意识提取选择性编码。 |
| `test_subconscious_maintenance.py` | RecMem/巩固 | `db/07` | 潜意识维护。 |
| `test_subconscious_observations_additional.py` | RecMem/巩固 | `db/17` | 潜意识观测补充。 |
| `test_incubation_loop.py` | RecMem/巩固 | `db/18` | 孵化循环。 |
| `test_deliberate_transformation.py` | RecMem/巩固 | `db/02` | 刻意转变。 |

### 情绪/多巴胺
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_affective_state.py` | 情绪/多巴胺 | `db/13` | 情感状态归一与读写。 |
| `test_emotion_system.py` | 情绪/多巴胺 | `db/13` | 情绪系统。 |
| `test_emotional_state_additional.py` | 情绪/多巴胺 | `db/13` | 情绪状态补充。 |
| `test_emotional_intensity.py` | 情绪/多巴胺 | `db/13` | 情绪强度。 |
| `test_dopamine.py` | 情绪/多巴胺 | `db/28` | 多巴胺尖峰/漂移。 |
| `test_appraisal_depth.py` | 情绪/多巴胺 | `db/67` | 评估深度。 |
| `test_inferred_care.py` | 情绪/多巴胺 | `db/09` | 推断关怀。 |
| `test_relationship_pacing.py` | 情绪/多巴胺 | `db/86b` | 关系节奏。 |

### 心跳/能量/目标
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_heartbeat_state.py` | 心跳/能量/目标 | `db/07` | 心跳状态机。 |
| `test_heartbeat_economy.py` | 心跳/能量/目标 | `db/42a` | 心跳能量经济。 |
| `test_heartbeat_agentic_sql.py` | 心跳/能量/目标 | `db/43` | 主动式心跳 SQL。 |
| `test_finalize_heartbeat.py` | 心跳/能量/目标 | `db/13` | 心跳收尾。 |
| `test_heartbeat_actions_implemented.py` | 心跳/能量/目标 | `db/17` | 心跳动作实现覆盖。 |
| `test_goals_graph_extra.py` | 心跳/能量/目标 | `db/08` | 目标图补充。 |
| `test_continuity_instinct.py` | 心跳/能量/目标 | `db/75` | 连续性本能。 |
| `test_agent_runtime.py` | 心跳/能量/目标 | `db/37` | agent 运行时状态机。 |
| `test_agent_admin.py` | 心跳/能量/目标 | `db/65` | agent 管理下推。 |
| `test_agent_questions.py` | 心跳/能量/目标 | `db/45` | agent 提问。 |
| `test_agent_transcript.py` | 心跳/能量/目标 | `db/37` | agent transcript。 |
| `test_deliberation.py` | 心跳/能量/目标 | `db/42b` | 对抗性审议（council）。 |

### 信念/溯源/矛盾
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_belief_revision.py` | 信念/溯源/矛盾 | `db/59` | 信念修订与审计。 |
| `test_belief_propagation.py` | 信念/溯源/矛盾 | `db/46d` | 信念传播。 |
| `test_contradiction_events.py` | 信念/溯源/矛盾 | `db/46j` | 矛盾事件。 |
| `test_memory_supersessions.py` | 信念/溯源/矛盾 | `db/46c` | 记忆修订谱系。 |
| `test_temporal_memory_history.py` | 信念/溯源/矛盾 | `db/46k` | 时间点记忆历史。 |
| `test_trust_triggers.py` | 信念/溯源/矛盾 | `db/91` | 信任触发器。 |
| `test_action_claims.py` | 信念/溯源/矛盾 | `db/58` | 行动声明护栏。 |
| `test_operator_policy_corrections.py` | 信念/溯源/矛盾 | `db/46e` | 操作员策略纠正。 |
| `test_learning_reviews.py` | 信念/溯源/矛盾 | `db/56a` | 每周学习复盘。 |
| `test_self_state_mirrors.py` | 信念/溯源/矛盾 | `db/62` | 自省镜像。 |
| `test_origin_memories.py` | 信念/溯源/矛盾 | `db/60` | 起源记忆（免于淡忘、可被质疑）。 |

### 保留/来源文档
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_retention_consolidation.py` | 保留/来源文档 | `db/47` | 保留巩固。 |
| `test_retention_sources.py` | 保留/来源文档 | `db/47` | 来源保留。 |
| `test_retention_surface.py` | 保留/来源文档 | `db/47a` | 保留用户面。 |
| `test_source_documents.py` | 保留/来源文档 | `db/74a` | 来源文档存储。 |
| `test_source_chunks.py` | 保留/来源文档 | `db/83` | 来源分块。 |
| `test_source_chunk_search.py` | 保留/来源文档 | `db/83` | 分块混合检索。 |
| `test_source_artifacts.py` | 保留/来源文档 | `db/81` | 来源物保全。 |
| `test_document_approval.py` | 保留/来源文档 | `db/74` | 文档审批。 |
| `test_ingestion_jobs.py` | 保留/来源文档 | `db/73` | 摄取作业。 |
| `test_ingestion_receipts.py` | 保留/来源文档 | `db/72` | 摄取收据。 |
| `test_channel_source_artifacts.py` | 保留/来源文档 | `db/81` | 渠道来源物。 |
| `test_change_journal.py` | 保留/来源文档 | `db/71` | 变更日志。 |

### HMX
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_hmx_slice0.py` | HMX | `db/48` | HMX Slice 0 枚举/谱系。 |
| `test_hmx_export.py` | HMX | `db/48` | HMX 导出。 |
| `test_hmx_import.py` | HMX | `db/48` | HMX 导入。 |
| `test_hmx_staging.py` | HMX | `db/49` | HMX 导入暂存。 |
| `test_hmx_digest_sql.py` | HMX | `db/57` | SQL 规范摘要。 |
| `test_hmx_reembedding.py` | HMX | `db/51` | HMX 重嵌入。 |
| `test_hmx_in_flight_work.py` | HMX | `db/52` | HMX 在途工作。 |
| `test_hmx_protected_replacement.py` | HMX | `db/53` | 受保护替换。 |
| `test_hmx_authoritative_import.py` | HMX | `db/54` | 权威导入。 |
| `test_hmx_onboarding_interview.py` | HMX | `db/49a` | 入职访谈。 |

### 渠道/Outbox
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_outbox.py` | 渠道/Outbox | `db/42` | 事务性 outbox。 |
| `test_web_inbox.py` | 渠道/Outbox | `db/76` | web 收件箱。 |
| `test_channel_presence.py` | 渠道/Outbox | `db/46b` | 渠道 presence。 |
| `test_channel_shapers.py` | 渠道/Outbox | `db/36` | 渠道读整形。 |
| `test_chat_channel_runtime.py` | 渠道/Outbox | `db/34` | 聊天/渠道回合。 |
| `test_chat_sessions.py` | 渠道/Outbox | `db/78` | 聊天会话历史。 |
| `test_chat_energy.py` | 渠道/Outbox | `db/87` | 聊天能量循环。 |
| `test_inbound_disposition.py` | 渠道/Outbox | `db/86a` | 入站处置。 |
| `test_outbound_safety.py` | 渠道/Outbox | `db/86b` | 出站安全。 |
| `test_pwa_push.py` | 渠道/Outbox | `db/46b` | PWA push。 |
| `test_operator_approval.py` | 渠道/Outbox | `db/43` | 操作员审批。 |
| `test_automation_suggestions.py` | 渠道/Outbox | `db/44` | 自动化建议。 |
| `test_voice_notes.py` | 渠道/Outbox | `db/46a` | 语音留言。 |
| `test_voice_output.py` | 渠道/Outbox | `db/46f` | 语音输出。 |
| `test_voice_wake.py` | 渠道/Outbox | `db/46g` | 语音唤醒。 |
| `test_journal.py` | 渠道/Outbox | `db/46` | 日志。 |
| `test_daily_journal_fallback.py` | 渠道/Outbox | `db/46` | 每日日记回退。 |

### 工具/调度/配置
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_tool_runtime.py` | 工具/调度/配置 | `db/36` | 工具运行时。 |
| `test_db_native_tools.py` | 工具/调度/配置 | `db/38` | DB 原生工具。 |
| `test_tool_reachability_audit.py` | 工具/调度/配置 | `db/36` | 工具可达性审计。 |
| `test_external_calls.py` | 工具/调度/配置 | `db/33` | 外部调用。 |
| `test_prompt_render.py` | 工具/调度/配置 | `db/39` | prompt 渲染（黄金样本）。 |
| `test_backlog.py` | 工具/调度/配置 | `db/25` | backlog CRUD。 |
| `test_config_consent.py` | 工具/调度/配置 | `db/07` | 配置与同意。 |
| `test_initialization_flow.py` | 工具/调度/配置 | `db/10` | 初始化流程。 |
| `test_resource_requests.py` | 工具/调度/配置 | `db/74` | 资源请求。 |
| `test_termination_confirmation.py` | 工具/调度/配置 | `db/07` | 终止确认。 |

### 连接器/集成
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_connector_backfill.py` | 连接器/集成 | `db/79` | 连接器回填。 |
| `test_connector_action_policy.py` | 连接器/集成 | `db/80` | 连接器动作策略。 |
| `test_integration_connectors.py` | 连接器/集成 | `db/77` | 集成连接器。 |
| `test_life_integrations.py` | 连接器/集成 | `db/77a` | 生活集成。 |
| `test_cognition_v2.py` | 连接器/集成 | `db/85` | 连接器认知 v2。 |

### 节点/执行/运维
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_companion_nodes.py` | 节点/执行/运维 | `db/88a` | 同伴节点。 |
| `test_execution_backend_defaults.py` | 节点/执行/运维 | `db/46h` | 执行后端默认值。 |
| `test_wave_c_node_database.py` | 节点/执行/运维 | `db/88a` | Wave C 节点数据库。 |
| `test_worker_observability.py` | 节点/执行/运维 | `db/88` | worker 可观测性。 |
| `test_self_repair.py` | 节点/执行/运维 | `db/92` | 自修复。 |
| `test_backup_restore.py` | 节点/执行/运维 | `db/07` | 备份/恢复。 |
| `test_conduct_norms.py` | 节点/执行/运维 | `db/46e` | 行为规范。 |
| `test_consent_coherence.py` | 节点/执行/运维 | `db/10` | 同意一致性。 |
| `test_emergence_eval.py` | 节点/执行/运维 | `db/*` | 涌现评测。 |
| `test_context_additional.py` | 节点/执行/运维 | `db/09` | 上下文补充。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| 全部测试模块 | `../conftest.py`（fixture） | — |
