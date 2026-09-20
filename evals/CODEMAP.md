---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — evals/

## Summary
`evals/` 是 Hexis 的公开评测目录：`memory_benchmark/` 提供与厂商无关的记忆基准（case 语料、适配器、确定性评分），`retrieval/` 提供检索策略与轨迹的确定性 harness（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解记忆基准的 case/prediction 数据模型与校验 | 记忆基准 | `memory_benchmark/model.py` | `memory_benchmark/case.schema.v1.json`, `memory_benchmark/prediction.schema.v1.json` |
| 理解如何运行、评分与比较基准提交 | 记忆基准 | `memory_benchmark/run.py` | `memory_benchmark/scoring.py` |
| 理解参考适配器与 Hexis 记忆基底适配器 | 记忆基准 | `memory_benchmark/adapters.py` | `memory_benchmark/adapter-input.schema.v1.json` |
| 理解检索任务 harness 如何记录调用/延迟/输出大小 | 检索评测 | `retrieval/harness.py` | `retrieval/run.py` |
| 理解合成语料如何生成（无二进制 fixture） | 检索评测 | `retrieval/corpus_gen.py` | `retrieval/conftest.py` |
| 理解脚本化 RLM 检索轨迹评测 | 检索评测 | `retrieval/trajectory.py` | `retrieval/test_retrieval_trajectories.py` |

## Subdirectories
| Dir | Domain | Depends On | Purpose |
|---|---|---|---|
| `memory_benchmark/` | 记忆基准 | `core/memory_exchange` 等 | 公开、judge-free 的记忆基准：语料、适配器、评分与结果。 |
| `retrieval/` | 检索评测 | `db/*.sql`, `services/*` | 检索策略与轨迹的确定性 eval harness（临时库 + 合成语料）。 |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `BenchmarkCase` / `Prediction` 数据模型 | `memory_benchmark/model.py` | — |
| `score_predictions`（确定性评分） | `memory_benchmark/scoring.py` | — |
| `CommandAdapter`（厂商中立桥） | `memory_benchmark/adapters.py` | — |
| 检索 eval 主入口 | `retrieval/run.py` | — |
| 合成语料生成器 | `retrieval/corpus_gen.py` | — |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `__init__.py` | 包初始化 | — | evals 包标记。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `__init__.py` | — | `memory_benchmark/`, `retrieval/` |
