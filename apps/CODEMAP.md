---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — apps/

## Summary
Hexis 的全部进程入口与 CLI 子命令实现：`hexis_cli.py` 作为总调度，把命令分发给本目录的 `cli_*` 模块或转发给 `hexis_init`/`cli_chat`/`hexis_mcp_server`/`worker`，并由 `hexis_api.py` 提供 FastAPI 服务。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解 CLI 子命令分发（`func` 字符串如何路由到处理器） | CLI Entrypoint | `hexis_cli.py` | `cli_auth.py`, `cli_node.py`, `cli_execution.py` |
| 理解 argparse 子命令树与分组帮助的注册方式 | CLI Entrypoint | `hexis_cli.py` | — |
| 理解 Docker/Compose 生命周期与宿主 worker 判定 | CLI Stack Ops | `hexis_cli.py` | `core/host_services.py` |
| 理解交互式初始化向导的三条路径与 consent 流程 | Init Wizard | `hexis_init.py` | `apps/tui/` |
| 理解非交互式 init（provider/model/api-key 标志） | Init Wizard | `hexis_init.py` | `cli_auth.py` |
| 理解终端聊天 REPL 的流式输出与 slash 命令 | CLI Chat | `cli_chat.py` | `cli_prompts.py`, `cli_theme.py` |
| 理解 OAuth / 订阅 token 登录各 provider 的实现 | CLI Auth | `cli_auth.py` | `cli_theme.py` |
| 理解 companion node 的配对、唤醒与调用 | CLI Node | `cli_node.py` | `core/node_daemon.py` |
| 理解本地/SSH/远程 Docker 执行目标管理 | CLI Execution | `cli_execution.py` | `core/execution_backends.py` |
| 理解 HMX 记忆交换的导出/导入/复核 | CLI HMX | `cli_exchange.py` | `core/hmx_files.py` |
| 理解 docs/desk 归档柜终端命令 | CLI Docs | `cli_docs.py` | `cli_theme.py` |
| 理解行式提问组件（questionary 封装） | CLI Prompts | `cli_prompts.py` | `cli_chat.py` |
| 理解 CLI 主题、表格与状态徽章渲染 | CLI Theme | `cli_theme.py` | — |
| 理解 API 服务的路由分组与 SSE 端点 | API Server | `hexis_api.py` | `channels/presentation.py` |
| 理解 MCP 工具服务器的工具注册与分发 | MCP Server | `hexis_mcp_server.py` | `core/cognitive_memory_api.py` |
| 理解后台 worker 进程的入口委派 | Worker Entry | `worker.py` | `services/worker_service.py` |
| 理解本地语音 sidecar 的 loopback 启动参数 | Voice Sidecar | `voice_sidecar.py` | `core/voice_sidecar.py` |

