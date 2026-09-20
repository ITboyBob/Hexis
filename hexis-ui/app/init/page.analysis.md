---
source: page.tsx
lines: 2665
generated_at: 2026-09-20
---

> 初始化向导页面：按 `InitStage`（llm → choose_path → express/character/custom → consent → complete）分阶段驱动 `/api/init/*` 路由，收集 LLM 配置、角色卡、人格/价值观/世界观、目标，并完成双模型 consent 请求。

## Feature Index
| Intent | Lines | Notes |
|---|---|---|
| 理解初始化阶段机与阶段文案 | L:9-L:52 | `InitStage` 联合类型、`stageLabels`、`stagePrompt` |
| 理解 LLM provider 元数据与默认配置 | L:53-L:299 | `providerMeta`、`providerOrder`、`defaultLlmConfig`，及各表单类型 |
| 理解时区种子与 `postJson` 请求封装 | L:301-L:366 | `seedTimezone`、`postJson` |
| 理解 persona 草稿解析与角色卡合并 | L:367-L:466 | `personaDraftFromPayload`、`mergePersonaIntoCharacterCard` |
| 理解 DB 阶段到 UI 阶段映射与下一步推导 | L:467-L:517 | `dbStageToUiStage`、`hasCompleteLlmSetup`、`nextStageFromInitStatus` |
| 理解页面状态清单（LLM/角色/自定义/consent） | L:518-L:633 | 全部 `useState` 声明 |
| 理解状态加载与阶段同步 | L:634-L:692 | `loadStatus` 调 `/api/init/status` 并 `setStage` |
| 理解角色目录搜索与加载 | L:693-L:736 | `loadCatalog` 调 `/api/init/character-catalog` |
| 理解模型目录按角色加载 | L:737-L:809 | `loadModels` 调 `/api/init/models` |
| 理解 OAuth 授权完成后的刷新 | L:810-L:875 | `handleOAuthAuthenticated`、`oauthRefreshKey` |
| 理解 LLM 配置保存 | L:816-L:875 | `handleLlmSave` 调 `/api/init/llm` |
| 理解 Express 快速路径提交 | L:876-L:890 | `handleExpress` |
| 理解角色卡应用（适配 + 保存） | L:891-L:1019 | `handleCharacterApply`、`handleCatalogAdapt`、`handleCatalogApply` |
| 理解 Custom 分节提交（identity/values/worldview/boundaries/goals/relationship） | L:1020-L:1110 | `handleCustomSubmit` 分别 POST 各 `/api/init/*` |
| 理解 consent 请求与「先完成缺失步骤」提示 | L:1111-L:1187 | `handleConsentRequestAll`、`handleProceedAnyway` |
| 理解角色卡导入/导出 | L:1188-L:1243 | `handleImportCard`、`handleExportAsCard` |
| 理解 LLM 阶段渲染 | L:1358-L:1544 | 双模型选择、provider 与端点表单 |
| 理解 Choose Path / Express / Character 阶段渲染 | L:1545-L:2141 | 三条路径入口与目录浏览/预览/适配 |
| 理解 Custom 阶段渲染（分节表单） | L:2142-L:2511 | identity/values/goals 分节 tabs |
| 理解 Consent 阶段渲染与 consent 交换展示 | L:2512-L:2641 | `ConsentExchangeView`、`ready_for_consent` 分支 |
| 理解 Complete 阶段与进入主页 | L:2642-L:2665 | `router.push("/")` |

## Symbols
| Symbol | Type | Line |
|---|---|---|
| `Home` | component | L:518 |
| `nextStageFromInitStatus` | function | L:495 |
| `hasCompleteLlmSetup` | function | L:488 |
| `hasCompleteLlmConfig` | function | L:475 |
| `mergePersonaIntoCharacterCard` | function | L:435 |
| `safeCharacterFilename` | function | L:425 |
| `personaDraftFromPayload` | function | L:382 |
| `postJson` | function | L:315 |
| `seedTimezone` | function | L:301 |
| `dbStageToUiStage` | function | L:467 |
| `OAuthControl` | import | L:7 |
| `ConsentExchangeView` | import | L:6 |

## Logical Sections
| Lines | Content |
|---|---|
| L:1-L:8 | imports 与 "use client" |
| L:9-L:299 | 阶段类型、provider 元数据与全部表单类型定义 |
| L:301-L:466 | 请求封装、时区、persona 解析与角色卡合并 |
| L:467-L:517 | 阶段推导纯函数 |
| L:518-L:633 | `Home` 状态声明 |
| L:634-L:1243 | 副作用与处理器（状态加载、目录、模型、各路径提交、consent、导入导出） |
| L:1244-L:1357 | 顶部进度/状态摘要渲染 |
| L:1358-L:2511 | 各阶段主内容渲染（LLM/ChoosePath/Express/Character/Custom） |
| L:2512-L:2665 | Consent 与 Complete 阶段渲染 |
