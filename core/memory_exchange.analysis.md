---
source: memory_exchange.py
lines: 2986
generated_at: 2026-09-20
---

> HMX v1.7 记忆交换的策略层与执行层：定义各导出意图的区块策略、构造信封、执行导出/导入/暂存/评审全流程。

## Feature Index
| Intent | Lines | Notes |
|---|---|---|
| 理解 HMX 意图（port/duplicate/telepathy/analysis）与区块安全策略 | L:30-L:107 | `PROTECTED_SECTIONS` / `ALL_SECTIONS` / `DEFAULT_IMPORT_STRATEGY` 常量定义，是后续所有策略判断的依据 |
| 校验 HMX 文档是否符合规范 wire schema | L:203-L:353 | `load_hmx_schema` + `validate_hmx_document` + 内部校验辅助，被导入路径复用 |
| 按意图解析导出应包含的区块 | L:368-L:495 | `SectionPlan` / `resolve_export_sections` / `normalize_replace_sections` |
| 构造导出信封（capabilities + provenance + 统计） | L:508-L:809 | `build_envelope` 与 `_ref`/`_enrich_provenance`/`_postprocess_section`/`_fill_statistics` 强耦合 |
| 执行一次完整导出 | L:810-L:917 | `export_hmx`，调用信封构造与 DB SQL 函数 |
| 校验心智连续性（源与目标身份） | L:918-L:1029 | `verify_mind_continuity`，telepathy/analysis 的安全闸门 |
| JSONL 流式序列化与解析 | L:1030-L:1194 | `iter_hmx_jsonl` 与 `parse_hmx_jsonl` 互为逆操作 |
| 预演导入并给出影响预测（不改状态） | L:1512-L:2029 | `dry_run_hmx` 是导入的前置预测；与 `import_hmx` 共享区块/策略判断逻辑 |
| 暂存导入到 deliberative 评审库 | L:2090-L:2170 | `stage_hmx`，仅写 `hmx_import_staging` |
| 加载到物理隔离的分析存储 | L:2171-L:2239 | `load_analysis_hmx`，analysis_only 路径 |
| 执行导入（additive/authoritative/deliberative/analysis_only） | L:2240-L:2681 | `import_hmx` 按 strategy 分派，authoritative 委派给 `core/protected_replacement.py` |
| 评审暂存记录（接受/拒绝/修改/报价） | L:2682-L:2957 | `accept_staged_import` / `reject_staged_import` / `modify_staged_import` / `quote_staged_import` |
| 分析记录与暂存记录之间的升降级 | L:2958-L:2986 | `promote_analysis_to_staged` / `demote_staged_to_analysis` |
| 为受保护区块准备导入/还原载荷 | L:1380-L:1511 | `_protected_memory_record` / `prepare_protected_section_import` / `prepare_protected_section_restore`，供 authoritative 路径使用 |

## Symbols
| Symbol | Type | Line |
|---|---|---|
| `HmxPolicyError` | class | L:108 |
| `HmxSchemaError` | class | L:112 |
| `HmxImportResult` | class | L:117 |
| `HmxAuthoritativeResult` | class | L:133 |
| `HmxDryRunResult` | class | L:153 |
| `HmxStagingResult` | class | L:171 |
| `HmxAnalysisResult` | class | L:183 |
| `HmxReviewResult` | class | L:194 |
| `load_hmx_schema` | function | L:203 |
| `validate_hmx_document` | function | L:213 |
| `validate_intent` | function | L:355 |
| `new_export_id` | function | L:363 |
| `SectionPlan` | class | L:368 |
| `resolve_export_sections` | function | L:382 |
| `default_import_strategy` | function | L:475 |
| `normalize_replace_sections` | function | L:480 |
| `build_capabilities` | function | L:496 |
| `build_envelope` | function | L:508 |
| `export_hmx` | function | L:810 |
| `verify_mind_continuity` | function | L:918 |
| `iter_hmx_jsonl` | function | L:1030 |
| `parse_hmx_jsonl` | function | L:1080 |
| `load_source_context` | function | L:1195 |
| `prepare_protected_section_import` | function | L:1415 |
| `prepare_protected_section_restore` | function | L:1484 |
| `dry_run_hmx` | function | L:1512 |
| `stage_hmx` | function | L:2090 |
| `load_analysis_hmx` | function | L:2171 |
| `import_hmx` | function | L:2240 |
| `pending_hmx_reviews` | function | L:2696 |
| `accept_staged_import` | function | L:2700 |
| `reject_staged_import` | function | L:2792 |
| `modify_staged_import` | function | L:2818 |
| `quote_staged_import` | function | L:2887 |
| `promote_analysis_to_staged` | function | L:2958 |
| `demote_staged_to_analysis` | function | L:2981 |

## Logical Sections
| Lines | Content |
|---|---|
| L:1-L:107 | 模块文档、版本/意图/策略/区块常量定义 |
| L:108-L:202 | 异常与结果 dataclass（policy/schema/import/authoritative/dry-run/staging/analysis/review） |
| L:203-L:367 | schema 加载与文档/意图校验、export id 生成 |
| L:368-L:579 | 导出区块策略与信封构造起点（SectionPlan、capabilities、build_envelope） |
| L:580-L:1229 | 导出流水线（信封后处理、export_hmx、心智连续性校验、JSONL 编解码、源上下文） |
| L:1230-L:1511 | 增量导入准备：区块归一化、provenance 追加、受保护区块载荷准备 |
| L:1512-L:2089 | dry-run 预演与隔离记录交换辅助 |
| L:2090-L:2239 | 暂存（staging）与分析（analysis）记录加载 |
| L:2240-L:2681 | 导入主流程（按 strategy 分派，含 authoritative 委派） |
| L:2682-L:2986 | 评审决策、报价与暂存/分析升降级 |
