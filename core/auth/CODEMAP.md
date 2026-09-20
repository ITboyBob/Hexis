---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — core/auth/

## Summary
`core/auth/` 汇集各 provider 的 OAuth/令牌获取流程与文件系统凭据存储，向上为 CLI、UI 初始化流程和连接器提供统一认证能力（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解凭据如何持久化到 ~/.hexis/auth 并加锁 | Credential Store | `store.py` | `utils.py` |
| 理解 PKCE、state 与刷新判断等通用辅助 | Auth Utilities | `utils.py` | `store.py` |
| 理解浏览器初始化流程如何编排 OAuth 会话 | Auth Orchestration | `ui_flow.py` | `callback_server.py` |
| 理解本地 OAuth 回调服务器如何工作 | Auth Orchestration | `callback_server.py` | `ui_flow.py` |
| 理解 ChatGPT 订阅（Codex）OAuth 全流程 | Provider Auth | `openai_codex.py` | `store.py`, `utils.py` |
| 理解 Gmail 连接器 OAuth 与凭据刷新 | Connector Auth | `google_gmail.py` | `store.py` |
| 理解 Twitter/X 连接器 OAuth 与凭据刷新 | Connector Auth | `twitter_x.py` | `store.py` |
| 理解 Gemini CLI 与 Antigravity 的 Google OAuth | Provider Auth | `google_gemini_cli.py` | `google_antigravity.py` |
| 理解 GitHub Copilot 设备码流程 | Provider Auth | `github_copilot.py` | `utils.py` |
| 理解 GitHub PAT 的校验与凭据形态 | Provider Auth | `github_pat.py` | `store.py` |
| 理解 Chutes/Qwen/MiniMax 门户登录 | Provider Auth | `chutes.py` | `qwen_portal.py`, `minimax_portal.py` |
| 理解 Anthropic setup-token 认证 | Provider Auth | `anthropic_setup_token.py` | `store.py` |
| 理解 Spotify 授权码 + PKCE 设置 | Connector Auth | `spotify.py` | `store.py` |

## Key Exports
| Symbol | Source | Line |
|---|---|---|
| `load_auth` | `store.py` | L:34 |
| `save_auth` | `store.py` | L:44 |
| `delete_auth` | `store.py` | L:66 |
| `auth_lock` | `store.py` | L:76 |
| `generate_pkce` | `utils.py` | L:24 |
| `create_state` | `utils.py` | L:34 |
| `needs_refresh` | `utils.py` | L:49 |
| `AuthFlowCoordinator` | `ui_flow.py` | L:71 |
| `AuthFlowError` | `ui_flow.py` | L:45 |
| `run_callback_server` | `callback_server.py` | L:14 |
| `GMAIL_DEFAULT_CREDENTIAL_REF` | `google_gmail.py` | L:23 |
| `start_gmail_oauth` | `google_gmail.py` | L:438 |
| `TWITTER_X_DEFAULT_CREDENTIAL_REF` | `twitter_x.py` | L:23 |
| `start_twitter_x_oauth` | `twitter_x.py` | L:262 |
| `load_openai_codex_credentials` | `openai_codex.py` | L:312 |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `__init__.py` | Auth Utilities | ← `utils.py` | 重导出共享 auth 辅助函数 |
| `utils.py` | Auth Utilities | ← — \| → 18 files (foundational); rg "generate_pkce" --type py -l | provider 无关的 PKCE、state、刷新检查辅助 |
| `store.py` | Credential Store | ← — \| → 22 files (foundational); rg "save_auth" --type py -l | 以 JSON 文件形式存储 provider 凭据（~/.hexis/auth） |
| `callback_server.py` | Auth Orchestration | ← — \| → 5 files | OAuth PKCE 流程可复用的本地 HTTP 回调服务器 |
| `ui_flow.py` | Auth Orchestration | ← `store.py`(间接), `callback_server.py`, `__init__.py` | 浏览器初始化流程使用的短时 OAuth 会话协调器（大文件 635 行） |
| `openai_codex.py` | Provider Auth | ← `utils.py`, `core/integration_reliability.py` \| → 10 files | OpenAI Codex（ChatGPT 订阅）OAuth 流程 |
| `anthropic_setup_token.py` | Provider Auth | ← — \| → 2 files | Anthropic setup-token（Claude Code CLI 订阅）认证 |
| `chutes.py` | Provider Auth | ← `utils.py`, `core/integration_reliability.py` \| → 4 files | Chutes OAuth（PKCE）认证 |
| `github_copilot.py` | Provider Auth | ← `utils.py`, `core/integration_reliability.py` \| → 5 files | GitHub Copilot 设备码 OAuth 认证 |
| `github_pat.py` | Provider Auth | ← — \| → 4 files | GitHub 个人访问令牌认证 |
| `google_gemini_cli.py` | Provider Auth | ← `utils.py`, `core/integration_reliability.py` \| → 5 files | Google Gemini CLI OAuth（Cloud Code Assist） |
| `google_antigravity.py` | Provider Auth | ← `utils.py`, `core/integration_reliability.py` \| → 5 files | Google Antigravity OAuth（Cloud Code Assist 沙箱） |
| `google_gmail.py` | Connector Auth | ← `store.py`, `utils.py`, `core/integration_reliability.py` \| → 12 files | Gmail 一等连接器的 OAuth 设置辅助（大文件 760 行） |
| `twitter_x.py` | Connector Auth | ← `store.py`, `utils.py`, `core/integration_reliability.py` \| → 5 files | Twitter/X 一等连接器的 OAuth 设置辅助 |
| `minimax_portal.py` | Provider Auth | ← `utils.py`, `core/integration_reliability.py` \| → 4 files | MiniMax Portal OAuth（user-code + PKCE） |
| `qwen_portal.py` | Provider Auth | ← `utils.py`, `core/integration_reliability.py` \| → 5 files | Qwen Portal OAuth（设备码 + PKCE） |
| `spotify.py` | Connector Auth | ← `store.py`, `utils.py`, `core/integration_reliability.py` \| → 5 files | Spotify 授权码 + PKCE 设置与令牌刷新 |
| `gmail-credentials.json` | Connector Auth | ← — \| → — | 打包的 Gmail OAuth 已安装应用客户端（勿改，见 AGENTS.md） |

## File Dependencies
| File | Imports (in-dir) | Exposed To (in-dir) |
|---|---|---|
| `utils.py` | — | `openai_codex.py`, `chutes.py`, `github_copilot.py`, `google_gemini_cli.py`, `google_antigravity.py`, `minimax_portal.py`, `qwen_portal.py`, `google_gmail.py`, `twitter_x.py`, `spotify.py`, `__init__.py` |
| `store.py` | — | `google_gmail.py`, `twitter_x.py`, `spotify.py` |
| `callback_server.py` | — | `ui_flow.py` |
| `__init__.py` | `utils.py` | `ui_flow.py` |
| `ui_flow.py` | `__init__.py`, `callback_server.py` | — |
