---
source: hexis_cli.py
lines: 8531
generated_at: 2026-09-20
---

> Hexis 顶层 CLI 入口：用 argparse 注册全部 `hexis ...` 子命令，并把 `chat`/`init`/`ingest`/`mcp`/`worker` 转发给各自模块，其余命令在 `_dispatch` 中按 `func` 字符串路由到本文件或 `cli_auth`/`cli_node`/`cli_execution`/`cli_exchange`/`cli_docs` 的处理器。

## Feature Index
| Intent | Lines | Notes |
|---|---|---|
| 理解 CLI 顶层错误兜底与「数据库不可达」提示 | L:7262-L:7290 | `main` 捕获 KeyboardInterrupt/连接类异常，转成单行可操作提示 |
| 理解命令预解析与模块转发（chat/init/ingest/mcp/worker） | L:7292-L:7375 | `_dispatch` 在 argparse 之前扫描 `--instance`/`--version`，命中 `forward_map` 则 `_run_module` 转发 |
| 理解 argparse 子命令树的全量注册 | L:484-L:1922 | `build_parser` 单函数内联注册所有子命令与 `set_defaults(func=...)` |
| 理解全局分组帮助与 DB 通用参数 | L:364-L:483 | `_make_db_flags` 与 `_print_grouped_help` |
| 理解 instance 多实例管理（create/list/use/current/delete/clone/import） | L:2390-L:2577 | 处理器；分发见 L:7409-L:7425 |
| 理解 docs/desk 归档柜命令的分发 | L:7428-L:7474 | 实际实现在 `apps/cli_docs.py`，此处仅取 DSN 后调用 |
| 理解 consents 与 requests 决策命令 | L:2594-L:2857 | `_requests_list`/`_requests_decide`/`_consents_*`；分发见 L:7477-L:7508 |
| 理解 characters 角色卡管理（含导出需 DB） | L:2858-L:3202 | `_characters_*`；分发见 L:7511-L:7523 |
| 理解 chat_sessions 列表/查看/导出/标题/fork/clone | L:4058-L:4346 | 分发见 L:7525-L:7582 |
| 理解 HMX 记忆交换（export/import/review）的分发 | L:7584-L:7592 | 委派给 `apps/cli_exchange.py` 的 `run_*` |
| 理解 host service / tunnel / voice 命令族 | L:3203-L:4005 | `_handle_host_service_command`/`_handle_tunnel_command`/`_handle_voice_command`；分发见 L:7605-L:7617 |
| 理解 Docker 生命周期（up/dev/down/reset/upgrade/logs/ps/start/stop） | L:7642-L:8089 | `_dispatch` 内联，依赖 compose 探测与宿主 worker 判定 |
| 理解 UI 启动器（本地/容器两种路径） | L:6647-L:6983 | `_handle_ui` 与 `_handle_ui_container`；分发见 L:8114-L:8138 |
| 理解本地 embedding sidecar 的安装/启动/停用/清理 | L:5922-L:6646 | 所有权文件、PID 文件、端口占用与进程校验 |
| 理解 auth / node / execution 命令的委派 | L:8396-L:8418 | `func.startswith("auth"/"node_"/"execution_")` 调用对应模块的 dispatch |
| 理解 tools / channels / goals / schedule 命令 | L:8419-L:8531 | `_tools_*`、`_channels_*`、`_goals_*`、`_schedule_*` 处理器 |
| 理解 uninstall（含 purge / cli-only）流程 | L:6984-L:7261 | 卸载程序探测、数据目录清理与确认；分发见 L:7594-L:7603 |
| 理解 self-update 与 `hexis upgrade` | L:27-L:147, L:7977-L:8080 | PyPI 版本比较、安装方式识别与升级提示 |

## Symbols
| Symbol | Type | Line |
|---|---|---|
| `main` | function | L:7262 |
| `_dispatch` | function | L:7292 |
| `build_parser` | function | L:484 |
| `_print_grouped_help` | function | L:466 |
| `_make_db_flags` | function | L:364 |
| `ensure_docker` | function | L:173 |
| `ensure_compose` | function | L:197 |
| `run_compose` | function | L:272 |
| `_run_compose_capture` | function | L:292 |
| `_find_compose_file` | function | L:148 |
| `_stack_root_from_compose` | function | L:167 |
| `resolve_env_file` | function | L:217 |
| `_host_managed_compose_workers` | function | L:3292 |
| `_start_local_embedding_service` | function | L:6469 |
| `_print_rich_status` | function | L:5438 |
| `_handle_ui` | function | L:6647 |
| `_handle_ui_container` | function | L:6885 |
| `_uninstall` | function | L:7113 |
| `_get_dsn` | function | L:4006 |
| `_run_module` | function | L:3203 |

## Logical Sections
| Lines | Content |
|---|---|
| L:1-L:147 | 模块导入、版本探测与自更新逻辑 |
| L:148-L:483 | Docker/Compose 探测、环境文件解析、通用 DB 参数与分组帮助 |
| L:484-L:1922 | `build_parser`：全部子命令注册（最大单段） |
| L:1923-L:3202 | DB 型处理器：tools、instance、requests、consents、characters |
| L:3203-L:5093 | host service / tunnel / voice、chat sessions、retention、skills、migrate、backup |
| L:5094-L:5921 | channels setup、rich status、recall、goals、schedule |
| L:5922-L:6983 | 本地 embedding sidecar 管理与 UI 启动器 |
| L:6984-L:7261 | uninstall 流程 |
| L:7262-L:8531 | `main`/`_dispatch`：命令路由与全部 `func` 分支 |
