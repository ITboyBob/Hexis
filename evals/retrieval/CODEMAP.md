---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — evals/retrieval/

## Summary
`evals/retrieval/` 是检索评测 harness：在临时库 + 合成语料上运行真实工具处理器与脚本化 RLM 轨迹，记录调用、输出大小与延迟并产出 JSON 报告（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解 harness 如何记录工具调用/延迟/输出大小 | 评测 harness | `harness.py` | `run.py` |
| 理解合成语料如何生成并植入 gold 短语 | 语料生成 | `corpus_gen.py` | `conftest.py` |
| 理解检索任务断言（tier 1/tier 2） | 任务断言 | `test_retrieval_tasks.py` | `run.py` |
| 理解脚本化 RLM 轨迹评测（确定性模型侧） | 轨迹评测 | `trajectory.py` | `test_retrieval_trajectories.py` |
| 理解评测的临时库与语料 fixture 装配 | 评测 fixture | `conftest.py` | `corpus_gen.py` |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| 评测 harness 运行器 | `harness.py` | — |
| `gold` 短语与语料生成 | `corpus_gen.py` | — |
| 脚本化 RLM 轨迹助手 | `trajectory.py` | — |
| 评测入口（`python -m evals.retrieval.run`） | `run.py` | — |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `__init__.py` | 包初始化 | — | 检索评测包标记。 |
| `harness.py` | 评测 harness | `db/*.sql`, `services/*` | 运行工具处理器并记录调用、输出大小与延迟，汇总为 JSON 报告。 |
| `conftest.py` | 评测 fixture | `tests/conftest.py`, `corpus_gen.py` | 每模块临时库、生成语料一次摄取、真实维护步骤嵌入分块，会话末写报告。 |
| `corpus_gen.py` | 语料生成 | — | 确定性合成语料生成器（license-safe，无二进制 fixture，植入唯一 gold token）。 |
| `trajectory.py` | 轨迹评测 | `services/hexis_rlm` | 脚本化 RLM 助手：模型侧脚本化，RLM 循环/REPL/syscall/桌面真实。 |
| `run.py` | 评测 harness | `harness.py` | 独立运行器（tier 1；`HEXIS_EVAL_FULL=1` 开 tier 2）并打印报告。 |
| `test_retrieval_tasks.py` | 任务断言 | `harness.py` | 11 个检索任务断言。 |
| `test_retrieval_trajectories.py` | 轨迹评测 | `trajectory.py` | 3 个脚本化检索轨迹断言。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `conftest.py` | `corpus_gen.py` | `test_retrieval_tasks.py`, `test_retrieval_trajectories.py` |
| `run.py` | `harness.py` | — |
| `test_retrieval_tasks.py` | `harness.py` | — |
| `test_retrieval_trajectories.py` | `trajectory.py` | — |
