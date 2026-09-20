---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — db/migrations/

## Summary
`db/migrations/` 存放 251 个前向、仅追加的迁移文件（`NNNN_slug.sql`，共约 68,485 行），在已有数据库上以 `baseline + migrations` 的方式无数据丢失地演进 schema；每个已应用文件按 SHA-256 校验、永不重跑（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解迁移如何被 runner 应用、锁定与校验和验证 | 迁移机制 | `README.md` | `db/32_tables_runtime.sql`（`schema_migrations` 消费方） |
| 理解 HMX 记忆交换从枚举到受保护替换的完整切片演进 | HMX 交换 | `0004_hmx_export_functions.sql` | `0001`–`0015`, `0024`, `0250` |
| 理解 agent 配置/初始化下推与角色卡演进 | Agent 初始化与配置 | `0020_active_persona_context.sql` | `0022`, `0048`–`0056`, `0068` |
| 理解信念修订、起源记忆与身份作为信念 | 信念/溯源 | `0030_belief_revision.sql` | `0032`, `0040`, `0071`, `0076` |
| 理解保留/淡忘阶梯如何逐版落地 | 保留/衰减 | `0044_retention_on.sql` | `0092`, `0121`, `0236`, `0247` |
| 理解 ag_catalog 影子清理与 schema 守卫的修复史 | Schema 治理 | `0043_one_brain_one_schema.sql` | `0047`, `0050`, `0096`, `0100` |
| 理解 Python→SQL 下推（db_pushdown）如何分批完成 | DB 下推 | `0051_agent_admin_pushdown.sql` | `0052`–`0055`, `0063`, `0065`, `0067` |
| 理解来源文档/分块/混合检索的存储演进 | 来源文档与检索 | `0102_source_documents.sql` | `0113`–`0123`, `0149`–`0152`, `0189` |
| 理解连接器回填与认知基底的演进 | 集成 Connectors | `0106_connector_backfill_substrate.sql` | `0124`, `0130`, `0138`, `0165`, `0218` |
| 理解 RecMem span 嵌入与批处理修正 | RecMem | `0150_recmem_span_embeddings.sql` | `0129`, `0149`, `0151`, `0152` |
| 理解工作可观测性、任务结果与启动风暴检测 | Worker 可观测性 | `0153_worker_observability.sql` | `0154`, `0175`, `0242` |
| 理解氛围职责（ambient responsibilities）如何引入 | 自动化/职责 | `0185_ambient_responsibilities.sql` | `0186`, `0187`, `0207` |
| 理解自修复缺陷报告与失败分类的补丁史 | 审计/自修复 | `0190_self_repair_defect_reports.sql` | `0249`, `0251` |
| 理解操作员审批、自动化建议与 agent 提问的持久化 | 渠道/Outbox | `0206_operator_tool_approvals.sql` | `0207`, `0208`, `0215`, `0216` |
| 理解出站安全合约的分阶段落地 | 渠道/Outbox | `0223_outbound_safety.sql` | `0221`, `0222`, `0248` |
| 理解记忆修订谱系、矛盾事件与时间点召回 | 信念/溯源 | `0213_memory_supersessions.sql` | `0232`, `0233`, `0234`, `0238` |
| 理解同伴节点、语音与执行后端的 Wave C 演进 | 节点/语音/执行 | `0224_companion_nodes.sql` | `0228`–`0231`, `0237` |
| 理解心跳能量经济与审议（council）的 DB 化 | 心跳 | `0225_heartbeat_economy.sql` | `0226`, `0243`, `0245` |

## Subdirectories
| Dir | Domain | Depends On | Purpose |
|---|---|---|---|
| （无） | — | — | 本目录为扁平目录，仅含 `*.sql` 与 `README.md`。 |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `schema_migrations` 记录机制 | `README.md` | — |
| HMX 导出/导入函数族 | `0004_hmx_export_functions.sql` | — |
| `belief_revision_audit` 表 + `residual_v1` 策略 | `0030_belief_revision.sql` | — |
| `identity` 作为信念的记忆化 | `0040_identity_as_belief.sql` | — |
| `assert_db_brain_ready` / schema 守卫 | `0047_schema_guard.sql` | — |
| `source_documents` 表 | `0102_source_documents.sql` | — |
| `source_document_chunks` 表 | `0116_source_document_chunks.sql` | — |
| 连接器认知（user_model_claims 等） | `0124_connector_cognition.sql` | — |
| `worker_instances` / `worker_task_runs` | `0153_worker_observability.sql` | — |
| 氛围职责表 | `0185_ambient_responsibilities.sql` | — |
| `defect_reports` 自修复 | `0190_self_repair_defect_reports.sql` | — |
| `memory_supersessions` 谱系 | `0213_memory_supersessions.sql` | — |
| 出站安全合约与账本 | `0223_outbound_safety.sql` | — |
| `contradiction_cases` 矛盾事件 | `0233_contradiction_events.sql` | — |
| 刻意遗忘（deliberate forgetting） | `0236_deliberate_forgetting.sql` | — |

