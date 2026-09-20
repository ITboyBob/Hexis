---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — tests/core/

## Summary
`tests/core/` 是 109 个针对 `core/` 核心层的测试模块，覆盖记忆 API、agent loop、工具注册与执行、渠道、LLM provider、HMX 交换、网关与自省（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解记忆 API 与仓库层的行为契约 | 记忆 API | `test_cognitive_memory_extra.py` | `test_memory_repo.py`, `test_memory_tools.py` |
| 理解统一 AgentLoop 与工具编排 | Agent/LLM | `test_agent_loop.py` | `test_dynamic_tools.py`, `test_workflow.py` |
| 理解 LLM provider、重试与配置回退 | Agent/LLM | `test_llm.py` | `test_llm_retry.py`, `test_llm_config_fallback.py` |
| 理解工具目录同步、覆盖与统一 | 工具系统 | `test_tool_catalog_sync.py` | `test_tool_coverage.py`, `test_tool_unification.py` |
| 理解渠道适配器与消息呈现 | 渠道/消息 | `test_channels.py` | `test_channel_presentation.py`, `test_voice_channels.py` |
| 理解 HMX 交换、digest 与信任锚 | HMX | `test_hmx_exchange.py` | `test_hmx_digest.py`, `test_hmx_trust_anchors.py` |
| 理解网关事件总线与消费者 | 网关/事件 | `test_gateway.py` | `test_gateway_consumer.py`, `test_gateway_phase3.py` |
| 理解 HTTP API 服务与实例管理 | API/实例 | `test_core_api.py` | `test_instance_api.py`, `test_agent_api.py` |
| 理解技能市场、能力目录与插件校验 | 技能/插件 | `test_skills_marketplace.py` | `test_skill_capability_catalog.py`, `test_plugin_validation.py` |
| 理解自省、能力成熟度与 DB 脑审计 | 自省/治理 | `test_self_inspection.py` | `test_capability_maturity.py`, `test_db_brain_audit.py` |
| 理解记忆巩固与再巩固 | 记忆巩固 | `test_recmem_api.py` | `test_reconsolidation.py`, `test_recmem_compaction.py` |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `temp_test_db` fixture | `../conftest.py` | L:41 |
| `db_pool` fixture | `../conftest.py` | L:116 |

## Files
> 109 个模块，按被测子系统分组；每组内逐文件列出。

### 记忆 API
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_cognitive_memory_extra.py` | 记忆 API | `core.cognitive_memory_api` | 认知记忆 API 扩展路径。 |
| `test_memory_repo.py` | 记忆 API | `core.memory_repo` | 记忆仓库层。 |
| `test_memory_tools.py` | 记忆 API | `core.tools.memory` | 记忆工具处理器。 |
| `test_backlog_tools.py` | 记忆 API | `core.tools.backlog` | backlog 工具。 |
| `test_goals.py` | 记忆 API | `core.tools.goals` | 目标工具。 |
| `test_desk_tools.py` | 记忆 API | `core.tools.desk` | RecMem 桌面工具。 |
| `test_document_tools.py` | 记忆 API | `core.tools.documents` | 文档工具。 |
| `test_journal_tools.py` | 记忆 API | `core.tools.journal` | 日志工具。 |
| `test_history_search_tool.py` | 记忆 API | `core.tools.history` | 历史搜索工具。 |
| `test_recmem_api.py` | 记忆巩固 | `core.cognitive_memory_api` | RecMem API。 |
| `test_reconsolidation.py` | 记忆巩固 | `services.reconsolidation` | 再巩固。 |
| `test_recmem_compaction.py` | 记忆巩固 | `db/31` | RecMem 压缩。 |
| `test_slow_ingest_dedup.py` | 记忆 API | `services.slow_ingest_rlm` | 慢速摄取去重。 |

### Agent/LLM
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_agent_loop.py` | Agent/LLM | `core.agent_loop` | 统一 AgentLoop。 |
| `test_agent_api.py` | Agent/LLM | `core.agent_api` | agent API。 |
| `test_agent_api_sync.py` | Agent/LLM | `core.agent_api` | agent API 同步。 |
| `test_llm.py` | Agent/LLM | `core.llm` | LLM 客户端。 |
| `test_llm_retry.py` | Agent/LLM | `core.llm` | LLM 重试。 |
| `test_llm_batch.py` | Agent/LLM | `core.llm` | 批量 LLM。 |
| `test_llm_config_fallback.py` | Agent/LLM | `core.llm_config` | LLM 配置回退。 |
| `test_llm_config_identity.py` | Agent/LLM | `core.llm_config` | LLM 配置身份。 |
| `test_providers_anthropic_http.py` | Agent/LLM | `core.providers` | Anthropic HTTP provider。 |
| `test_providers_google_code_assist.py` | Agent/LLM | `core.providers` | Google Code Assist provider。 |
| `test_openai_codex_oauth.py` | Agent/LLM | `core.providers` | OpenAI Codex OAuth。 |
| `test_workflow.py` | Agent/LLM | `core.tools.workflow` | 工作流编排。 |
| `test_dynamic_tools.py` | Agent/LLM | `core.tools.dynamic` | 动态工具。 |

