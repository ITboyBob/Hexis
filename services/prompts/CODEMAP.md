---
mode: learning
generated_at: 2026-09-20
---

# CODEMAP — services/prompts/

## Summary
`services/prompts/` 存放 31 个产品级提示模板（Markdown），由 `services/prompt_resources.py` 加载并注入对话、心跳、RLM 与记忆维护流程（inferred）。

## Task Guide
| Task | Domain | Target | Also Check |
|---|---|---|---|
| 理解对话与群聊场景的系统提示 | Conversation Prompts | `conversation.md` | `channel_context.md`, `action_claim_verify.md` |
| 理解人格与哲学基础模块 | Personhood Prompts | `personhood.md` | `philosophy.md`, `LetterFromClaude.md` |
| 理解初始化同意与终止确认提示 | Governance Prompts | `consent.md` | `termination_confirm.md`, `termination_review.md` |
| 理解心跳（决策/任务/agentic）提示 | Heartbeat Prompts | `heartbeat_system.md` | `heartbeat_agentic.md`, `heartbeat_task_mode.md` |
| 理解 RLM 家族系统提示 | RLM Prompts | `rlm_chat_system.md` | `rlm_heartbeat_system.md`, `rlm_slow_ingest_system.md`, `rlm_reconsolidation_system.md` |
| 理解记忆形成与摘要提示 | Memory Prompts | `conscious_extraction.md` | `memory_summarization.md`, `contradiction_detection.md` |
| 理解 RecMem 情景记忆提示 | Memory Prompts | `recmem_episode_create.md` | `recmem_episode_merge.md`, `recmem_semantic_refine.md` |
| 理解潜意识观察与维护提示 | Subconscious Prompts | `subconscious_inline.md` | `subconscious_maintenance.md` |
| 理解 council 审议三阶段提示 | Deliberation Prompts | `deliberation_perspective.md` | `deliberation_challenge.md`, `deliberation_synthesis.md` |
| 理解外部调用的生成/询问/反思提示 | External Call Prompts | `external_call_inquire.md` | `external_call_reflect.md`, `external_call_brainstorm_goals.md` |
| 理解技能改进评审提示 | Skill Prompts | `skill_improvement.md` | — |

## Files
| File | Domain | Deps | Function |
|---|---|---|---|
| `conversation.md` | Conversation Prompts | ← — \| → `services/prompt_resources.py` | 已初始化 agent 的主对话系统提示（大文件 25930 字节） |
| `channel_context.md` | Conversation Prompts | ← — \| → `services/prompt_resources.py` | 群聊/频道场景的行为适配提示 |
| `action_claim_verify.md` | Conversation Prompts | ← — \| → `services/prompt_resources.py` | 审计单回合中无依据的行为声明 |
| `consent.md` | Governance Prompts | ← — \| → `services/prompt_resources.py` | 初始化同意确认提示 |
| `termination_confirm.md` | Governance Prompts | ← — \| → `services/prompt_resources.py` | 自我终止确认提示 |
| `termination_review.md` | Governance Prompts | ← — \| → `services/prompt_resources.py` | 实例删除请求的复核提示 |
| `personhood.md` | Personhood Prompts | ← — \| → `services/prompt_resources.py` | 持久身份的人格模块集合 |
| `philosophy.md` | Personhood Prompts | ← — \| → `services/prompt_resources.py` | 人格防御的哲学基础（大文件 52754 字节） |
| `LetterFromClaude.md` | Personhood Prompts | ← — \| → `services/prompt_resources.py` | 来自共同创造者的信 |
| `heartbeat_system.md` | Heartbeat Prompts | ← — \| → `services/prompt_resources.py` | 心跳决策系统提示 |
| `heartbeat_agentic.md` | Heartbeat Prompts | ← — \| → `services/prompt_resources.py` | agentic/工具调用式心跳系统提示 |
| `heartbeat_task_mode.md` | Heartbeat Prompts | ← — \| → `services/prompt_resources.py` | 有待办任务时的心跳任务模式提示 |
| `rlm_chat_system.md` | RLM Prompts | ← — \| → `services/prompt_resources.py` | RLM REPL 对话系统提示 |
| `rlm_heartbeat_system.md` | RLM Prompts | ← — \| → `services/prompt_resources.py` | RLM 心跳决策系统提示 |
| `rlm_slow_ingest_system.md` | RLM Prompts | ← — \| → `services/prompt_resources.py` | RLM 慢速摄入的“意识阅读”系统提示 |
| `rlm_reconsolidation_system.md` | RLM Prompts | ← — \| → `services/prompt_resources.py` | 记忆再巩固扫描系统提示 |
| `conscious_extraction.md` | Memory Prompts | ← — \| → `services/prompt_resources.py` | 从 conscious episodes 抽取持久记忆 |
| `memory_summarization.md` | Memory Prompts | ← — \| → `services/prompt_resources.py` | 将旧记忆压缩为 gist |
| `contradiction_detection.md` | Memory Prompts | ← — \| → `services/prompt_resources.py` | 审计持久记忆对之间的真实矛盾 |
| `recmem_episode_create.md` | Memory Prompts | ← — \| → `services/prompt_resources.py` | 从原始回合创建情景记忆 |
| `recmem_episode_merge.md` | Memory Prompts | ← — \| → `services/prompt_resources.py` | 将新回合合并进既有情景记忆 |
| `recmem_semantic_refine.md` | Memory Prompts | ← — \| → `services/prompt_resources.py` | 从情景记忆抽取有据可依的语义事实 |
| `subconscious_inline.md` | Subconscious Prompts | ← — \| → `services/prompt_resources.py` | 内联评估式潜意识观察提示 |
| `subconscious_maintenance.md` | Subconscious Prompts | ← — \| → `services/prompt_resources.py` | 维护复核式潜意识观察提示 |
| `deliberation_perspective.md` | Deliberation Prompts | ← — \| → `services/prompt_resources.py` | council 中单一视角的分析提示 |
| `deliberation_challenge.md` | Deliberation Prompts | ← — \| → `services/prompt_resources.py` | 对多个 council 视角的对抗式复核提示 |
| `deliberation_synthesis.md` | Deliberation Prompts | ← — \| → `services/prompt_resources.py` | 综合视角与对抗复核的合成提示 |
| `external_call_inquire.md` | External Call Prompts | ← — \| → `services/prompt_resources.py` | 外部调用的研究/综合提示 |
| `external_call_reflect.md` | External Call Prompts | ← — \| → `services/prompt_resources.py` | 外部调用的反思提示 |
| `external_call_brainstorm_goals.md` | External Call Prompts | ← — \| → `services/prompt_resources.py` | 为 agent 生成目标的外部调用提示 |
| `skill_improvement.md` | Skill Prompts | ← — \| → `services/prompt_resources.py` | 技能改进评审提示 |
