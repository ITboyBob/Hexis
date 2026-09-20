---
source: hexis_api.py
lines: 3845
generated_at: 2026-09-20
---

> Hexis FastAPI 服务：在 lifespan 中建 asyncpg 连接池并跑迁移，向 Next.js 面板暴露 SSE 聊天流、OpenAI 兼容端点、集成/职责/用户模型/ingest/voice/PWA/init 等 REST 与 WebSocket 路由。

## Feature Index
| Intent | Lines | Notes |
|---|---|---|
| 理解应用启动/关闭与连接池、迁移、build-change 记录 | L:57-L:148 | lifespan 设置全局 `_pool`，退出时关闭 MCP runtime 与池 |
| 理解 Bearer 鉴权中间件（Pro 可选 API key） | L:125-L:148 | `_BearerAuthMiddleware` |
| 理解请求体 Pydantic 模型全集（chat/ingest/voice/init/PWA） | L:150-L:365 | 大量 `BaseModel` 定义 |
| 理解 SSE 事件序列化与 OpenAI 错误格式 | L:367-L:444 | `_sse_event`、`_openai_sse_data`、`_openai_error`、`_record_consent_trace` |
| 理解 OpenAI 兼容模型发现与请求准备 | L:446-L:598 | `_active_openai_model`、`_openai_model_object`、`_prepare_openai_chat`、`_openai_agent_events` |
| 理解 init 阶段的 OAuth/授权路由 | L:599-L:669 | `/api/init/auth/*`、`/api/init/models/openai-codex` |
| 理解健康检查与根路由 | L:669-L:686 | `/health`、`/` |
| 理解 Gmail/Spotify OAuth 回调的 HTML 页面 | L:686-L:924 | `_gmail_callback_page`、`_handle_gmail_oauth_callback`、`_handle_spotify_oauth_callback` |
| 理解状态与网页收件箱回复 | L:924-L:1055 | `/api/status`、`/api/inbox/reply` |
| 理解集成状态/动作（connector 清单与工具调用） | L:1055-L:1434 | `_integration_status_payload`、`_integration_action_arguments`、`_tool_result_payload`、`_tee_outbox_to_web_inbox` |
| 理解 outbound 台账与控制 | L:1434-L:1484 | `/api/outbound`、`/api/outbound/control` |
| 理解节点（companion node）WebSocket 与配对/吊销 | L:1484-L:1551 | `/api/nodes/connect`、`/api/nodes`、`/api/nodes/pairing`、`/api/nodes/revoke` |
| 理解职责（responsibilities）列表/详情/动作 | L:1551-L:1647 | 三个 `/api/responsibilities*` 路由 |
| 理解用户模型 claims 查询与复核 | L:1647-L:1693 | `/api/user-model/claims`、`/api/user-model/claims/{id}/review` |
| 理解 connector 重要性评分 | L:1693-L:1751 | `/api/connector-importance` |
| 理解入站 webhook 与 Slack 交互回调 | L:1751-L:1887 | `/api/webhook/{source}`、Slack 签名校验、`/api/slack/interactivity` |
| 理解 SSE：gateway 事件流与手动 heartbeat 流 | L:1887-L:2112 | `/api/events/stream`、`/api/heartbeat/run`、`_sse_event_stream`、`_stream_manual_heartbeat` |
| 理解聊天 SSE 主流程 `_stream_chat` | L:3025-L:3339 | 与 `/api/chat` 配对；事件映射为前端 token/phase |
| 理解 ingest：文本/URL/文件/附件准备与作业查询 | L:2112-L:2812 | `/api/ingest/text`、`/api/ingest/url`、`/api/ingest/file`、`/api/attachments`、`/api/attachments/{id}/ingest`、`/api/ingest/jobs*` |
| 理解 voice：转写/合成/音频/状态 | L:2466-L:2597 | `/api/voice/transcribe`、`/api/voice/synthesize`、`/api/voice/audio/{id}`、`/api/voice/status` |
| 理解 PWA：推送配置/订阅/取消与在线状态 | L:2596-L:2720 | `/api/pwa/push/config`、`/api/pwa/push/subscriptions`、`/api/pwa/presence` |
| 理解 OpenAI 兼容 `/v1/models` 与 `/v1/chat/completions` | L:2811-L:3025 | 缓冲与流式两条路径 |
| 理解 init consent：请求/覆盖与既有 consent 复用 | L:3339-L:3821 | `_resolve_fallback_api_key`、`_fetch_consent_record`、`_apply_existing_consent`、`init_consent_request/override` |
| 理解 uvicorn 启动入口与默认端口 | L:3822-L:3845 | `main`，默认 127.0.0.1:43817 |

## Symbols
| Symbol | Type | Line |
|---|---|---|
| `app` | constant | L:107 |
| `lifespan` | function | L:65 |
| `main` | function | L:3822 |
| `_BearerAuthMiddleware` | class | L:125 |
| `ChatRequest` | class | L:178 |
| `ChatAttachment` | class | L:159 |
| `ChatVisualAttachment` | class | L:150 |
| `IngestTextRequest` | class | L:248 |
| `IngestUrlRequest` | class | L:257 |
| `IntegrationActionRequest` | class | L:264 |
| `ResponsibilityActionRequest` | class | L:272 |
| `SpeechSynthesisRequest` | class | L:210 |
| `WebPushSubscriptionRequest` | class | L:223 |
| `OpenAIChatCompletionRequest` | class | L:322 |
| `InitConsentRequest` | class | L:347 |
| `_stream_chat` | function | L:3025 |
| `_sse_event_stream` | function | L:2055 |
| `_stream_manual_heartbeat` | function | L:1919 |
| `integration_action` | function | L:1283 |
| `init_consent_request` | function | L:3470 |

## Logical Sections
| Lines | Content |
|---|---|
| L:1-L:149 | 模块导入、全局池、lifespan、CORS 与 Bearer 中间件 |
| L:150-L:444 | Pydantic 请求模型与 SSE/OpenAI 序列化辅助 |
| L:446-L:686 | OpenAI 兼容准备、init auth 路由、健康检查 |
| L:686-L:1055 | OAuth 回调 HTML、状态、网页收件箱回复 |
| L:1055-L:1551 | 集成状态/动作、outbound、companion node |
| L:1551-L:1887 | responsibilities、user-model、connector-importance、webhook、Slack |
| L:1887-L:2112 | SSE 事件流与手动 heartbeat 流 |
| L:2112-L:3025 | chat/ingest/attachments/voice/PWA 路由与辅助 |
| L:3025-L:3845 | `_stream_chat`、OpenAI 兼容端点、init consent、`main` |