## Files
> 共 251 个文件，按迁移编号的时代/主题分带描述。**不逐条列出**；每带给出代表文件、行数与主题范围。

### 0001–0015 HMX 交换（Slice 0–11）
| File | Domain | Deps | Function |
|---|---|---|---|
| `0001_hmx_enum_values.sql` | HMX 交换 | — | 同步 HMX 枚举与 AGE 标签（`-- migrate:no-transaction` 的自动提交特例，10 行）。 |
| `0004_hmx_export_functions.sql` | HMX 交换 | `0001` | HMX 导出函数集（290 行）。 |
| `0007_hmx_additive_import.sql` | HMX 交换 | `0004` | 加性导入路径（404 行）。 |
| `0012_hmx_protected_replacement.sql` | HMX 交换 | `0007` | 受保护区替换协议状态与审计（309 行）。 |
| `0002`, `0003`, `0005`, `0006`, `0008`–`0011`, `0013`–`0015` | HMX 交换 | `0001`/`0004` | 谱系、bootstrap 溯源、narrative export id、可选 section、受保护导入、审议分析、重嵌入、在途工作、权威导入、回退与验收诊断。 |

### 0016–0043 意识层与 DB 下推奠基
| File | Domain | Deps | Function |
|---|---|---|---|
| `0016_cross_session_fts.sql` | 来源文档与检索 | — | 跨会话全文检索（115 行）。 |
| `0020_active_persona_context.sql` | Agent 初始化与配置 | `0016` | 把已初始化角色人格注入共享意识 prompt（55 行）。 |
| `0029_action_claim_guardrail.sql` | 信念/溯源 | `0020` | 行动声明护栏表与检测（535 行）。 |
| `0030_belief_revision.sql` | 信念/溯源 | `0029` | 信念修订审计表、`residual_v1` 策略与证据合成（308 行）。 |
| `0033_conscious_extraction.sql` | 意识/提取 | `0030` | 潜意识观测者选择性编码（379 行）。 |
| `0040_identity_as_belief.sql` | 信念/溯源 | `0033` | 身份作为信念的迁移（890 行）。 |
| `0043_one_brain_one_schema.sql` | Schema 治理 | `0040` | 从 ag_catalog 驱逐陈旧 Hexis 函数（62 行）。 |
| 其余 `0017`–`0042` | 混合 | 前序 | 技能提案、全天活跃时段、绑定记忆重要性、自我上下文去重、心跳释放、cron 数学、记忆 dispatcher v2、起源记忆、自省镜像、场景巩固、保留开启、日记习惯等。 |

### 0044–0080 保留、初始化收敛与第一批 Python→SQL 下推
| File | Domain | Deps | Function |
|---|---|---|---|
| `0044_retention_on.sql` | 保留/衰减 | `0043` | 默认开启可逆巩固（22 行）。 |
| `0047_schema_guard.sql` | Schema 治理 | `0044` | schema 影子报告/修复与事件触发器（112 行）。 |
| `0051_agent_admin_pushdown.sql` | DB 下推 | `0047` | agent 配置原子应用与联系人 upsert（259 行）。 |
| `0052_ingest_persist.sql` | DB 下推 | `0051` | 原子摄取持久化（219 行）。 |
| `0055_heartbeat_plan.sql` | 心跳 | `0052` | 心跳计划下推（171 行）。 |
| `0063_read_shapers.sql` | DB 下推 | `0055` | 读整形下推（567 行）。 |
| `0065_humanizer_patterns.sql` | DB 下推 | `0063` | AI 写作模式表与 SQL 检测（166 行）。 |
| `0072_change_journal.sql` | 审计/自修复 | `0065` | 变更日志（356 行）。 |
| `0081`–`0080` 段其余 | 混合 | 前序 | 同意门控、表归位、慢速摄取、评估预算、信任来源权威、摄取收据/作业、资源请求、连续性本能、web inbox、统一召回、元记忆面、孵化循环、关系节奏等。 |

