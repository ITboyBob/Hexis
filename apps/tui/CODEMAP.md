---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — apps/tui/

## Summary
保留但已非默认的 Textual 界面层：`HexisInitApp` 与 `HexisChatApp` 提供图形化初始化与聊天，其 widgets、模型目录与文本工具被行式 CLI 复用（inferred：`apps/cli_chat.py` 只借用 `textkit`/`tips`）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解 Textual 初始化应用的屏幕流转 | TUI Init | `init_app.py` | `init_screens.py`, `init_widgets.py` |
| 理解初始化各步骤屏幕（LLM/角色/自定义/consent） | TUI Init | `init_screens.py` | `hexis_init.py` |
| 理解初始化专用 widgets（步骤条、大五人格滑条、模型菜单） | TUI Widgets | `init_widgets.py` | `design.py` |
| 理解 Textual 聊天屏幕与流式渲染 | TUI Chat | `chat_app.py` | `chat_widgets.py` |
| 理解聊天 widgets（流式块、工具树、斜杠菜单、输入框） | TUI Chat | `chat_widgets.py` | `activity.py` |
| 理解跨 provider 的模型目录抓取与默认推荐 | TUI Model Catalog | `model_catalog.py` | `core/integration_reliability.py` |
| 理解主题色与样式 token 访问 | TUI Design | `design.py` | `hexis.tcss` |
| 理解状态栏渲染 | TUI Status Bar | `status_bar.py` | `activity.py` |
| 理解文本清理/脱敏/截断工具 | TUI Textkit | `textkit.py` | `cli_chat.py` |
| 理解首启提示语逻辑 | TUI Tips | `tips.py` | `cli_chat.py` |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `HexisInitApp` | `init_app.py` | L:84 |
| `HexisChatApp` | `chat_app.py` | L:447 |
| `fetch_models` | `model_catalog.py` | L:188 |
| `recommended_default` | `model_catalog.py` | L:121 |
| `strip_scaffolding` | `textkit.py` | L:48 |
| `redact` | `textkit.py` | L:124 |
| `random_tip` | `tips.py` | L:48 |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `__init__.py` | TUI Init | ← `—` \| → `—` | 懒加载导出 `HexisInitApp`/`HexisChatApp`，避免导入时拉起 Textual。 |
| `activity.py` | TUI Chat | ← `—` | 工具调用活动的 `ToolEntry`/`ToolActivity` 数据结构。 |
| `chat_app.py` | TUI Chat | ← `core.agent_loop`, `core.cognitive_memory_api`, `services.chat` | Textual 聊天屏幕与 App，驱动流式对话与工具展示。 |
| `chat_widgets.py` | TUI Chat | ← `activity.py`, `design.py`, `textkit.py` | 聊天用 widgets：流式块、推理块、工具树、对话轮、斜杠菜单。 |
| `design.py` | TUI Design | ← `—` | 主题色 token 访问与带样式文本辅助。 |
| `dialogs.py` | TUI Design | ← `—` | 确认与错误两个 `ModalScreen` 对话框。 |
| `hexis.tcss` | TUI Design | ← `—` | Textual 样式表。 |
| `init_app.py` | TUI Init | ← `design.py`, `dialogs.py`, `init_screens.py`, `theme.py` | Textual 初始化 App 与 `InitState` 状态容器。 |
| `init_screens.py` | TUI Init | ← `hexis_init.py`, `model_catalog.py`, `init_widgets.py` | 初始化各步骤 `Screen`（LLM、路径、角色画廊、自定义、consent）。 |
| `init_widgets.py` | TUI Widgets | ← `design.py` | 步骤条、特质滑条、大五人格、角色预览、模型菜单/组合框。 |
| `model_catalog.py` | TUI Model Catalog | ← `core.integration_reliability` \| → `apps.hexis_init` | 抓取 models.dev 与 OpenAI 兼容端点模型列表，含缓存与默认推荐。 |
| `status_bar.py` | TUI Status Bar | ← `design.py`, `textkit.py` | 聊天界面底部状态栏 widget。 |
| `textkit.py` | TUI Textkit | ← `—` \| → `apps.cli_chat` | 剥离 `<think>`/工具脚手架、脱敏与文本截断工具。 |
| `theme.py` | TUI Design | ← `design.py` | 主题常量再导出。 |
| `tips.py` | TUI Tips | ← `—` \| → `apps.cli_chat` | 首启提示语的已读标记与随机选取。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `chat_app.py` | `chat_widgets.py`, `design.py`, `dialogs.py`, `status_bar.py`, `theme.py`, `tips.py` | — |
| `chat_widgets.py` | `activity.py`, `design.py`, `textkit.py` | `chat_app.py` |
| `init_app.py` | `design.py`, `dialogs.py`, `init_screens.py`, `theme.py` | — |
| `init_screens.py` | `design.py`, `dialogs.py`, `init_widgets.py`, `model_catalog.py` | `init_app.py` |
| `init_widgets.py` | `design.py` | `init_screens.py` |
| `status_bar.py` | `design.py`, `textkit.py` | `chat_app.py` |
| `theme.py` | `design.py` | `chat_app.py`, `init_app.py` |
| `activity.py` | — | `chat_widgets.py` |
| `design.py` | — | `chat_app.py`, `chat_widgets.py`, `init_app.py`, `init_screens.py`, `init_widgets.py`, `status_bar.py`, `theme.py` |
| `dialogs.py` | — | `chat_app.py`, `init_app.py`, `init_screens.py` |
| `textkit.py` | — | `chat_widgets.py`, `status_bar.py` |
| `model_catalog.py` | — | `init_screens.py` |
| `tips.py` | — | `chat_app.py` |
