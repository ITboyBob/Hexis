---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — core/tools/

## Summary
`core/tools/` 是 Hexis 的可插拔工具框架：`base.py`/`registry.py`/`policy.py`/`hooks.py` 定义抽象与生命周期，其余模块各自实现一组面向模型的 `ToolHandler`（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解工具基类、上下文与执行结果契约 | Tool Framework | `base.py` | `registry.py`, `policy.py` |
| 理解工具如何注册、分组与按上下文启用 | Tool Framework | `registry.py` | `config.py`, `hooks.py` |
| 理解工具执行前后的钩子与审计轨迹 | Tool Framework | `hooks.py` | `policy.py` |
| 理解工具调用的策略检查与拒绝 | Tool Framework | `policy.py` | `base.py` |
| 理解记忆召回/记住/文档检索等工具 | Memory Tools | `memory.py` | `desk.py`, `documents.py` |
| 理解 HMX 导出导入的 agent 工具面 | Memory Tools | `memory_exchange.py` | `protected_replacement.py` |
| 理解内容摄入（fast/slow/hybrid/git）工具 | Ingest Tools | `ingest.py` | `web.py` |
| 理解技能发现与激活工具 | Skill Tools | `skills.py` | `self_extension.py` |
| 理解个人数据连接器工具与安装向导 | Connector Tools | `integrations.py` | `gmail_actions.py`, `twitter_x_actions.py` |
| 理解邮件与消息渠道工具 | Messaging Tools | `email.py` | `messaging.py` |
| 理解日历/联系人/生活集成工具 | Life Tools | `calendar.py` | `contacts.py`, `life_integrations.py` |
| 理解 shell、代码执行与文件系统工具 | Execution Tools | `shell.py` | `code_execution.py`, `filesystem.py` |
| 理解 REPL 沙箱到工具的同步桥 | Execution Tools | `repl_bridge.py` | `code_execution.py` |
| 理解 MCP 服务器接入与惰性运行时 | MCP Tools | `mcp_runtime.py` | `mcp.py`, `config.py` |
| 理解自我检视/自修复/自我扩展工具 | Self Tools | `self_inspection.py` | `self_repair.py`, `self_extension.py` |
| 理解工作流编排与多步工具链 | Workflow Tools | `workflow.py` | `council.py` |
| 理解多智能体 council 与审议工具 | Council Tools | `council.py` | `workflow.py` |
| 理解备份/保留/配置导出工具 | Backup Tools | `backup.py` | `config.py` |
| 理解伴随节点与语音/图像等能力工具 | Node & Media Tools | `nodes.py` | `speech.py`, `image_gen.py` |
| 理解计划任务、目标、待办与责任工具 | Planning Tools | `cron.py` | `goals.py`, `backlog.py`, `responsibilities.py` |
| 理解网络搜索/抓取的 provider 分派 | Web Tools | `web_search_providers.py` | `web.py`, `brave_search.py`, `firecrawl.py` |
| 理解 Web 搜索/抓取/摘要工具 | Web Tools | `web.py` | `browser.py` |
| 理解子代理会话与异步提问工具 | Session Tools | `sessions.py` | `questions.py` |
| 理解操作者策略与审批相关工具 | Governance Tools | `operator_policies.py` | `protected_replacement.py` |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `ToolContext` | `base.py` | L:39 |
| `ToolExecutionContext` | `base.py` | L:286 |
| `ToolResult` | `base.py` | L:222 |
| `ToolSpec` | `base.py` | L:128 |
| `ToolHandler` | `base.py` | L:377 |
| `ToolCategory` | `base.py` | L:23 |
| `ToolRegistry` | `registry.py` | L:107 |
| `create_default_registry` | `registry.py` | L:857 |
| `create_full_registry` | `registry.py` | L:1017 |
| `ToolsConfig` | `config.py` | L:82 |
| `HookRegistry` | `hooks.py` | L:132 |
| `AuditTrailHook` | `hooks.py` | L:264 |
| `ToolPolicy` | `policy.py` | L:52 |
| `CodeExecutionHandler` | `code_execution.py` | L:61 |
| `ReplToolBridge` | `repl_bridge.py` | L:72 |
| `create_memory_tools` | `memory.py` | L:2446 |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `__init__.py` | Tool Framework | ← — \| → — | 汇总并重导出工具框架公共符号与各 create_* 工厂 |
| `api_keys.py` | Tool Framework | ← — \| → — | 从 resolver、工具配置与环境解析工具 API key |
| `base.py` | Tool Framework | ← — \| → 100+ files (foundational); rg "ToolExecutionContext" --type py -l | 工具系统的基类、上下文、结果与错误类型契约 |
| `config.py` | Tool Framework | ← — \| → 27+ files | 工具系统配置 dataclass 与加载/保存 |
| `hooks.py` | Tool Framework | ← — \| → 23+ files | 工具生命周期钩子与审计轨迹注册表 |
| `policy.py` | Tool Framework | ← — \| → — | 工具执行策略检查与结果 |
| `registry.py` | Tool Framework | ← `base.py`, `config.py`, `hooks.py`, `policy.py` \| → 31+ files | 工具注册表、构建器与默认/完整注册表工厂（大文件 1071 行） |
| `memory.py` | Memory Tools | ← — \| → 7+ files | 记忆召回、检索、文档与概念探索等工具（大文件 2478 行） |
| `memory_exchange.py` | Memory Tools | ← `core/hmx_files.py`, `core/memory_exchange.py` \| → — | 受技能门控的 HMX 导出/导入/评审工具 |
| `protected_replacement.py` | Memory Tools | ← `core/memory_exchange.py`, `core/protected_replacement.py` \| → — | HMX 受保护区块替换协议的工具面 |
| `desk.py` | Memory Tools | ← — \| → — | RecMem 桌面（中期工作记忆）工具 |
| `documents.py` | Memory Tools | ← — \| → — | 已摄入文档的审批工具 |
| `journal.py` | Memory Tools | ← — \| → — | 永久性日志记录工具 |
| `ingest.py` | Ingest Tools | ← — \| → 21+ files | 快速/慢速/混合/git 内容摄入工具 |
| `web.py` | Web Tools | ← — \| → — | Web 搜索、抓取与摘要工具 |
| `web_search_providers.py` | Web Tools | ← `core/integration_reliability.py` \| → — | 面向 `web_search` 的 provider 注册与结果归一化 |
| `brave_search.py` | Web Tools | ← `core/integration_reliability.py`, `api_keys.py` \| → — | Brave Search API 搜索工具 |
| `firecrawl.py` | Web Tools | ← `core/integration_reliability.py`, `api_keys.py` \| → — | Firecrawl 网页抓取工具 |
| `browser.py` | Web Tools | ← — \| → — | 基于 Playwright 的浏览器自动化工具 |
| `filesystem.py` | Execution Tools | ← — \| → — | 读写、glob、grep 等文件系统工具 |
| `shell.py` | Execution Tools | ← `core/execution_backends.py` \| → — | 带沙箱与安全约束的 shell 命令执行工具 |
| `code_execution.py` | Execution Tools | ← `core/execution_backends.py` \| → 20+ files | 将 HexisLocalREPL 暴露为可调用工具 |
| `repl_bridge.py` | Execution Tools | ← — \| → 4+ files | REPL 沙箱内同步调用工具的桥接 |
| `skills.py` | Skill Tools | ← `services/skill_runtime.py` \| → 14+ files | 技能发现、激活与创作/评审工具（大文件 1070 行） |
| `self_extension.py` | Self Tools | ← — \| → — | 自撰能力的基板变更可见性 |
| `self_inspection.py` | Self Tools | ← — \| → — | 检视源码与实时数据库 schema 的只读工具 |
| `self_repair.py` | Self Tools | ← — \| → — | 检视与管理基板缺陷的自修复工具 |
| `dynamic.py` | Self Tools | ← `self_extension.py` \| → — | 允许 agent 用 Python 定义新 ToolHandler |
| `workflow.py` | Workflow Tools | ← — \| → — | 多步工具链编排与依赖解析 |
| `council.py` | Council Tools | ← — \| → — | 多视角 council 审议与 persona 工具 |
| `integrations.py` | Connector Tools | ← — \| → — | 个人数据连接器安装与配置工具（大文件 2422 行） |
| `life_integrations.py` | Life Tools | ← `core/integration_reliability.py`, `services/life_integrations.py` \| → — | 日常生活集成工具 |
| `gmail_actions.py` | Connector Tools | ← — \| → — | Gmail provider 动作工具 |
| `twitter_x_actions.py` | Connector Tools | ← — \| → — | Twitter/X provider 动作工具 |
| `calendar.py` | Life Tools | ← — \| → — | 日历查看/创建/更新/删除与会议准备工具 |
| `contacts.py` | Life Tools | ← — \| → — | 个人 CRM 联系人工具 |
| `email.py` | Messaging Tools | ← `core/integration_reliability.py` \| → — | 邮件发送/读取工具（大文件 1198 行） |
| `messaging.py` | Messaging Tools | ← `core/integration_reliability.py` \| → — | Discord/Slack/Telegram 等消息工具 |
| `nodes.py` | Node & Media Tools | ← — \| → — | 经审批的伴随节点访问工具 |
| `speech.py` | Node & Media Tools | ← — \| → — | 本地语音合成输出工具 |
| `image_gen.py` | Node & Media Tools | ← — \| → — | DALL-E 3 图像生成工具 |
| `local_audio_analysis.py` | Node & Media Tools | ← — \| → — | 设备本地说话人分离工具（审批门控） |
| `humanizer.py` | Node & Media Tools | ← — \| → — | 检测并去除 AI 写作痕迹 |
| `cron.py` | Planning Tools | ← — \| → — | 计划任务的创建/列出/更新/取消 |
| `goals.py` | Planning Tools | ← — \| → — | 目标的创建/更新/完成与管理 |
| `backlog.py` | Planning Tools | ← — \| → — | 任务/待办 CRUD |
| `responsibilities.py` | Planning Tools | ← — \| → — | 环境责任管理工具 |
| `sessions.py` | Session Tools | ← — \| → — | 后台子代理会话的生成与管理 |
| `questions.py` | Session Tools | ← — \| → — | 暂停实时回合或异步提问的澄清工具 |
| `resources.py` | Session Tools | ← — \| → — | 向操作者结构化请求资源 |
| `usage_query.py` | Governance Tools | ← — \| → — | 查询 api_usage 用量与成本 |
| `operator_policies.py` | Governance Tools | ← — \| → — | 操作者检视/撤销常设策略的工具 |
| `backup.py` | Backup Tools | ← — \| → — | 数据库备份、保留策略与配置导出/导入工具 |
| `mcp.py` | MCP Tools | ← — \| → — | 连接 MCP 服务器的客户端与工具处理器 |
| `mcp_runtime.py` | MCP Tools | ← `mcp.py`, `config.py` \| → — | 进程级惰性 MCP 传输运行时 |
| `sync_adapter.py` | Tool Framework | ← — \| → — | 异步工具操作的同步包装 |
| `integration_http.py` | Tool Framework | ← `core/integration_reliability.py` \| → — | 面向工具的外部 HTTP 可靠性辅助 |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `base.py` | — | 全部工具模块（基类/上下文） |
| `config.py` | `base.py` | `registry.py`, `mcp_runtime.py` |
| `hooks.py` | — | `registry.py` |
| `policy.py` | — | `registry.py` |
| `registry.py` | `base.py`, `config.py`, `hooks.py`, `policy.py` | `__init__.py` |
| `api_keys.py` | `base.py` | `brave_search.py`, `firecrawl.py` |
| `self_extension.py` | — | `dynamic.py` |
| `mcp.py` | — | `mcp_runtime.py` |
| `__init__.py` | 全部工具模块 | — |
