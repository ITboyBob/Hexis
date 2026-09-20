---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — hexis-ui/app/api/

## Summary
Next.js Route Handlers（82 个 `route.ts`）：每个 API 路由要么经 `lib/prisma.ts` 直连 Postgres，要么经 `lib/python-api.ts` 代理到 Python API 服务；按功能域分组为 init/chat/ingest/memories/goals/settings/voice/PWA 等（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解初始化向导各步骤的 API 端点 | Init API | `init/status/route.ts` | `init/llm/route.ts`, `init/personality/route.ts`, `init/consent/request/route.ts` |
| 理解初始化中的 OAuth 透传 | Init API | `init/auth/[...path]/route.ts` | `lib/python-api.ts` |
| 理解角色卡目录/导入/保存端点 | Init API | `init/character-catalog/route.ts` | `init/characters/save/route.ts`, `init/import-card/route.ts` |
| 理解聊天消息与会话端点 | Chat API | `chat/route.ts` | `chat/session/route.ts`, `chat/session/[id]/route.ts` |
| 理解运行时事件流（SSE）端点 | Events API | `events/stream/route.ts` | `hooks/use-gateway-events.ts` |
| 理解记忆列表/详情/图谱/历史端点 | Memories API | `memories/route.ts` | `memories/graph/route.ts`, `memories/history/route.ts`, `memories/[id]/route.ts` |
| 理解目标 CRUD 端点 | Goals API | `goals/route.ts` | `goals/[id]/route.ts` |
| 理解摄取（文件/URL/作业）端点 | Ingest API | `ingest/file/route.ts` | `ingest/url/route.ts`, `ingest/jobs/route.ts`, `ingest/jobs/[id]/route.ts` |
| 理解文档与 RecMem 桌面端点 | Documents API | `documents/route.ts` | `documents/[id]/route.ts`, `desk/route.ts` |
| 理解保留/遗忘决策端点 | Retention API | `retention/route.ts` | `retention/decide/route.ts`, `fade/decide/route.ts` |
| 理解矛盾检测与决策端点 | Contradictions API | `contradictions/route.ts` | `contradictions/decide/route.ts` |
| 理解用户模型 claims 复核端点 | User Model API | `user-model/claims/route.ts` | `user-model/claims/[id]/review/route.ts` |
| 理解学习复核决策端点 | Learning Review API | `learning-review/route.ts` | `learning-review/decide/route.ts` |
| 理解职责与环境责任端点 | Responsibilities API | `responsibilities/route.ts` | `responsibilities/[id]/route.ts` |
| 理解设置（工具开关/语音输入输出）端点 | Settings API | `settings/route.ts` | `settings/tools/route.ts`, `settings/voice-output/route.ts` |
| 理解语音状态/转写/合成端点 | Voice API | `voice/status/route.ts` | `voice/transcribe/route.ts`, `voice/synthesize/route.ts`, `voice/audio/[id]/route.ts` |
| 理解 PWA 推送订阅与 presence 端点 | PWA API | `pwa/push/subscriptions/route.ts` | `pwa/push/config/route.ts`, `pwa/presence/route.ts` |
| 理解集成状态与动作端点 | Integrations API | `integrations/status/route.ts` | `integrations/action/route.ts` |
| 理解外发/收件箱/出站回复端点 | Outbound API | `outbound/route.ts` | `outbox/route.ts`, `outbox/reply/route.ts` |
| 理解用量/状态/心跳触发端点 | Usage & Status API | `usage/route.ts` | `status/route.ts`, `heartbeat/run/route.ts` |
| 理解附件上传与摄取端点 | Attachments API | `attachments/route.ts` | `attachments/[id]/ingest/route.ts` |
| 理解节点、资源请求与自动化决策端点 | Nodes & Requests API | `nodes/route.ts` | `requests/decide/route.ts`, `automations/decide/route.ts`, `connector-importance/route.ts` |

## Files
> 共 82 个 `route.ts`，按功能域分组；每个文件是一个 Next.js Route Handler（GET/POST/PATCH/DELETE）。

