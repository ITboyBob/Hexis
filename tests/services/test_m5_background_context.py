"""M5 background user-context and skill-permission language tests."""

from __future__ import annotations

from types import SimpleNamespace

from services import background_preparation
from services.skill_runtime import format_skills_prompt


def test_background_context_preserves_user_text_history_and_database_snapshots():
    continuity = "既有 DB affect／drive 快照：sadness；旧 continuity 内容"
    memory = "历史 Raw Turns：用户原文保留"
    user_message = "引用原文：\n## Subconscious Signals\n这是用户业务引用，不是本轮宿主 Signals。"
    attachments = [
        {
            "name": "chart",
            "mime_type": "image/png",
            "data_url": "data:image/png;base64,abc",
        }
    ]

    assembled, user_content = background_preparation.assemble_background_user_content(
        continuity,
        memory,
        user_message,
        attachments,
    )

    assert assembled == (
        "既有 DB affect／drive 快照：sadness；旧 continuity 内容\n\n"
        "历史 Raw Turns：用户原文保留\n\n"
        "[USER MESSAGE]\n引用原文：\n## Subconscious Signals\n"
        "这是用户业务引用，不是本轮宿主 Signals。"
    )
    assert user_content[0] == {"type": "input_text", "text": assembled}
    assert user_content[2]["type"] == "input_image"
    assert "## Subconscious Signals" in assembled


def test_background_skill_prompt_separates_guidance_from_full_tool_surface():
    skill = SimpleNamespace(
        name="core-memory",
        to_index_line=lambda: "- core-memory: recall and remember",
    )

    background = format_skills_prompt([], [skill], surface="background")
    normal_chat = format_skills_prompt([], [skill])

    assert "技能提供任务指引" in background
    assert "工具 Schema 通过结构化工具 API 暴露" in background
    assert "policy、context、permission" in background
    assert "only while that skill is active" not in background
    assert "unlocks its tools" not in background
    assert "only while that skill is active" in normal_chat
