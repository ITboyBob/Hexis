---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — docs/

## Summary
`docs/` 是 Hexis 的面向用户与贡献者的文档树：按 start/guides/operations/integrations/reference/concepts/philosophy/contributing 分节，根级散落 index/faq/handoff 与设计审计长文（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 查找安装与首个 agent 的上手路径 | Start | `start/index.md` | `start/quickstart.md`, `start/installation.md` |
| 查找任务导向的操作指南（角色卡/摄取/心跳/技能/遗忘） | Guides | `guides/index.md` | `guides/character-cards.md`, `guides/ingestion.md`, `guides/heartbeat.md` |
| 查找运维手册（Docker/worker/数据库/私网访问/发布） | Operations | `operations/index.md` | `operations/production.md`, `operations/secure-remote-access.md` |
| 查找 CLI/工具/配置键/数据库 API 参考 | Reference | `reference/index.md` | `reference/cli.md`, `reference/tools.md`, `reference/config-keys.md` |
| 理解「数据库即大脑」与记忆架构设计 | Concepts | `concepts/index.md` | `concepts/database-is-the-brain.md`, `concepts/memory-architecture.md` |
| 理解人格、伦理与同意的哲学框架 | Philosophy | `philosophy/index.md` | `philosophy/PERSONHOOD.md`, `philosophy/PHILOSOPHY.md` |
| 查找 LLM provider 与消息渠道接入文档 | Integrations | `integrations/index.md` | `integrations/auth/`, `integrations/channels/`, `integrations/external/` |
| 查找开发环境与测试约定 | Contributing | `contributing/index.md` | `contributing/testing.md` |
| 理解记忆保留/压缩的底层设计 | Memory Design | `memory_retention_design.md` | `guides/forgetting-well.md`, `guides/memory-operations.md` |
| 理解 HMX 记忆交换的验收标准 | Portability | `hmx-acceptance.md` | `portable-brain-audit.md`, `reference/hmx.md` |
| 查阅历史审计与已归档的参考笔记 | Archive | `_archive/index` | `_archive/audit-2026-07-29.md`, `_archive/reference-rlm.md` |
| 查找当前任务实现计划 | Plans | `plans/mission-implementation-plan.md` | `MISSION.md`, `PLAN.md` |

## Subdirectories
| Dir | Domain | Depends On | Purpose |
|---|---|---|---|
| `start/` | Start | — | 前置条件、安装、首个 agent 与首轮对话。 |
| `guides/` | Guides | `concepts/` | 20 篇任务导向操作指南（角色卡、摄取、心跳、技能、遗忘、多实例等）。 |
| `operations/` | Operations | `reference/` | Docker/worker/数据库/嵌入/语音/执行后端/私网访问/发布/排障。 |
| `integrations/` | Integrations | `reference/` | Auth provider、消息渠道与外部服务接入。 |
| `reference/` | Reference | — | CLI、工具目录、能量模型、数据库 API/schema、配置键、HMX、插件系统。 |
| `concepts/` | Concepts | — | 架构与设计深潜（数据库即大脑、记忆架构、心跳、身份、自我发展、同意）。 |
| `philosophy/` | Philosophy | — | 人格、伦理与同意的哲学框架。 |
| `contributing/` | Contributing | `operations/` | 开发环境与测试约定。 |
| `plans/` | Plans | — | 当前任务实现计划（非产品文档）。 |
| `_archive/` | Archive | — | 历史审计与已归档参考笔记（INIT_FLOW、RLM、perf、quirks）。 |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| 文档总入口 | `index.md` | — |
| 导航清单 | `nav.md` | — |
| 记忆保留设计 | `memory_retention_design.md` | — |
| HMX 验收标准 | `hmx-acceptance.md` | — |
| 可移植脑审计 | `portable-brain-audit.md` | — |
| 优化笔记 | `optimize.md` | — |
| 交接说明 | `handoff.md` | — |
| FAQ | `faq.md` | — |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `index.md` | Docs Hub | ← 各分节 `index.md` | 文档总入口与快速链接。 |
| `nav.md` | Docs Hub | ← `index.md` | 文档导航清单。 |
| `faq.md` | Docs Hub | ← `index.md` | 成本、隐私、provider、重置、生产就绪等常见问题。 |
| `handoff.md` | Docs Hub | ← — | 项目交接说明。 |
| `memory_retention_design.md` | Memory Design | ← `concepts/memory-architecture.md` | 压缩原生的记忆基底设计。 |
| `hmx-acceptance.md` | Portability | ← `reference/hmx.md` | HMX 记忆交换的验收标准。 |
| `portable-brain-audit.md` | Portability | ← `hmx-acceptance.md` | 可移植脑的审计记录。 |
| `optimize.md` | Docs Hub | ← — | 性能优化笔记。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `index.md` | `nav.md` | 各分节 `index.md` |
| `nav.md` | — | `index.md` |
| `memory_retention_design.md` | — | `guides/forgetting-well.md` |
| `hmx-acceptance.md` | — | `portable-brain-audit.md`, `reference/hmx.md` |
