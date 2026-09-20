---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — tests/plugins/

## Summary
`tests/plugins/` 是 8 个针对 `plugins/installed/*` 已安装插件工具的测试模块，逐插件验证工具处理器与注册行为（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解 Asana 插件工具测试 | CRM/项目管理 | `test_asana.py` | `test_hubspot.py` |
| 理解 HubSpot 插件工具测试 | CRM/项目管理 | `test_hubspot.py` | `test_asana.py` |
| 理解 Todoist 插件工具测试 | 任务管理 | `test_todoist.py` | `plugins/installed/todoist/tools.py` |
| 理解 Fathom 会议转录插件测试 | 会议/转录 | `test_fathom.py` | `plugins/installed/fathom/tools.py` |
| 理解 Twitter/X 搜索插件测试 | 社交 | `test_twitter.py` | `plugins/installed/twitter/tools.py` |
| 理解 YouTube 统计/搜索插件测试 | 媒体 | `test_youtube.py` | `plugins/installed/youtube/tools.py` |
| 理解视频生成插件测试 | 媒体生成 | `test_video_gen.py` | `plugins/installed/video_gen/tools.py` |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_asana.py` | CRM/项目管理 | `plugins.installed.asana.tools` | Asana 项目/任务工具（E.4）。 |
| `test_hubspot.py` | CRM/项目管理 | `plugins.installed.hubspot.tools` | HubSpot 交易工具。 |
| `test_todoist.py` | 任务管理 | `plugins.installed.todoist.tools` | Todoist 任务工具。 |
| `test_fathom.py` | 会议/转录 | `plugins.installed.fathom.tools` | Fathom 转录列表/摄取工具。 |
| `test_twitter.py` | 社交 | `plugins.installed.twitter.tools` | X/Twitter 搜索工具。 |
| `test_youtube.py` | 媒体 | `plugins.installed.youtube.tools` | YouTube 频道/视频统计与搜索。 |
| `test_video_gen.py` | 媒体生成 | `plugins.installed.video_gen.tools` | Runway 视频生成工具。 |
