---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — tests/services/

## Summary
`tests/services/` 是 51 个针对 `services/` 服务层的测试模块，覆盖 worker 循环、RLM 心跳/聊天、摄取与分块、连接器、保留面、出站投递与后台职责（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解 worker 环境、可观测性与后台循环 | Worker/运行时 | `test_worker_environment.py` | `test_worker_observability.py` |
| 理解 RLM 心跳决策、聊天与 REPL 循环 | RLM/心跳 | `test_rlm_heartbeat.py` | `test_rlm_chat.py`, `test_rlm_repl.py` |
| 理解主动式心跳与任务模式 | RLM/心跳 | `test_heartbeat_agentic.py` | `test_heartbeat_task_mode.py` |
| 理解文档摄取、分块、恢复与来源文档 | 摄取/分块 | `test_ingest_chunks.py` | `test_ingest_resume.py`, `test_ingest_source_documents.py` |
| 理解附件读取与视觉附件 | 摄取/分块 | `test_attachment_reading.py` | `test_visual_attachments.py` |
| 理解记忆嵌入与记忆修订 | 记忆服务 | `test_memory_embeddings.py` | `test_memory_supersessions.py` |
| 理解连接器回填、认知与 setup | 连接器/集成 | `test_connector_cognition.py` | `test_channel_backfill.py`, `test_connector_setup.py` |
| 理解 Gmail 动作与回填 | 连接器/集成 | `test_gmail_actions.py` | `test_gmail_backfill.py` |
| 理解保留用户面与技能改进/可达性 | 保留/技能 | `test_retention_surface.py` | `test_skill_improvement.py`, `test_skill_reachability.py` |
| 理解出站调度投递与 web push | 渠道/投递 | `test_scheduled_outbox_delivery.py` | `test_web_push.py` |
| 理解潜意识评估与矛盾检测 | 潜意识/评估 | `test_subconscious_appraisal.py` | `test_contradictions.py` |
| 理解摘要生成与集成 | 摘要 | `test_summarization.py` | `test_summarization_integration.py` |
| 理解语音、语音留言与本地音频 | 语音 | `test_speech.py` | `test_voice_notes.py`, `test_local_audio_analysis.py` |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `temp_test_db` fixture | `../conftest.py` | L:41 |
| `db_pool` fixture | `../conftest.py` | L:116 |

## Files
> 51 个模块（含 `__init__.py`），按被测子系统分组。

### Worker/运行时
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_worker_environment.py` | Worker/运行时 | `services.worker_service` | worker 环境构建。 |
| `test_worker_observability.py` | Worker/运行时 | `services.worker_service` | worker 可观测性。 |
| `test_capability_probe.py` | Worker/运行时 | `core.tools.config` | 能力探测。 |
| `test_tool_surface_audit.py` | Worker/运行时 | `services.tool_surface_audit` | 工具面审计。 |
| `test_node_gateway.py` | Worker/运行时 | `services.node_gateway`, `core.node_identity` | 节点网关。 |
| `test_node_wake_turn.py` | Worker/运行时 | `services.node_gateway`, `services.chat` | 节点唤醒回合。 |
| `test_interlocutor.py` | Worker/运行时 | `services.agent` | 对话者。 |

### RLM/心跳
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_rlm_heartbeat.py` | RLM/心跳 | `services.hexis_rlm`, `services.rlm_repl` | RLM 心跳决策。 |
| `test_rlm_chat.py` | RLM/心跳 | `services.hexis_rlm` | RLM 聊天。 |
| `test_rlm_repl.py` | RLM/心跳 | `services.rlm_repl` | RLM REPL。 |
| `test_heartbeat_agentic.py` | RLM/心跳 | `services.heartbeat_agentic`, `services.worker_service` | 主动式心跳。 |
| `test_heartbeat_task_mode.py` | RLM/心跳 | `services.heartbeat_agentic` | 心跳任务模式。 |
| `test_prompt_caching.py` | RLM/心跳 | `core.llm` | prompt 缓存。 |
| `test_deliberation.py` | RLM/心跳 | `services.deliberation` | 审议。 |

