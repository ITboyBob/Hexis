---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — services/ingest/

## Summary
`services/ingest/` 是 Hexis 的通用内容摄入流水线（原单模块 `services/ingest.py` 的包形式），负责多格式读取、分节、LLM 抽取与记忆入库（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解一次摄入从 CLI 到入库的完整流程 | Ingestion Pipeline | `pipeline.py` | `cli.py`, `store.py` |
| 理解不同文件类型如何选择 reader | Document Readers | `readers.py` | `config.py` |
| 理解长文如何切分为带锚点的 section | Sectioning | `sectioning.py` | `config.py` |
| 理解摄入配置与数据模型（Config/Section/Appraisal） | Ingestion Config | `config.py` | `pipeline.py` |
| 理解 LLM 抽取与内容评估 | Ingestion LLM | `llm.py` | `pipeline.py` |
| 理解抽取结果如何写入认知记忆 | Ingestion Store | `store.py` | `core/cognitive_memory_api.py` |
| 理解后台摄入任务的领取与执行 | Ingestion Jobs | `jobs.py` | `pipeline.py` |
| 理解原始产物在 DB 与磁盘之间的放置策略 | Ingestion Artifacts | `artifacts.py` | `config.py` |
| 理解 `python -m services.ingest` CLI 子命令 | Ingestion CLI | `cli.py` | `pipeline.py` |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `IngestionPipeline` | `pipeline.py` | L:75 |
| `MemoryStore` | `store.py` | L:36 |
| `Config` | `config.py` | L:42 |
| `IngestionMode` | `config.py` | L:28 |
| `get_reader` | `readers.py` | L:1030 |
| `YouTubeTranscriptReader` | `readers.py` | L:198 |
| `Sectioner` | `sectioning.py` | L:139 |
| `IngestLLM` | `llm.py` | L:29 |
| `KnowledgeExtractor` | `llm.py` | L:143 |
| `run_ingestion_jobs_step` | `jobs.py` | L:163 |
| `prepare_artifact_info` | `artifacts.py` | L:22 |
| `main` | `cli.py` | L:457 |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `__init__.py` | Ingestion Pipeline | ← 同层各模块 | 保持原单模块公共接口的重导出面 |
| `__main__.py` | Ingestion CLI | ← `cli.py` | `python -m services.ingest` 入口 |
| `pipeline.py` | Ingestion Pipeline | ← `core/integration_reliability.py`, 同层各模块 \| → `slow_ingest_rlm.py` 等 | 摄入流水线主流程与归档内容处理（大文件 1162 行） |
| `readers.py` | Document Readers | ← `config.py` \| → `pipeline.py`, `cli.py` | 多格式文档读取器与 reader 选择（大文件 1069 行） |
| `sectioning.py` | Sectioning | ← `config.py` \| → `pipeline.py`, `cli.py` | 锚点推导、定位符分配与分节器 |
| `config.py` | Ingestion Config | ← — \| → 同层全部模块 | 摄入配置、模式与数据模型 |
| `llm.py` | Ingestion LLM | ← `core/llm.py`, `core/usage.py`, `config.py` \| → `pipeline.py` | 摄入用 LLM 客户端、评估器与知识抽取器 |
| `store.py` | Ingestion Store | ← `core/cognitive_memory_api.py`, `config.py` \| → `pipeline.py`, `cli.py` | 将抽取结果写入认知记忆 |
| `jobs.py` | Ingestion Jobs | ← `core/tools/ingest.py`(延迟) \| → — | 后台摄入任务的领取与处理步骤 |
| `artifacts.py` | Ingestion Artifacts | ← — \| → — | 原始产物在 DB 与磁盘之间的放置策略 |
| `cli.py` | Ingestion CLI | ← 同层各模块 \| → — | 摄入命令行子命令（ingest/status/backfill/process） |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `config.py` | — | `readers.py`, `sectioning.py`, `llm.py`, `store.py`, `pipeline.py`, `cli.py` |
| `readers.py` | `config.py` | `pipeline.py`, `cli.py` |
| `sectioning.py` | `config.py` | `pipeline.py`, `cli.py` |
| `llm.py` | `config.py` | `pipeline.py` |
| `store.py` | `config.py` | `pipeline.py`, `cli.py` |
| `pipeline.py` | `config.py`, `llm.py`, `readers.py`, `sectioning.py`, `store.py` | `cli.py` |
| `cli.py` | `config.py`, `pipeline.py`, `sectioning.py`, `store.py` | `__main__.py` |
| `jobs.py` | — | — |
| `__main__.py` | `cli.py` | — |
| `__init__.py` | 同层各模块 | — |