### 工具系统
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_tool_catalog_sync.py` | 工具系统 | `core.tools` | 工具目录同步。 |
| `test_tool_coverage.py` | 工具系统 | `core.tools` | 工具覆盖。 |
| `test_tool_unification.py` | 工具系统 | `core.tools` | 工具统一。 |
| `test_tools_hardening.py` | 工具系统 | `core.tools` | 工具加固。 |
| `test_code_execution.py` | 工具系统 | `core.tools.code_execution` | 代码执行。 |
| `test_execution_backends.py` | 工具系统 | `core.execution_backends` | 执行后端。 |
| `test_cron_tools.py` | 工具系统 | `core.tools.cron` | cron 工具。 |
| `test_messaging_tools.py` | 工具系统 | `core.tools.messaging` | 消息工具。 |
| `test_question_tools.py` | 工具系统 | `core.tools.questions` | 提问工具。 |
| `test_responsibility_tools.py` | 工具系统 | `core.tools.responsibilities` | 职责工具。 |
| `test_self_repair_tool.py` | 工具系统 | `core.tools.self_repair` | 自修复工具。 |
| `test_ingest_tools.py` | 工具系统 | `core.tools.base` | 摄取工具。 |
| `test_url_ingest.py` | 工具系统 | `core.tools.ingest` | URL 摄取。 |
| `test_browser.py` | 工具系统 | `core.tools.browser` | 浏览器工具。 |
| `test_browser_launch.py` | 工具系统 | `core.browser`, `core.auth.callback_server` | 浏览器启动。 |
| `test_brave_search.py` | 工具系统 | `core.tools.brave_search` | Brave 搜索。 |
| `test_firecrawl.py` | 工具系统 | `core.tools.firecrawl` | Firecrawl。 |
| `test_image_gen.py` | 工具系统 | `core.tools.image_gen` | 图像生成。 |
| `test_speech_tool.py` | 工具系统 | `core.tools.speech`, `services.speech` | 语音工具。 |
| `test_local_audio_analysis_tool.py` | 工具系统 | `core.tools.local_audio_analysis` | 本地音频分析。 |
| `test_calendar_write.py` | 工具系统 | `core.tools.calendar` | 日历写入。 |
| `test_email_read.py` | 工具系统 | `core.tools.email` | 邮件读取。 |
| `test_email_ingestion.py` | 工具系统 | `core.tools.email` | 邮件摄取。 |
| `test_github_pat.py` | 工具系统 | `core.auth.github_pat` | GitHub PAT。 |
| `test_spotify_integration.py` | 工具系统 | `core.auth.spotify`, `core.integration_reliability` | Spotify 集成。 |
| `test_twitter_x_action_tools.py` | 工具系统 | `core.auth.twitter_x`, `core.auth.store` | Twitter/X 动作工具。 |
| `test_meeting_prep.py` | 工具系统 | `core.tools.calendar` | 会议准备。 |
| `test_contacts.py` | 工具系统 | `core.tools.contacts` | 联系人工具。 |
| `test_humanizer.py` | 工具系统 | `core.tools.humanizer` | 人类化写作工具。 |
| `test_usage_query.py` | 工具系统 | `core.tools.usage_query` | 用量查询工具。 |

### 渠道/消息
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_channels.py` | 渠道/消息 | `channels.base` | 渠道适配器。 |
| `test_channels_phase2.py` | 渠道/消息 | `channels.media` | 渠道第二阶段。 |
| `test_channel_presentation.py` | 渠道/消息 | `channels.base`, `channels.manager` | 渠道呈现。 |
| `test_chat.py` | 渠道/消息 | `services.chat`, `core.agent_loop` | 聊天。 |
| `test_voice_channels.py` | 渠道/消息 | `channels.base`, `channels.media` | 语音渠道。 |
| `test_voice_sidecar.py` | 渠道/消息 | `apps.voice_sidecar`, `core.voice_sidecar` | 语音 sidecar。 |
| `test_outbound_tool_dispatch.py` | 渠道/消息 | `core.tools.messaging` | 出站工具派发。 |
| `test_inbound_disposition_routing.py` | 渠道/消息 | `channels.base` | 入站处置路由。 |
| `test_gmail_action_tools.py` | 渠道/消息 | `core.auth.google_gmail` | Gmail 动作工具。 |
| `test_gmail_connector_tools.py` | 渠道/消息 | `core.auth.google_gmail` | Gmail 连接器工具。 |
| `test_pwa_access.py` | 渠道/消息 | `core.cli_api` | PWA 访问。 |

