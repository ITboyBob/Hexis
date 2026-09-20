---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — schemas/

## Summary
`schemas/` 存放 Hexis 的公开、版本化互操作契约：`hmx-1.7.schema.json` 是 HMX（Hexis Memory Exchange）v1.7 的规范性 JSON Schema，随仓库以 MIT 许可发布并被 `core.memory_exchange` 直接消费（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解 HMX v1.7 的字段契约与校验规则 | HMX Schema | `hmx-1.7.schema.json` | `README.md` |
| 理解其他项目如何导入/导出 HMX | HMX Schema | `README.md` | `db/48_functions_memory_exchange.sql` |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| HMX v1.7 规范性 JSON Schema | `hmx-1.7.schema.json` | — |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `hmx-1.7.schema.json` | HMX Schema | — | HMX v1.7 规范性、版本化互操作契约，被 `core.memory_exchange` 用于导入/导出校验。 |
| `README.md` | HMX Schema | `hmx-1.7.schema.json` | 说明 HMX 的消费方式与互操作原则（保留未知字段、尊重 export intent、重映射引用、保留溯源）。 |
| `__init__.py` | 包初始化 | — | schemas 包标记。 |