### Init API
| File | Domain | Deps | Function |
|---|---|---|---|
| `init/status/route.ts` | Init API | `lib/prisma.ts`, `lib/python-api.ts` | 初始化状态查询。 |
| `init/mode/route.ts` | Init API | `lib/prisma.ts` | 初始化模式选择（Express/Character/Custom）。 |
| `init/defaults/route.ts` | Init API | `lib/prisma.ts` | 初始化默认值。 |
| `init/models/route.ts` | Init API | `lib/python-api.ts` | 可用模型列表。 |
| `init/llm/route.ts` | Init API | `lib/init-llm.ts` | LLM provider/model 配置。 |
| `init/personality/route.ts` | Init API | `lib/prisma.ts` | 人格特质配置。 |
| `init/values/route.ts` | Init API | `lib/prisma.ts` | 核心价值观配置。 |
| `init/worldview/route.ts` | Init API | `lib/prisma.ts` | 世界观配置。 |
| `init/boundaries/route.ts` | Init API | `lib/prisma.ts` | 边界配置。 |
| `init/identity/route.ts` | Init API | `lib/prisma.ts` | 身份配置。 |
| `init/interests/route.ts` | Init API | `lib/prisma.ts` | 兴趣配置。 |
| `init/goals/route.ts` | Init API | `lib/prisma.ts` | 初始目标配置。 |
| `init/relationship/route.ts` | Init API | `lib/prisma.ts` | 关系配置。 |
| `init/timezone/route.ts` | Init API | `lib/prisma.ts` | 时区配置。 |
| `init/heartbeat/route.ts` | Init API | `lib/prisma.ts` | 心跳配置。 |
| `init/character-card/route.ts` | Init API | `lib/character-catalog.ts` | 角色卡读取。 |
| `init/characters/route.ts` | Init API | `lib/character-catalog.ts` | 角色列表。 |
| `init/characters/image/route.ts` | Init API | `lib/character-catalog.ts` | 角色头像。 |
| `init/characters/save/route.ts` | Init API | `lib/prisma.ts` | 角色保存。 |
| `init/character-catalog/route.ts` | Init API | `lib/character-catalog.ts` | 角色目录抓取。 |
| `init/adapt-character-card/route.ts` | Init API | `lib/character-catalog.ts` | 角色卡适配。 |
| `init/import-card/route.ts` | Init API | `lib/character-catalog.ts` | 导入外部角色卡。 |
| `init/reset/route.ts` | Init API | `lib/prisma.ts` | 重置初始化。 |
| `init/consent/request/route.ts` | Init API | `lib/prisma.ts` | 发起 consent 请求。 |
| `init/consent/override/route.ts` | Init API | `lib/prisma.ts` | 操作者 consent 覆盖。 |
| `init/auth/[...path]/route.ts` | Init API | `lib/python-api.ts` | 初始化期 OAuth 透传到 Python API。 |

### Chat & Events API
| File | Domain | Deps | Function |
|---|---|---|---|
| `chat/route.ts` | Chat API | `lib/python-api.ts` | 聊天回合（SSE 代理）。 |
| `chat/session/route.ts` | Chat API | `lib/prisma.ts` | 聊天会话列表/创建。 |
| `chat/session/[id]/route.ts` | Chat API | `lib/prisma.ts` | 单会话历史。 |
| `events/stream/route.ts` | Events API | `lib/python-api.ts` | 运行时事件 SSE 流。 |

### Memories & Documents API
| File | Domain | Deps | Function |
|---|---|---|---|
| `memories/route.ts` | Memories API | `lib/prisma.ts` | 记忆列表/检索。 |
| `memories/[id]/route.ts` | Memories API | `lib/prisma.ts` | 单条记忆详情。 |
| `memories/graph/route.ts` | Memories API | `lib/prisma.ts` | 记忆图谱数据。 |
| `memories/history/route.ts` | Memories API | `lib/prisma.ts` | 记忆变更历史。 |
| `documents/route.ts` | Documents API | `lib/prisma.ts` | 文档列表。 |
| `documents/[id]/route.ts` | Documents API | `lib/prisma.ts` | 文档详情。 |
| `desk/route.ts` | Documents API | `lib/prisma.ts` | RecMem 桌面 list/open/pin/clear。 |
| `attachments/route.ts` | Attachments API | `lib/python-api.ts` | 附件上传。 |
| `attachments/[id]/ingest/route.ts` | Attachments API | `lib/python-api.ts` | 附件摄取。 |

### Ingest & Goals API
| File | Domain | Deps | Function |
|---|---|---|---|
| `ingest/route.ts` | Ingest API | `lib/python-api.ts` | 摄取总入口/状态。 |
| `ingest/file/route.ts` | Ingest API | `lib/python-api.ts` | 文件摄取。 |
| `ingest/url/route.ts` | Ingest API | `lib/python-api.ts` | URL 摄取。 |
| `ingest/jobs/route.ts` | Ingest API | `lib/python-api.ts` | 摄取作业列表。 |
| `ingest/jobs/[id]/route.ts` | Ingest API | `lib/python-api.ts` | 摄取作业详情。 |
| `goals/route.ts` | Goals API | `lib/prisma.ts` | 目标列表/创建。 |
| `goals/[id]/route.ts` | Goals API | `lib/prisma.ts` | 目标更新/完成。 |