### 0081–0101 配置注册表与文本清理
| File | Domain | Deps | Function |
|---|---|---|---|
| `0081_unified_recall.sql` | 召回 | `0080` | 统一召回路径（382 行）。 |
| `0096_config_defaults_registry.sql` | 配置治理 | `0081` | `config_defaults` 注册表，getter 回退默认值（133 行）。 |
| `0100_scrub_retired_label_from_functions.sql` | 文本清理 | `0096` | 清理函数体中的退役本地 provider 措辞（28 行）。 |
| 其余 `0082`–`0101` | 混合 | 前序 | 元记忆面、孵化循环、推断关怀、自我延展日记、严格 init、潜意识告警校准、人本尺度记忆、会话推断语域、行为规范、渠道状态默认值、退役标签清扫。 |

### 0102–0123 来源文档、分块、连接器与 RecMem 桌面
| File | Domain | Deps | Function |
|---|---|---|---|
| `0102_source_documents.sql` | 来源文档与检索 | `0101` | 持久原始来源文档（446 行）。 |
| `0104_db_owned_chat_sessions.sql` | 渠道/Outbox | `0102` | DB 拥有的聊天会话历史（369 行）。 |
| `0116_source_document_chunks.sql` | 来源文档与检索 | `0104` | 来源文档分块（563 行）。 |
| `0118_source_chunk_hybrid_search.sql` | 来源文档与检索 | `0116` | 分块混合检索（579 行）。 |
| `0124_connector_cognition.sql` | 集成 Connectors | `0118` | 连接器认知基底（670 行）。 |
| 其余 `0103`, `0105`–`0123` | 混合 | 前序 | 集成连接器、能力推导、回填基底/ provider claim、动作授权、Gmail 工具、渠道来源物、RecMem GC、桌面搜索/pin、artifact 作业、来源保留、检索阶梯 prompt、自动桌面。 |

### 0125–0149 检索强化、worker 前置与关系/情绪耦合
| File | Domain | Deps | Function |
|---|---|---|---|
| `0129_async_memory_embeddings.sql` | 嵌入 | `0124` | 持久记忆异步嵌入生命周期（367 行）。 |
| `0134_chat_energy_loop.sql` | 渠道/Outbox | `0129` | 聊天能量/连接循环（141 行）。 |
| 其余 `0125`–`0149` | 混合 | 前序 | 可移植脑认知 v2、图路径 prompt 上下文、奖励事件钩子、Twitter 归档回填、user model 上下文、记忆强化事件、MCP 注册优先、渐进评估深度、渠道 presence、recall pending FTS、人格挑战边界、affect 信号耦合、关系损伤 carryover、web carryover、embeddinggemma 默认、RLM chat/streaming 配置、RecMem 嵌入输入边界。 |

### 0150–0174 RecMem span 嵌入、worker 可观测性与渠道投递
| File | Domain | Deps | Function |
|---|---|---|---|
| `0150_recmem_span_embeddings.sql` | RecMem | `0149` | 长单元按可搜索子 span 嵌入，父向量作路由质心（557 行）。 |
| `0153_worker_observability.sql` | Worker 可观测性 | `0150` | worker 存活与逐任务执行历史（409 行）。 |
| `0161_chat_continuity_packet.sql` | 渠道/Outbox | `0153` | 聊天连续性包（584 行）。 |
| `0174_channel_delivery_obligations.sql` | 渠道/Outbox | `0161` | 渠道投递义务账本（268 行）。 |
| 其余 `0151`, `0152`, `0154`–`0173` | 混合 | 前序 | span 嵌入修正、批处理 chunk 嵌入、worker 任务结果、聊天会话 artifact、按需技能提案、web inbox 投递、outbox 即时性、关系损伤证据、无效先例 raw units、Gmail 策略/删除、同意出生记忆召回、嵌入批上限、人格基底契约、异步 URL 摄取、渠道暂停/不可达。 |

