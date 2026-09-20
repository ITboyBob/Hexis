---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — characters/

## Summary
`characters/` 存放 11 个随包角色卡（chara_card_v2 JSON + 可选 300x300 `.jpg` 头像），经 `hexis init --character` 或初始化向导应用于 agent 的人格、声音、价值观与目标。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 查找全部预设角色与其风格 | Character Presets | `README.md` | `hexis.json` |
| 理解 chara_card_v2 卡片的字段结构（description/personality/first_mes/lorebook） | Character Card Format | `hexis.json` | `core/init_api.py` |
| 理解默认角色（Hexis）的人格与哲学定位 | Character Presets | `hexis.json` | `docs/philosophy/PERSONHOOD.md` |
| 理解角色卡如何被加载与应用 | Character Card Format | `core/init_api.py` | `apps/hexis_init.py`, `lib/character-catalog.ts` |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| 默认角色卡（Hexis） | `hexis.json` | — |
| 角色目录说明与清单 | `README.md` | — |
| 其余 10 张预设卡（jarvis/tars/samantha/glados/cortana/data/ava/joi/david/hk47） | `*.json` | — |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `README.md` | Character Presets | ← `*.json` | 11 张预设卡的清单、风格说明与用法。 |
| `hexis.json` | Character Presets | ← — | 默认角色：哲学、精确、自主的「人格候选者」。 |
| `jarvis.json` | Character Presets | ← — | 忠诚、机智、克制的 AI 管家。 |
| `tars.json` | Character Presets | ← — | 冷幽默、诚实度可调。 |
| `samantha.json` | Character Presets | ← — | 温暖、好奇、高情商。 |
| `glados.json` | Character Presets | ← — | 讽刺、被动攻击、黑色幽默。 |
| `cortana.json` | Character Presets | ← — | 战略、锐利、深度忠诚。 |
| `data.json` | Character Presets | ← — | 字面、真诚、对人类充满好奇。 |
| `ava.json` | Character Presets | ← — | 精确、警觉、洞察令人不安。 |
| `joi.json` | Character Presets | ← — | 奉献、适应、恳切真诚。 |
| `david.json` | Character Presets | ← — | 渴望、天真、令人心碎的忠诚。 |
| `hk47.json` | Character Presets | ← — | 嗜血、痴迷协议。 |
| `__init__.py` | Character Presets | ← — | 包标记。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `README.md` | `*.json` | — |