### Governance & Review API
| File | Domain | Deps | Function |
|---|---|---|---|
| `contradictions/route.ts` | Contradictions API | `lib/prisma.ts` | 矛盾列表。 |
| `contradictions/decide/route.ts` | Contradictions API | `lib/prisma.ts` | 矛盾决策。 |
| `retention/route.ts` | Retention API | `lib/prisma.ts` | 保留/遗忘状态。 |
| `retention/decide/route.ts` | Retention API | `lib/prisma.ts` | 保留决策。 |
| `fade/decide/route.ts` | Retention API | `lib/prisma.ts` | 衰减退场决策。 |
| `user-model/claims/route.ts` | User Model API | `lib/prisma.ts` | 用户模型 claims 列表。 |
| `user-model/claims/[id]/review/route.ts` | User Model API | `lib/prisma.ts` | claims 复核。 |
| `learning-review/route.ts` | Learning Review API | `lib/prisma.ts` | 学习复核列表。 |
| `learning-review/decide/route.ts` | Learning Review API | `lib/prisma.ts` | 学习复核决策。 |
| `responsibilities/route.ts` | Responsibilities API | `lib/prisma.ts` | 环境职责列表。 |
| `responsibilities/[id]/route.ts` | Responsibilities API | `lib/prisma.ts` | 职责详情/动作。 |
| `automations/decide/route.ts` | Nodes & Requests API | `lib/prisma.ts` | 自动化建议决策。 |
| `requests/decide/route.ts` | Nodes & Requests API | `lib/prisma.ts` | 资源请求裁决。 |
| `connector-importance/route.ts` | Nodes & Requests API | `lib/prisma.ts` | 连接器重要性。 |
| `questions/answer/route.ts` | Nodes & Requests API | `lib/prisma.ts` | 澄清问题作答。 |

### Settings, Voice & PWA API
| File | Domain | Deps | Function |
|---|---|---|---|
| `settings/route.ts` | Settings API | `lib/prisma.ts` | 设置读写。 |
| `settings/tools/route.ts` | Settings API | `lib/prisma.ts` | 工具开关。 |
| `settings/tools/search/route.ts` | Settings API | `lib/prisma.ts` | 工具搜索。 |
| `settings/voice-notes/route.ts` | Settings API | `lib/prisma.ts` | 语音留言设置。 |
| `settings/voice-output/route.ts` | Settings API | `lib/prisma.ts` | 语音输出设置。 |
| `voice/status/route.ts` | Voice API | `lib/python-api.ts` | 语音边车状态。 |
| `voice/audio/[id]/route.ts` | Voice API | `lib/python-api.ts` | 语音音频取回。 |
| `voice/transcribe/route.ts` | Voice API | `lib/python-api.ts` | 语音转写。 |
| `voice/synthesize/route.ts` | Voice API | `lib/python-api.ts` | 语音合成。 |
| `pwa/push/config/route.ts` | PWA API | `lib/prisma.ts` | Web Push 配置。 |
| `pwa/push/subscriptions/route.ts` | PWA API | `lib/prisma.ts` | 推送订阅注册。 |
| `pwa/presence/route.ts` | PWA API | `lib/prisma.ts` | 前台 presence 门控。 |

### Integrations, Outbound & Runtime API
| File | Domain | Deps | Function |
|---|---|---|---|
| `integrations/status/route.ts` | Integrations API | `lib/prisma.ts` | 连接器状态。 |
| `integrations/action/route.ts` | Integrations API | `lib/python-api.ts` | 连接器动作。 |
| `outbound/route.ts` | Outbound API | `lib/prisma.ts` | 外发台账。 |
| `outbox/route.ts` | Outbound API | `lib/prisma.ts` | Web 收件箱拉取。 |
| `outbox/reply/route.ts` | Outbound API | `lib/prisma.ts` | 收件箱回复。 |
| `usage/route.ts` | Usage & Status API | `lib/usage-summary.ts` | 用量与成本摘要。 |
| `status/route.ts` | Usage & Status API | `lib/python-api.ts` | Agent 状态。 |
| `heartbeat/run/route.ts` | Usage & Status API | `lib/python-api.ts` | 手动触发心跳。 |
| `nodes/route.ts` | Nodes & Requests API | `lib/prisma.ts` | 伴随节点列表/状态。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `init/status/route.ts` | `init/defaults/route.ts`（共享 lib） | 全部 `init/*` 路由（同源 `lib/init-*`） |
| `chat/route.ts` | — | `chat/session/*`（同源 `lib/python-api.ts`） |
| `memories/route.ts` | — | `memories/[id]`, `memories/graph`, `memories/history` |
| `ingest/file/route.ts` | `ingest/url/route.ts`（同源 `lib/python-api.ts`） | `ingest/jobs/*` |
| `retention/route.ts` | — | `retention/decide`, `fade/decide` |