### 0175–0205 氛围职责、自修复、技能选择与审批
| File | Domain | Deps | Function |
|---|---|---|---|
| `0185_ambient_responsibilities.sql` | 自动化/职责 | `0174` | 氛围职责：持久 observe/evaluate/notify 承诺（976 行）。 |
| `0190_self_repair_defect_reports.sql` | 审计/自修复 | `0185` | 自修复缺陷报告（916 行）。 |
| `0199_persist_action_receipts.sql` | 审计/自修复 | `0190` | 持久行动收据（433 行）。 |
| `0202_semantic_skill_selection.sql` | 技能 | `0199` | 语义技能选择（59 行）。 |
| 其余 `0175`–`0205` | 混合 | 前序 | 启动风暴检测、Gmail 本地摄取/结构化 OAuth/托管默认/文案、心跳摘要门控、氛围职责收尾、连续性包、结构化混合候选、目标优先级归一、跳过活跃心跳、web inbox 回填/动作、HMX 重嵌入歧义、人格放宽、技能遥测、阈值、退役死心跳动作、能力可达性审计。 |

### 0206–0227 操作员治理、出站安全、节点/语音 Wave C
| File | Domain | Deps | Function |
|---|---|---|---|
| `0207_automation_suggestions.sql` | 自动化/职责 | `0206` | 同意优先的自动化建议（745 行）。 |
| `0213_memory_supersessions.sql` | 信念/溯源 | `0207` | 记忆修订谱系（504 行）。 |
| `0223_outbound_safety.sql` | 渠道/Outbox | `0213` | 目的约束出站通讯、注意力预算、披露与跨渠道 STOP（2213 行）。 |
| `0224_companion_nodes.sql` | 节点/语音/执行 | `0223` | 签名同伴节点与调用队列（576 行）。 |
| `0225_heartbeat_economy.sql` | 心跳 | `0224` | 能量银行与自适应节律（938 行）。 |
| `0226_deliberation.sql` | 心跳 | `0225` | 对抗性审议（council）（540 行）。 |
| 其余 `0206`, `0208`–`0222`, `0227` | 混合 | 前序 | 操作员工具审批、agent 提问、语音留言、PWA push/presence、内嵌向量不变量、信念传播、操作员策略纠正、渠道操作员身份、情绪族、连接器认知 LLM-first、可信目标起源、入站处置、NUL 修复、Wave B 集成。 |

### 0228–0251 收尾：语音输出、矛盾事件、时间记忆与运维修补
| File | Domain | Deps | Function |
|---|---|---|---|
| `0233_contradiction_events.sql` | 信念/溯源 | `0228` | 矛盾作为持久复审事件（951 行）。 |
| `0236_deliberate_forgetting.sql` | 保留/衰减 | `0233` | 刻意遗忘（662 行）。 |
| `0240_agent_transcript.sql` | Agent 运行时 | `0236` | agent transcript（74 行）。 |
| `0251_dns_failure_classification.sql` | 审计/自修复 | `0240` | 为 `classify_defect_event` 补 DNS/名称解析失败分类（85 行）。 |
| 其余 `0228`–`0250` | 混合 | 前序 | 语音输出/唤醒、节点能力升级、执行后端、记忆溯源、时间记忆历史、每周学习复盘、Wave C 节点动作、迁移校验和与矛盾兼容、每日日记回退、潜意识 prompt 拆分、worker code schema skew、RLM 心跳 prompt 刷新、陈旧工具定义检查、心跳工具广告修复、Gmail 持续同步、保留声明、用户联系追踪与冷却、证据候选恢复、HMX 入职访谈。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `0001_hmx_enum_values.sql` | — | `0004`, `0007`, `0012` 等 HMX 带 |
| `0004_hmx_export_functions.sql` | `0001` | `0007`, `0012` |
| `0012_hmx_protected_replacement.sql` | `0007` | `0013`, `0014` |
| `0030_belief_revision.sql` | `0029` | `0033`, `0040` |
| `0043_one_brain_one_schema.sql` | `0040` | `0047`, `0050` |
| `0051_agent_admin_pushdown.sql` | `0047` | `0052` |
| `0052_ingest_persist.sql` | `0051` | `0055` |
| `0102_source_documents.sql` | `0101` | `0104`, `0116` |
| `0116_source_document_chunks.sql` | `0104` | `0118` |
| `0124_connector_cognition.sql` | `0118` | `0125`–`0130` |
| `0150_recmem_span_embeddings.sql` | `0149` | `0151`, `0153` |
| `0185_ambient_responsibilities.sql` | `0174` | `0190` |
| `0213_memory_supersessions.sql` | `0207` | `0223` |
| `0223_outbound_safety.sql` | `0213` | `0224` |
| `0225_heartbeat_economy.sql` | `0224` | `0226` |
| `0233_contradiction_events.sql` | `0228` | `0236` |
