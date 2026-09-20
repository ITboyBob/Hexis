---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — skills/installed/

## Summary
`skills/installed/` 是 41 个随包技能定义目录的目录集，每个目录含一份 `SKILL.md`（YAML frontmatter 声明 name/description/category/contexts 等，正文为技能指令）；本图按 Domain 汇总目录，不逐一枚举文件（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 查找系统/基础设施类技能（记忆、执行、自省、技能编写） | 系统 | `core-memory/SKILL.md` | `code-execution/`, `self-inspection/`, `operations/` |
| 查找生产力类技能（日程、会议、任务、周报） | 生产力 | `calendar/SKILL.md` | `meeting-prep/`, `weekly-review/`, `daily-briefing/` |
| 查找通讯/渠道类技能（邮件、收件箱、Gmail、外联） | 通讯 | `inbox-triage/SKILL.md` | `gmail-actions/`, `email-digest/`, `outreach/` |
| 查找知识/研究类技能（研究、知识摄取、个人笔记） | 知识 | `research/SKILL.md` | `knowledge-ingest/`, `personal-notes/`, `council/` |
| 查找创意类技能（图像、语音、音乐、人类化） | 创意 | `image-gen/SKILL.md` | `speech/`, `spotify/`, `humanizer/` |
| 查找分析类技能（成本报告） | 分析 | `cost-report/SKILL.md` | `weekly-review/` |
| 理解技能如何声明 category/contexts 元数据 | 技能元数据 | `core-memory/SKILL.md` | `skills/base.py` |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| 41 个技能定义（每个目录一份 `SKILL.md`） | `*/SKILL.md` | — |

## Files
> 41 个技能目录按 category 汇总；每个目录仅含 `SKILL.md`。

### 系统 System
| File | Domain | Deps | Function |
|---|---|---|---|
| `core-memory/` | 系统 | `skills/base.py` | 核心记忆技能（category: system）。 |
| `code-execution/` | 系统 | `skills/base.py` | 代码执行（category: system）。 |
| `operations/` | 系统 | `skills/base.py` | 运维操作（category: system）。 |
| `self-inspection/` | 系统 | `skills/base.py` | 自省（category: system）。 |
| `self-reflection/` | 系统 | `skills/base.py` | 心跳中的自主反思指南。 |
| `skill-authoring/` | 系统 | `skills/base.py` | 技能编写（category: system）。 |
| `host-node/` | 系统 | `skills/base.py` | 主机节点（category: system）。 |
| `delegation/` | 生产力 | `skills/base.py` | 委托（category: productivity）。 |

### 生产力 Productivity
| File | Domain | Deps | Function |
|---|---|---|---|
| `calendar/` | 生产力 | `skills/base.py` | 日历（category: productivity）。 |
| `crm-lookup/` | 生产力 | `skills/base.py` | CRM 查询（category: productivity）。 |
| `daily-briefing/` | 生产力 | `skills/base.py` | 每日简报（category: productivity）。 |
| `expense-capture/` | 生产力 | `skills/base.py` | 费用记录（category: productivity）。 |
| `github-issues/` | 生产力 | `skills/base.py` | GitHub issues（category: productivity）。 |
| `home-assistant/` | 生产力 | `skills/base.py` | Home Assistant（category: productivity）。 |
| `meeting-follow-up/` | 生产力 | `skills/base.py` | 会议跟进（category: productivity）。 |
| `meeting-prep/` | 生产力 | `skills/base.py` | 会议准备（category: productivity）。 |
| `notion/` | 生产力 | `skills/base.py` | Notion（category: productivity）。 |
| `travel-prep/` | 生产力 | `skills/base.py` | 出行准备（category: productivity）。 |
| `trello/` | 生产力 | `skills/base.py` | Trello（category: productivity）。 |
| `weekly-review/` | 生产力 | `skills/base.py` | 周度复盘（category: productivity）。 |

### 通讯 Communication
| File | Domain | Deps | Function |
|---|---|---|---|
| `connector-action-authorization/` | 通讯 | `skills/base.py` | 连接器动作授权（category: communication）。 |
| `email-digest/` | 通讯 | `skills/base.py` | 邮件摘要（category: communication）。 |
| `gmail-actions/` | 通讯 | `skills/base.py` | Gmail 动作（category: communication）。 |
| `gmail-connector-setup/` | 通讯 | `skills/base.py` | Gmail 连接器 setup（category: communication）。 |
| `inbox-triage/` | 通讯 | `skills/base.py` | 收件箱分诊（category: communication）。 |
| `integration-connector-setup/` | 通讯 | `skills/base.py` | 集成连接器 setup（category: communication）。 |
| `outreach/` | 通讯 | `skills/base.py` | 外联（category: communication）。 |
| `speech/` | 通讯 | `skills/base.py` | 语音（category: communication）。 |
| `twitter-x-actions/` | 通讯 | `skills/base.py` | Twitter/X 动作（category: communication）。 |
| `twitter-x-connector-setup/` | 通讯 | `skills/base.py` | Twitter/X 连接器 setup（category: communication）。 |

### 知识 Knowledge
| File | Domain | Deps | Function |
|---|---|---|---|
| `audio-analysis/` | 知识 | `skills/base.py` | 音频分析（category: knowledge）。 |
| `council/` | 知识 | `skills/base.py` | 多视角审议（category: knowledge）。 |
| `knowledge-ingest/` | 知识 | `skills/base.py` | 知识摄取（category: knowledge）。 |
| `memory-exchange/` | 知识 | `skills/base.py` | 记忆交换（category: knowledge）。 |
| `personal-notes/` | 知识 | `skills/base.py` | 个人笔记（category: knowledge）。 |
| `research/` | 知识 | `skills/base.py` | 使用 web 工具做研究。 |

### 创意 Creative
| File | Domain | Deps | Function |
|---|---|---|---|
| `humanizer/` | 创意 | `skills/base.py` | 人类化写作（category: creative）。 |
| `image-gen/` | 创意 | `skills/base.py` | 图像生成（category: creative）。 |
| `spotify/` | 创意 | `skills/base.py` | Spotify（category: creative）。 |

### 分析/研究 Analytics/Research
| File | Domain | Deps | Function |
|---|---|---|---|
| `cost-report/` | 分析 | `skills/base.py` | 成本报告（category: analytics）。 |
| `weather/` | 研究 | `skills/base.py` | 天气（category: research）。 |
