---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — tests/fixtures/

## Summary
`tests/fixtures/` 存放测试用的黄金样本数据：HMX 规范摘要与 SQL prompt 渲染的期望输出，供逐字节/逐字符比对（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解 HMX digest 的字节契约黄金样本 | Digest 黄金样本 | `digest/README.md` | `digest/audit_record_digest_v1.json`, `digest/protected_section_digest_v1.json` |
| 理解 SQL prompt 渲染的期望输出 | Prompt 黄金样本 | `prompt_render/` | `db/test_prompt_render.py` |

## Subdirectories
| Dir | Domain | Depends On | Purpose |
|---|---|---|---|
| `digest/` | Digest 黄金样本 | `db/57`, `core/digest.py` | HMX 规范摘要（audit record / protected section）的字节级黄金样本。 |
| `prompt_render/` | Prompt 黄金样本 | `db/39` | 心跳/聊天/潜意识等 prompt 渲染函数的期望输出文本。 |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `digest/README.md` | Digest 黄金样本 | — | 说明黄金样本的来源与再生成方式。 |
| `digest/audit_record_digest_v1.json` | Digest 黄金样本 | `db/57` | audit record 摘要 v1 期望值。 |
| `digest/protected_section_digest_v1.json` | Digest 黄金样本 | `db/57` | protected section 摘要 v1 期望值。 |
| `prompt_render/*.txt` | Prompt 黄金样本 | `db/39` | 21 个渲染期望输出（heartbeat/chatctx/personhood/subconscious 等场景）。 |
