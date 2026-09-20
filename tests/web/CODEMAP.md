---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — tests/web/

## Summary
`tests/web/` 是 9 个针对 `apps/hexis_api.py`（dashboard 后端与 HTTP API）的测试模块，覆盖端点、附件、收件箱回复、初始化认证、集成动作与语音 API（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解核心 HTTP API 端点与路由 | API 端点 | `test_web.py` | `test_attachments_api.py` |
| 理解初始化与认证流程 | 初始化/认证 | `test_init_auth.py` | `test_web.py` |
| 理解收件箱回复与附件上传 | 收件箱/附件 | `test_inbox_reply.py` | `test_attachments_api.py` |
| 理解 OpenAI 兼容端点 | OpenAI 兼容 | `test_openai_compat.py` | `test_web.py` |
| 理解集成动作与生活集成 API | 集成 API | `test_integration_action_api.py` | `test_life_integration_api.py` |
| 理解语音 API | 语音 API | `test_speech_api.py` | `test_web.py` |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `temp_test_db` fixture | `../conftest.py` | L:41 |
| `db_pool` fixture | `../conftest.py` | L:116 |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_web.py` | API 端点 | `apps.hexis_api` | Hexis API server 核心端点。 |
| `test_init_auth.py` | 初始化/认证 | `apps.hexis_api`, `core.auth.ui_flow` | 初始化认证流。 |
| `test_attachments_api.py` | 收件箱/附件 | `apps.hexis_api` | 附件上传/读取 API。 |
| `test_inbox_reply.py` | 收件箱/附件 | `apps.hexis_api` | 收件箱回复。 |
| `test_openai_compat.py` | OpenAI 兼容 | `apps.hexis_api` | OpenAI 兼容端点。 |
| `test_integration_action_api.py` | 集成 API | `apps.hexis_api` | 集成动作 API。 |
| `test_life_integration_api.py` | 集成 API | `apps.hexis_api`, `core.tools` | 生活集成 API。 |
| `test_speech_api.py` | 语音 API | `apps.hexis_api`, `services.speech` | 语音合成 API。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| 全部测试模块 | `../conftest.py`（fixture） | — |
