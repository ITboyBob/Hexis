---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — tests/cli/

## Summary
`tests/cli/` 是 22 个针对 `apps/hexis_cli.py` 与 CLI 安装/启动流程的测试模块，验证终端子命令、初始化、compose/镜像策略、语音、隧道与自更新（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解 `hexis up` 的构建/镜像选择策略 | 启动/编排 | `test_up_build_policy.py` | `test_compose_defaults.py`, `test_deterministic_images.py` |
| 理解非交互式与 OpenAI 兼容初始化 | 初始化 | `test_init_noninteractive.py` | `test_init_openai_compatible.py`, `test_init_stack_startup.py` |
| 理解 CLI 认证与聊天历史 | CLI 交互 | `test_cli_auth.py` | `test_cli_chat_history.py`, `test_cli.py` |
| 理解文档桌面与转录 CLI | CLI 交互 | `test_docs_desk_cli.py` | `test_transcript_cli.py` |
| 理解 HMX、执行与同伴节点 CLI | CLI 子命令 | `test_hmx_cli.py` | `test_execution_cli.py`, `test_companion_node_cli.py` |
| 理解语音与隧道 CLI | CLI 子命令 | `test_voice_cli.py` | `test_tunnel_cli.py` |
| 理解安装/卸载与自更新 | 生命周期 | `test_uninstall.py` | `test_self_update.py` |
| 理解嵌入 sidecar 与主机服务诊断 | 基础设施 | `test_embedding_sidecar.py` | `test_host_services_cli.py` |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `temp_test_db` fixture | `../conftest.py` | L:41 |

## Files
> 22 个模块，按子系统分组。

### 启动/编排
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_up_build_policy.py` | 启动/编排 | `apps.hexis_cli` | `hexis up` 构建策略。 |
| `test_compose_defaults.py` | 启动/编排 | `apps.hexis_cli` | compose 默认值。 |
| `test_deterministic_images.py` | 启动/编排 | `apps.hexis_cli` | 确定性镜像标签。 |
| `test_init_stack_startup.py` | 启动/编排 | `apps.hexis_cli` | 初始化栈启动。 |

### 初始化
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_init_noninteractive.py` | 初始化 | `apps.hexis_cli` | 非交互式初始化。 |
| `test_init_openai_compatible.py` | 初始化 | `apps.hexis_cli` | OpenAI 兼容初始化。 |

### CLI 交互
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_cli.py` | CLI 交互 | `apps.hexis_cli` | CLI 基础。 |
| `test_cli_auth.py` | CLI 交互 | `apps.hexis_cli` | CLI 认证。 |
| `test_cli_chat_history.py` | CLI 交互 | `apps.hexis_cli` | 聊天历史。 |
| `test_docs_desk_cli.py` | CLI 交互 | `apps.hexis_cli` | 文档桌面。 |
| `test_transcript_cli.py` | CLI 交互 | `apps.hexis_cli` | 转录。 |
| `test_ui_launcher.py` | CLI 交互 | `apps.hexis_cli` | UI 启动器。 |

### CLI 子命令
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_hmx_cli.py` | CLI 子命令 | `apps.cli_exchange`, `core.digest` | HMX 命令。 |
| `test_execution_cli.py` | CLI 子命令 | `apps.hexis_cli` | 执行后端命令。 |
| `test_companion_node_cli.py` | CLI 子命令 | `apps.cli_node`, `apps.hexis_cli` | 同伴节点命令。 |
| `test_voice_cli.py` | CLI 子命令 | `apps.hexis_cli`, `core.voice_sidecar` | 语音命令。 |
| `test_tunnel_cli.py` | CLI 子命令 | `apps.hexis_cli`, `core.tunnel` | 隧道命令。 |
| `test_ingest_forwarding.py` | CLI 子命令 | `apps.hexis_cli` | 摄取转发。 |

### 生命周期
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_uninstall.py` | 生命周期 | `apps.hexis_cli` | 卸载。 |
| `test_self_update.py` | 生命周期 | `apps.hexis_cli` | 自更新。 |

### 基础设施
| File | Domain | Deps | Function |
|---|---|---|---|
| `test_embedding_sidecar.py` | 基础设施 | `core.cli_api` | 嵌入 sidecar 诊断。 |
| `test_host_services_cli.py` | 基础设施 | `apps.hexis_cli` | 主机服务命令。 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| 全部测试模块 | `../conftest.py`（fixture） | — |
