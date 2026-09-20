---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — skills/

## Summary
`skills/` 是 Hexis 的技能系统：`base.py` 定义 `SkillSpec` 与技能上下文/分类枚举，`loader.py` 从 markdown frontmatter 发现并加载技能，`installed/` 存放 40+ 个随包技能定义（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解技能的数据模型（上下文/分类/安装/MCP 绑定） | 技能框架 | `base.py` | `loader.py` |
| 理解技能发现来源（bundled/user/workspace/extra）与加载 | 技能框架 | `loader.py` | `base.py` |
| 理解技能依赖安装与执行 | 技能框架 | `loader.py` | `base.py` |
| 查看随包技能目录并按其分类浏览 | 已安装技能 | `installed/CODEMAP.md` | `installed/*/SKILL.md` |

## Subdirectories
| Dir | Domain | Depends On | Purpose |
|---|---|---|---|
| `installed/` | 已安装技能 | `skills/base.py` | 41 个随包技能定义目录，每个含一份 `SKILL.md`（YAML frontmatter + markdown 正文）。 |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `SkillSpec` | `base.py` | L:85 |
| `SkillContext`（heartbeat/chat/mcp） | `base.py` | L:14 |
| `SkillCategory` | `base.py` | L:22 |
| `InstallMethod` | `base.py` | L:36 |
| `MCPBinding` | `base.py` | L:53 |
| `load_skills` | `loader.py` | L:191 |
| `discover_skill_dirs` | `loader.py` | L:170 |
| `install_skill_deps` | `loader.py` | L:267 |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `base.py` | 技能框架 | — | 技能类型定义：`SkillSpec` 解析表示、上下文/分类枚举、安装方式与 MCP 绑定。 |
| `loader.py` | 技能框架 | `base.py` | 从目录发现并加载技能 markdown：简易 YAML frontmatter 解析、bundled/user/workspace/extra 来源、依赖安装。 |
| `__init__.py` | 包初始化 | — | skills 包标记。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `base.py` | — | `loader.py`, `installed/*` |
| `loader.py` | `base.py` | — |
