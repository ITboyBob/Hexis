---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — channels/

## Summary
多渠道消息适配层：把 Discord/Telegram/Slack/Signal/WhatsApp/iMessage/Matrix 的原生消息归一化为 `ChannelMessage`，经 `ChannelManager` 与 `conversation` 走记忆增强对话管线，并由 `outbox` 消费器实现主动外发。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解跨平台消息与适配器的核心抽象 | Channel Core | `base.py` | `media.py`, `presentation.py` |
| 理解多渠道编排：启动/重启/入站路由/统一发送 | Channel Orchestration | `manager.py` | `conversation.py`, `commands.py` |
| 理解入站消息如何进入记忆增强对话管线 | Channel Conversation | `conversation.py` | `base.py`, `services/chat.py` |
| 理解流式回复的渐进式编辑合并（防限流） | Channel Streaming | `streaming.py` | `manager.py` |
| 理解附件安全下载与 SSRF 防护 | Channel Media | `media.py` | `core/integration_reliability.py` |
| 理解跨平台可移植消息块与渲染 | Channel Presentation | `presentation.py` | `base.py`, `outbox.py` |
| 理解斜杠命令注册与分发 | Channel Commands | `commands.py` | `manager.py` |
| 理解心跳触发的主动外发（direct/last_active/broadcast） | Channel Outbox | `outbox.py` | `manager.py`, `presentation.py` |
| 理解 Discord 适配器的 bot 接入 | Channel Adapter | `discord_adapter.py` | `base.py` |
| 理解 Telegram 长轮询适配器 | Channel Adapter | `telegram_adapter.py` | `base.py` |
| 理解 Slack Socket Mode / HTTP events 适配器 | Channel Adapter | `slack_adapter.py` | `base.py` |
| 理解 Signal 经 signal-cli-rest-api 的 SSE 适配器 | Channel Adapter | `signal_adapter.py` | `base.py` |
| 理解 WhatsApp Business Cloud API 适配器 | Channel Adapter | `whatsapp_adapter.py` | `base.py` |
| 理解 iMessage 经 BlueBubbles 的轮询适配器 | Channel Adapter | `imessage_adapter.py` | `base.py` |
| 理解 Matrix homeserver 适配器 | Channel Adapter | `matrix_adapter.py` | `base.py` |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `ChannelAdapter` | `base.py` | L:82 |
| `ChannelMessage` | `base.py` | L:37 |
| `ChannelCapabilities` | `base.py` | L:24 |
| `ChannelManager` | `manager.py` | L:54 |
| `process_channel_message` | `conversation.py` | L:120 |
| `stream_channel_message` | `conversation.py` | L:270 |
| `MessagePresentation` | `presentation.py` | L:122 |
| `render_presentation` | `presentation.py` | L:421 |
| `presentation_from_text` | `presentation.py` | L:362 |
| `citations_from_tool_output` | `presentation.py` | L:339 |
| `ChannelOutboxConsumer` | `outbox.py` | L:74 |
| `StreamCoalescer` | `streaming.py` | L:32 |
| `Attachment` | `media.py` | L:46 |
| `parse_allowlist` | `base.py` | L:231 |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `__init__.py` | Channel Core | ← `—` | 汇总导出适配器抽象、管理器、附件与 presentation 类型。 |
| `base.py` | Channel Core | ← `—` \| → 2 files; rg "from channels.base" --type py -l | 定义 `ChannelMessage`/`ChannelAdapter`/`ChannelCapabilities` 与 allowlist、分块辅助。 |
| `commands.py` | Channel Commands | ← `core.agent_api`, `core.cognitive_memory_api` | 斜杠命令的注册表与内建 status/recall/goals/energy/help。 |
| `conversation.py` | Channel Conversation | ← `core.agent_api`, `services.chat` | 入站消息走记忆增强对话管线，并管理按发送者的会话状态。 |
| `discord_adapter.py` | Channel Adapter | ← `—` \| → `services.channel_worker.py` | 经 bot token 连接 Discord 的适配器。 |
| `imessage_adapter.py` | Channel Adapter | ← `core.integration_reliability` \| → `services.channel_worker.py` | 经 BlueBubbles 轮询 REST 的 iMessage 适配器。 |
| `manager.py` | Channel Orchestration | ← `core.integration_reliability` \| → `services.channel_worker.py` | 编排多个适配器：启动、退避重启、入站路由与统一发送。 |
| `matrix_adapter.py` | Channel Adapter | ← `—` \| → `services.channel_worker.py` | 经 matrix-nio 的 Matrix 适配器（sync_forever + room_send）。 |
| `media.py` | Channel Media | ← `core.integration_reliability` \| → 2 files; rg "from channels.media" --type py -l | 附件归一化、安全下载、字节物化与 SSRF 防护。 |
| `outbox.py` | Channel Outbox | ← `core.integration_reliability` \| → `services.channel_worker.py` | 消费 RabbitMQ outbox，把心跳主动消息投递到目标渠道。 |
| `presentation.py` | Channel Presentation | ← `—` \| → 4 files; rg "from channels.presentation" --type py -l | 可移植的展示块模型、规范化、从工具输出抽取引用与渲染。 |
| `signal_adapter.py` | Channel Adapter | ← `core.integration_reliability` \| → 3 files; rg "from channels.signal_adapter" --type py -l | 经 signal-cli-rest-api 的 SSE 入站/HTTP 出站适配器。 |
| `slack_adapter.py` | Channel Adapter | ← `—` \| → 4 files; rg "from channels.slack_adapter" --type py -l | 经 slack-bolt 的 Socket Mode/HTTP events 适配器。 |
| `streaming.py` | Channel Streaming | ← `—` | 流式 token 合并为周期性消息编辑，供支持编辑的渠道使用。 |
| `telegram_adapter.py` | Channel Adapter | ← `—` \| → 2 files; rg "from channels.telegram_adapter" --type py -l | 经 python-telegram-bot 长轮询的 Telegram 适配器。 |
| `whatsapp_adapter.py` | Channel Adapter | ← `core.integration_reliability` \| → `services.channel_worker.py` | 经 WhatsApp Business Cloud API 的 webhook 入站/REST 出站适配器。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `manager.py` | `base.py`, `commands.py`, `conversation.py`, `presentation.py` | `outbox.py`, `__init__.py` |
| `conversation.py` | `base.py`, `media.py`, `presentation.py`, `streaming.py` | `manager.py`, `__init__.py` |
| `base.py` | `presentation.py` | `manager.py`, `conversation.py`, `discord_adapter.py`, `imessage_adapter.py`, `matrix_adapter.py`, `signal_adapter.py`, `slack_adapter.py`, `telegram_adapter.py`, `whatsapp_adapter.py`, `streaming.py` |
| `presentation.py` | — | `base.py`, `conversation.py`, `manager.py`, `outbox.py`, `discord_adapter.py`, `slack_adapter.py`, `telegram_adapter.py` |
| `media.py` | — | `conversation.py`, `discord_adapter.py`, `imessage_adapter.py`, `matrix_adapter.py`, `signal_adapter.py`, `slack_adapter.py`, `telegram_adapter.py`, `whatsapp_adapter.py` |
| `outbox.py` | `manager.py`, `presentation.py` | — |
| `commands.py` | — | `manager.py` |
| `streaming.py` | `base.py` | `conversation.py` |
| `discord_adapter.py` | `base.py`, `media.py`, `presentation.py` | — |
| `telegram_adapter.py` | `base.py`, `media.py`, `presentation.py` | — |
| `slack_adapter.py` | `base.py`, `media.py`, `presentation.py` | — |
| `signal_adapter.py` | `base.py`, `media.py` | — |
| `whatsapp_adapter.py` | `base.py`, `media.py` | — |
| `imessage_adapter.py` | `base.py`, `media.py` | — |
| `matrix_adapter.py` | `base.py`, `media.py` | — |
