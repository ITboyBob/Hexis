---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — plugins/installed/

## Summary
`plugins/installed/` 存放随 Hexis 分发的 7 个内置插件，每个插件是一个目录，含 `plugin.json`（manifest 与工具清单）、`tools.py`（工具处理器）与 `skills/<name>/SKILL.md`（该插件随附的技能）（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 查找项目管理/CRM 类插件与工具 | 项目管理/CRM | `asana/plugin.json` | `todoist/plugin.json`, `hubspot/plugin.json` |
| 查找会议转录插件与摄取工具 | 会议/转录 | `fathom/plugin.json` | `fathom/tools.py` |
| 查找社交与媒体插件 | 社交/媒体 | `twitter/plugin.json` | `youtube/plugin.json`, `video_gen/plugin.json` |
| 理解单个插件的工具实现结构 | 插件实现 | `asana/tools.py` | `plugins/base.py` |
| 理解插件随附技能的声明方式 | 插件技能 | `asana/skills/asana/SKILL.md` | `skills/loader.py` |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `asana_create_task` / `asana_list_projects` | `asana/tools.py` | — |
| `fathom_transcripts` / `fathom_ingest` | `fathom/tools.py` | — |
| `hubspot_list_deals` / `hubspot_get_deal` | `hubspot/tools.py` | — |
| `todoist_create_task` / `todoist_list_tasks` / `todoist_complete_task` | `todoist/tools.py` | — |
| `twitter_search` | `twitter/tools.py` | — |
| `generate_video` | `video_gen/tools.py` | — |
| `youtube_channel_stats` / `youtube_video_stats` / `youtube_search` | `youtube/tools.py` | — |

## Files
> 每个插件目录结构一致（`plugin.json` + `tools.py` + `__init__.py` + `skills/<name>/SKILL.md`），按插件逐个汇总。

| File | Domain | Deps | Function |
|---|---|---|---|
| `asana/` | 项目管理/CRM | `plugins/base.py` | Asana 项目/任务集成，工具 `asana_create_task`、`asana_list_projects`，含 `skills/asana/SKILL.md`。 |
| `fathom/` | 会议/转录 | `plugins/base.py` | Fathom 会议转录（列表与摄取进记忆），工具 `fathom_transcripts`、`fathom_ingest`。 |
| `hubspot/` | 项目管理/CRM | `plugins/base.py` | HubSpot CRM 交易集成，工具 `hubspot_list_deals`、`hubspot_get_deal`。 |
| `todoist/` | 项目管理/CRM | `plugins/base.py` | Todoist 任务管理（创建/列出/完成），工具 `todoist_create_task`、`todoist_list_tasks`、`todoist_complete_task`。 |
| `twitter/` | 社交/媒体 | `plugins/base.py` | X/Twitter 经 xAI live search 搜索，工具 `twitter_search`。 |
| `video_gen/` | 社交/媒体 | `plugins/base.py` | 经 Runway 的视频生成，工具 `generate_video`。 |
| `youtube/` | 社交/媒体 | `plugins/base.py` | YouTube 频道/视频统计与搜索，工具 `youtube_channel_stats`、`youtube_video_stats`、`youtube_search`。 |
| `__init__.py` | 包初始化 | — | installed 包标记。 |