### HMX
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_hmx_exchange.py` | HMX | `core.memory_exchange` | HMX 导出/导入。 |
| `test_hmx_digest.py` | HMX | `core.digest` | HMX 规范摘要。 |
| `test_hmx_digest_fixtures.py` | HMX | `core.digest` | digest 黄金样本。 |
| `test_hmx_interview.py` | HMX | `core.memory_exchange` | HMX 访谈。 |
| `test_hmx_operator_override.py` | HMX | `core.memory_exchange` | HMX 操作员覆盖。 |
| `test_hmx_tools.py` | HMX | `core.digest`, `core.memory_exchange` | HMX 工具。 |
| `test_hmx_trust_anchors.py` | HMX | `core.memory_exchange` | HMX 信任锚。 |

### 网关/事件
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_gateway.py` | 网关/事件 | `core.gateway` | 网关事件总线。 |
| `test_gateway_consumer.py` | 网关/事件 | `core.gateway`, `services.worker_service` | 网关消费者。 |
| `test_gateway_phase3.py` | 网关/事件 | `core.gateway` | 网关第三阶段。 |
| `test_rabbitmq_bridge.py` | 网关/事件 | `core.rabbitmq_bridge` | RabbitMQ 桥。 |
| `test_companion_node_runtime.py` | 网关/事件 | `core.node_daemon`, `core.agent_loop` | 同伴节点运行时。 |
| `test_node_wake.py` | 网关/事件 | `core.node_daemon`, `core.node_identity` | 节点唤醒。 |
| `test_wave_c_node_actions.py` | 网关/事件 | `core.node_life`, `core.node_actions` | Wave C 节点动作。 |

### API/实例
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_core_api.py` | API/实例 | `core.cognitive_memory_api` | 核心记忆 API 集成。 |
| `test_instance_api.py` | API/实例 | `core.instance_api`, `core.instance` | 实例 API。 |
| `test_instance.py` | API/实例 | `core.instance` | 实例管理。 |
| `test_init_api.py` | API/实例 | `core.init_api` | 初始化 API。 |
| `test_auth_utils.py` | API/实例 | `core.auth.utils` | 认证工具。 |
| `test_auth_ui_flow.py` | API/实例 | `core.auth.ui_flow`, `core.auth.callback_server` | 认证 UI 流。 |
| `test_approval_gate.py` | API/实例 | `core.agent_loop` | 审批门。 |
| `test_api_usage.py` | API/实例 | `core.usage` | API 用量。 |
| `test_tunnel.py` | API/实例 | `core.tunnel` | 隧道。 |
| `test_host_services.py` | API/实例 | `core.host_services` | 主机服务。 |
| `test_backup.py` | API/实例 | `core.tools.backup` | 备份。 |
| `test_sync_utils.py` | API/实例 | `core.sync_utils` | 同步工具。 |

### 技能/插件
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_skills_marketplace.py` | 技能/插件 | `skills.base`, `skills.loader` | 技能市场。 |
| `test_skill_capability_catalog.py` | 技能/插件 | `services.skill_runtime`, `core.tools.config` | 技能能力目录。 |
| `test_plugin_validation.py` | 技能/插件 | `plugins.base`, `plugins.loader` | 插件校验。 |
| `test_extensibility.py` | 技能/插件 | `plugins.base`, `core.tools.base` | 可扩展性。 |
| `test_hexis_mcp_server.py` | 技能/插件 | `apps.hexis_mcp_server` | Hexis MCP server。 |
| `test_mcp_lazy_runtime.py` | 技能/插件 | `core.tools.config` | MCP 懒加载运行时。 |

### 自省/治理
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_self_inspection.py` | 自省/治理 | `core.tools.self_inspection`, `services.skill_runtime` | 自省工具。 |
| `test_capability_maturity.py` | 自省/治理 | `core.capability_maturity` | 能力成熟度。 |
| `test_db_brain_audit.py` | 自省/治理 | `scripts.db_brain_audit` | DB 脑审计。 |
| `test_schema.py` | 自省/治理 | `core.schema` | schema 契约。 |
| `test_is_group_coverage.py` | 自省/治理 | `channels.base` | 群聊覆盖。 |
| `test_council.py` | 自省/治理 | `core.tools.council` | 审议（council）。 |
| `test_integration_reliability.py` | 自省/治理 | `core.integration_reliability` | 集成可靠性。 |

### 其他
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_integration_connector_tools.py` | 其他 | `core.tools.integrations` | 集成连接器工具。 |
| `test_life_integration_tools.py` | 其他 | `core.tools.life_integrations` | 生活集成工具。 |
| `test_tui_widgets.py` | 其他 | `apps.tui` | TUI 组件。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| 全部测试模块 | `../conftest.py`（fixture） | — |