## Subdirectories
| Dir | Domain | Depends On | Purpose |
|---|---|---|---|
| `tui/` | TUI | `core/cli_api.py` | 保留但已非默认的 Textual 初始化/聊天界面与其 widgets、模型目录。 |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `main` | `hexis_cli.py` | L:7262 |
| `build_parser` | `hexis_cli.py` | L:484 |
| `app` | `hexis_api.py` | L:107 |
| `main` | `hexis_api.py` | L:3822 |
| `main` | `hexis_init.py` | L:1529 |
| `main` | `hexis_mcp_server.py` | L:784 |
| `register_auth_subparsers` | `cli_auth.py` | L:31 |
| `dispatch_auth_command` | `cli_auth.py` | L:174 |
| `register_node_parser` | `cli_node.py` | L:17 |
| `dispatch_node_command` | `cli_node.py` | L:793 |
| `register_execution_parser` | `cli_execution.py` | L:24 |
| `dispatch_execution_command` | `cli_execution.py` | L:488 |
| `run_export` | `cli_exchange.py` | L:415 |
| `_print_rich_status` | `hexis_cli.py` | L:5438 |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `cli_auth.py` | CLI Auth | ← `core.auth.*` \| → `core/hmx_interview.py` | 为 `hexis auth` 注册并分发所有 OAuth/订阅 token provider 的登录、状态与登出。 |
| `cli_chat.py` | CLI Chat | ← `core.agent_loop`, `core.cognitive_memory_api`, `services.chat` | 行式流式聊天 REPL，带工具可见性、slash 命令与 init 后 greet。 |
| `cli_docs.py` | CLI Docs | ← `—` \| → `—` | `hexis docs`/`hexis desk` 的归档柜与 RecMem 桌面终端命令。 |
| `cli_exchange.py` | CLI HMX | ← `core.hmx_files`, `core.memory_exchange`, `core.trust_anchors` | HMX 记忆交换的命令行 I/O 与报告输出。 |
| `cli_execution.py` | CLI Execution | ← `core.execution_backends` \| → `—` | 本地、SSH、远程 Docker 执行目标的管理命令。 |
| `cli_node.py` | CLI Node | ← `core.node_daemon`, `core.node_identity`, `services.node_gateway` | 签名、仅外联的 companion node 生命周期命令。 |
| `cli_prompts.py` | CLI Prompts | ← `questionary` \| → `core/hmx_interview.py` | 键盘优先的行式提问封装（选择、文本、确认、密码）。 |
| `cli_theme.py` | CLI Theme | ← `rich` \| → `—` | 共享 CLI 主题、表格/面板/徽章与能量条等渲染辅助。 |
| `hexis_api.py` | API Server | ← `channels.presentation`, `core.agent_loop`, `services.chat` \| → 8 files (foundational); rg "apps.hexis_api" --type py -l | FastAPI 服务，暴露 SSE 聊天、OpenAI 兼容端点与集成/ingest/voice/init 等路由。 → see hexis_api.analysis.md |
| `hexis_cli.py` | CLI Entrypoint | ← `core.cli_api`, `core.agent_api`, `core.memory_exchange` \| → `—` | 顶层 `hexis` 命令：注册全部子命令、转发模块并兜底错误。 → see hexis_cli.analysis.md |
| `hexis_init.py` | Init Wizard | ← `core.agent_api`, `core.init_api`, `core.llm` | 交互/非交互的初始化向导（Express/Character/Custom → Consent）。 |
| `hexis_mcp_server.py` | MCP Server | ← `core.cognitive_memory_api` | 以 MCP 协议暴露 CognitiveMemory 工具，含注册与分发。 |
| `voice_sidecar.py` | Voice Sidecar | ← `—` \| → `core/voice_sidecar.py` | 仅 loopback 的 Piper HTTP 合成服务入口。 |
| `worker.py` | Worker Entry | ← `services.worker_service` \| → `core/host_services.py` | 后台 heartbeat/maintenance/gateway worker 的薄 CLI 包装。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `hexis_cli.py` | `cli_auth.py`, `cli_docs.py`, `cli_exchange.py`, `cli_execution.py`, `cli_node.py`, `cli_theme.py` | `cli_chat.py`, `hexis_init.py` |
| `hexis_init.py` | `cli_auth.py`, `cli_chat.py`, `cli_prompts.py`, `cli_theme.py` | `cli_chat.py` |
| `cli_chat.py` | `cli_prompts.py`, `cli_theme.py` | `hexis_init.py` |
| `cli_auth.py` | `cli_theme.py` | `hexis_cli.py`, `hexis_init.py` |
| `cli_docs.py` | `cli_theme.py` | `hexis_cli.py` |
| `cli_exchange.py` | `cli_theme.py` | `hexis_cli.py` |
| `cli_execution.py` | — | `hexis_cli.py` |
| `cli_node.py` | — | `hexis_cli.py` |
| `cli_prompts.py` | — | `cli_chat.py`, `hexis_init.py` |
| `cli_theme.py` | — | `cli_auth.py`, `cli_chat.py`, `cli_docs.py`, `cli_exchange.py`, `hexis_cli.py`, `hexis_init.py` |
| `hexis_api.py` | — | — |
| `hexis_mcp_server.py` | — | — |
| `voice_sidecar.py` | — | — |
| `worker.py` | — | — |
