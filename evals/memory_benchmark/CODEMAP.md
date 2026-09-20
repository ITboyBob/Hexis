---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — evals/memory_benchmark/

## Summary
`evals/memory_benchmark/` 是公开、judge-free 的记忆基准实现：以纯 dataclass + JSON 定义 case/prediction 数据模型，提供参考适配器与厂商中立的 command 适配器，并对提交做确定性评分（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解 case 与 prediction 的字段契约与校验 | 数据模型 | `model.py` | `case.schema.v1.json`, `prediction.schema.v1.json` |
| 理解如何 validate/run/compare 基准 | 运行与评分 | `run.py` | `scoring.py` |
| 理解参考基线适配器与真实 Hexis 适配器 | 适配器 | `adapters.py` | `adapter-input.schema.v1.json` |
| 理解评分规则（确定性、无 LLM judge） | 运行与评分 | `scoring.py` | `model.py` |
| 查看历史运行结果 | 结果 | `results/` | `cases.v1.jsonl` |
| 理解基准语料与使用说明 | 语料/文档 | `cases.v1.jsonl` | `README.md` |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `BenchmarkCase` | `model.py` | — |
| `Prediction` | `model.py` | — |
| `BENCHMARK_NAME` / `BENCHMARK_VERSION` | `model.py` | — |
| 评分入口 | `scoring.py` | — |
| 适配器基类与 `CommandAdapter` | `adapters.py` | — |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `__init__.py` | 包初始化 | — | 基准包标记。 |
| `model.py` | 数据模型 | — | 基准的校验后公开数据模型（纯 dataclass/JSON，无需安装 Hexis）。 |
| `scoring.py` | 运行与评分 | `model.py` | 确定性、无 judge 的提交评分。 |
| `adapters.py` | 适配器 | `model.py` | 参考基线与真实 Hexis 记忆基底适配器；command 适配器是厂商中立桥。 |
| `run.py` | 运行与评分 | `model.py`, `scoring.py`, `adapters.py` | CLI：validate / run / compare（410 行）。 |
| `cases.v1.jsonl` | 语料 | — | 25 行基准 case 语料。 |
| `case.schema.v1.json` | 数据模型 | — | case 的 JSON Schema。 |
| `prediction.schema.v1.json` | 数据模型 | — | prediction 的 JSON Schema。 |
| `adapter-input.schema.v1.json` | 适配器 | — | 适配器 stdin 输入文档 schema。 |
| `README.md` | 文档 | — | 基准说明与使用方式。 |
| `results/2026-08-28.json` | 结果 | — | 一次基准运行结果。 |
| `results/2026-08-28-hexis-predictions.json` | 结果 | — | 对应 Hexis 预测输出。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `scoring.py` | `model.py` | `run.py` |
| `adapters.py` | `model.py` | `run.py` |
| `run.py` | `model.py`, `scoring.py`, `adapters.py` | — |
