---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — core/providers/

## Summary
`core/providers/` 实现不依赖官方 SDK 的 LLM provider 运行时，被 `core/llm.py` 按 provider 惰性调用（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解 Anthropic Messages 的 HTTP 客户端（api-key 与 OAuth 两种模式） | Provider Runtime | `anthropic_http.py` | `core/llm.py` |
| 理解 Google Cloud Code Assist 的 SSE 流式实现 | Provider Runtime | `google_code_assist.py` | `core/llm.py` |
| 理解 provider 如何被 `llm.py` 选择与分派 | Provider Runtime | `anthropic_http.py` | `google_code_assist.py`, `core/llm.py` |
| 理解 SSE 事件解析与错误处理契约 | Provider Runtime | `google_code_assist.py` | `core/integration_reliability.py` |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `anthropic_http_completion` | `anthropic_http.py` | L:194 |
| `stream_anthropic_http_completion` | `anthropic_http.py` | L:225 |
| `google_code_assist_completion` | `google_code_assist.py` | L:292 |
| `stream_google_code_assist_completion` | `google_code_assist.py` | L:323 |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `__init__.py` | Provider Runtime | ← — \| → — | 包文档，标记非 SDK provider 实现所在 |
| `anthropic_http.py` | Provider Runtime | ← `core/integration_reliability.py` \| → `core/llm.py` | 基于 HTTP 的 Anthropic Messages 客户端（支持 api-key 与 OAuth） |
| `google_code_assist.py` | Provider Runtime | ← `core/integration_reliability.py` \| → `core/llm.py` | Google Cloud Code Assist 的 SSE 流式客户端 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `anthropic_http.py` | — | — |
| `google_code_assist.py` | — | — |
