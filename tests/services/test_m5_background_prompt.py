"""M5 background prompts omit current-turn appraisal assumptions."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_background_prompt_uses_separate_conversation_and_personhood():
    from services import agent, prompt_resources

    prompt_resources.load_personhood_library.cache_clear()
    background = await agent.build_system_prompt(
        "chat",
        None,
        surface="background",
        active_skills=[],
        available_skills=[],
        allowed_tool_names=None,
    )
    normal_chat = await agent.build_system_prompt(
        "chat",
        None,
        surface="chat",
        active_skills=[],
        available_skills=[],
        allowed_tool_names=None,
    )
    heartbeat = await agent.build_system_prompt(
        "heartbeat",
        None,
        surface="background",
        active_skills=[],
        available_skills=[],
        allowed_tool_names=None,
    )

    assert "## Subconscious Signals" not in background
    assert "Before you respond, your subconscious has already reacted" not in background
    assert "evidence-grounded subconscious appraisal" not in background
    assert "本轮潜意识评估" not in background
    assert "既有情绪与 drive 快照" in background
    assert "持续身份与会话连续性" in background
    assert "## Subconscious Signals" in normal_chat
    assert "Strong affect should visibly bend the reply" in normal_chat
    assert prompt_resources.load_heartbeat_agentic_prompt().strip() in heartbeat
    assert "持续身份与会话连续性" not in heartbeat


def test_background_prompt_loader_fails_when_its_file_is_missing(monkeypatch, tmp_path):
    from services import prompt_resources

    monkeypatch.setattr(prompt_resources, "BACKGROUND_CONVERSATION_PROMPT_PATH", tmp_path / "missing.md")
    with pytest.raises(FileNotFoundError, match="background conversation prompt"):
        prompt_resources.load_background_conversation_prompt()
