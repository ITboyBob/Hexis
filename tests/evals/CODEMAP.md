---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — tests/evals/

## Summary
`tests/evals/` 是 2 个针对 `evals/` 评测框架的测试模块，验证公开记忆基准的 schema/评分契约与用户模型轨迹（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解记忆基准 case/prediction schema 与评分契约 | 记忆基准 | `test_memory_benchmark.py` | `evals/memory_benchmark/model.py`, `evals/memory_benchmark/scoring.py` |
| 理解用户模型轨迹评测 | 用户模型轨迹 | `test_user_model_trajectories.py` | `evals/retrieval/trajectory.py` |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_memory_benchmark.py` | 记忆基准 | `evals.memory_benchmark` | 基准文档 schema 校验、评分与结果比较。 |
| `test_user_model_trajectories.py` | 用户模型轨迹 | `services.connector_cognition` | 用户模型轨迹评测。 |