### 摄取/分块
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_ingest_chunks.py` | 摄取/分块 | `services.ingest` | 摄取分块。 |
| `test_ingest_resume.py` | 摄取/分块 | `services.ingest` | 摄取恢复。 |
| `test_ingest_source_documents.py` | 摄取/分块 | `services.ingest` | 摄取来源文档。 |
| `test_ingest_artifact_jobs.py` | 摄取/分块 | `services.ingest.jobs` | 摄取 artifact 作业。 |
| `test_sectioning_locators.py` | 摄取/分块 | `services.ingest.sectioning` | 分节定位符。 |
| `test_attachment_reading.py` | 摄取/分块 | `services.attachments` | 附件读取。 |
| `test_visual_attachments.py` | 摄取/分块 | `services.agent` | 视觉附件。 |
| `test_reader_results.py` | 摄取/分块 | `services.ingest.readers` | 读取结果。 |
| `test_extraction.py` | 摄取/分块 | `services.extraction` | 提取。 |

### 记忆服务
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_memory_embeddings.py` | 记忆服务 | `services.memory_embeddings` | 记忆嵌入生命周期。 |
| `test_memory_supersessions.py` | 记忆服务 | `services.memory_supersessions` | 记忆修订。 |
| `test_recmem.py` | 记忆服务 | `services.recmem`, `services.worker_service` | RecMem 服务。 |
| `test_hmx_reembedding.py` | 记忆服务 | `services.worker_service` | HMX 重嵌入。 |
| `test_subconscious_appraisal.py` | 潜意识/评估 | `services.agent` | 潜意识评估。 |
| `test_contradictions.py` | 潜意识/评估 | `services.contradictions` | 矛盾检测。 |

### 连接器/集成
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_connector_cognition.py` | 连接器/集成 | `services.connector_cognition` | 连接器认知。 |
| `test_connector_setup.py` | 连接器/集成 | `services.connector_setup` | 连接器 setup。 |
| `test_channel_backfill.py` | 连接器/集成 | `services.channel_backfill` | 渠道回填。 |
| `test_gmail_actions.py` | 连接器/集成 | `core.auth.google_gmail` | Gmail 动作。 |
| `test_gmail_backfill.py` | 连接器/集成 | `core.auth.google_gmail` | Gmail 回填。 |
| `test_life_integrations.py` | 连接器/集成 | `services.life_integrations` | 生活集成。 |
| `test_inbound_disposition.py` | 连接器/集成 | `services.inbound_disposition` | 入站处置。 |

### 保留/技能
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_retention_surface.py` | 保留/技能 | `services.retention_surface` | 保留用户面。 |
| `test_skill_improvement.py` | 保留/技能 | `services.skill_improvement` | 技能改进。 |
| `test_skill_reachability.py` | 保留/技能 | `skills.loader` | 技能可达性。 |
| `test_ambient_responsibilities.py` | 保留/技能 | `services.ambient_responsibilities` | 氛围职责。 |
| `test_automation_suggestions.py` | 保留/技能 | `skills.base` | 自动化建议。 |
| `test_belief_propagation.py` | 保留/技能 | `services.belief_propagation` | 信念传播。 |
| `test_operator_policy_corrections.py` | 保留/技能 | `core.tools.operator_policies` | 操作员策略纠正。 |

### 渠道/投递
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_scheduled_outbox_delivery.py` | 渠道/投递 | `services.worker_service` | 计划 outbox 投递。 |
| `test_web_push.py` | 渠道/投递 | `services.web_push` | Web Push。 |

### 摘要/语音
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_summarization.py` | 摘要 | `services.summarization` | 摘要生成。 |
| `test_summarization_integration.py` | 摘要 | `services.summarization` | 摘要集成。 |
| `test_speech.py` | 语音 | `services.speech` | 语音合成。 |
| `test_voice_notes.py` | 语音 | `channels.base`, `channels.media` | 语音留言。 |
| `test_local_audio_analysis.py` | 语音 | `services.local_audio_analysis` | 本地音频分析。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| 全部测试模块 | `../conftest.py`（fixture） | — |
