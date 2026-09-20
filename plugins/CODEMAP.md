---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — plugins/

## Summary
`plugins/` 是 Hexis 的插件系统：`base.py` 定义插件接口与 API 对象，`loader.py` 从文件系统发现/加载插件，`registry.py` 收集已注册的工具/hook/技能目录，`installed/` 存放随包分发的插件（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解插件接口、manifest 校验与 API 对象 | 插件框架 | `base.py` | `loader.py`, `registry.py` |
| 理解插件发现来源与加载顺序 | 插件框架 | `loader.py` | `base.py` |
| 理解已加载插件的工具/hook/技能如何被收集 | 插件框架 | `registry.py` | `base.py` |
| 查看随包分发的具体插件与工具清单 | 已安装插件 | `installed/CODEMAP.md` | `installed/*/plugin.json` |

## Subdirectories
| Dir | Domain | Depends On | Purpose |
|---|---|---|---|
| `installed/` | 已安装插件 | `plugins/base.py` | 随包分发的 7 个插件（Asana/Fathom/HubSpot/Todoist/Twitter/Video Gen/YouTube），每个含 `plugin.json`、`tools.py` 与 `skills/*/SKILL.md`。 |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `PluginManifest` | `base.py` | L:37 |
| `HexisPluginApi` | `base.py` | L:164 |
| `HexisPlugin`（ABC） | `base.py` | L:242 |
| `discover_plugins` | `loader.py` | L:43 |
| `PluginRegistry` | `registry.py` | L:34 |
| `PluginValidationError` | `base.py` | L:32 |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `base.py` | 插件框架 | `core.tools.base`, `core.tools.hooks` | 插件接口与 API 对象：manifest、注册工具/hook、可选工具包装、`HexisPlugin` 抽象基类。 |
| `loader.py` | 插件框架 | `base.py` | 从 `plugins/installed/` 与 DB 配置的 `plugin.external_dirs` 发现并加载插件模块。 |
| `registry.py` | 插件框架 | `base.py` | 收集所有已加载插件的工具处理器、hook 与技能目录。 |
| `__init__.py` | 包初始化 | — | plugins 包标记。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `base.py` | — | `loader.py`, `registry.py`, `installed/*` |
| `loader.py` | `base.py` | `registry.py` |
| `registry.py` | `base.py` | — |
