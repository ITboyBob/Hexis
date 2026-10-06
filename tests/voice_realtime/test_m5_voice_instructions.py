from __future__ import annotations

from uuid import uuid4

import pytest

from services.voice_recall import VOICE_RECALL_TOOL
from services.voice_realtime_session import VoiceRealtimeSession


class _Client:
    def __init__(self):
        self.tools = None
        self.instructions = None

    async def connect(self, instructions, *, tools=None):
        self.instructions = instructions
        self.tools = tools
        return "qwen-session"


@pytest.mark.asyncio
async def test_m5_bundle_preserves_m4_voice_and_emotion_rules_and_adds_frontend_tools():
    from services.voice_instructions import load_m4_instructions, load_m5_instructions
    from services.m5_tool_contracts import realtime_tool_definitions

    m4 = load_m4_instructions()
    m5 = load_m5_instructions()

    assert "## 人格与关系" in m5.stable
    assert "## 内在证据规则" in m5.stable
    assert "## Emotion" in m5.per_turn_template
    assert "validated_emotion_label" in m5.per_turn_template
    assert "本轮工作上下文" in m5.per_turn_template
    assert "## Recall" not in m5.stable
    assert "## Conversation Continuity Packet" in m5.per_turn_template
    assert "## Memory Context" in m5.per_turn_template
    for tool_name in ("submit", "pend_message", "remember", "add_evidence"):
        assert tool_name in m5.stable
    assert "report_task_progress" not in m5.stable
    assert m5.stable_token_count < 2000
    assert len(m5.stable_hash) == 64
    assert m5.tokenizer_is_model_exact is False
    assert load_m4_instructions().stable == m4.stable

    client = _Client()
    session = VoiceRealtimeSession(
        session_id=uuid4(),
        media_session_id="media-m5",
        client=client,
    )
    await session.start(m5.stable, tool_surface="m5")

    assert session.tool_surface == "m5"
    assert client.tools[0] == {
        "type": "function",
        **VOICE_RECALL_TOOL["function"],
    }
    assert [item["name"] for item in client.tools[1:]] == [
        "submit",
        "pend_message",
        "remember",
        "add_evidence",
    ]
    assert all(
        set(item) == {"type", "name", "description", "parameters"}
        for item in client.tools
    )
    assert [item["name"] for item in realtime_tool_definitions()] == [
        "submit",
        "pend_message",
        "remember",
        "add_evidence",
    ]

    from apps.hexis_api import VoiceRealtimeSessionRequest

    assert VoiceRealtimeSessionRequest(session_id=uuid4()).tool_surface == "m4"
    assert (
        VoiceRealtimeSessionRequest(session_id=uuid4(), tool_surface="m5").tool_surface
        == "m5"
    )


@pytest.mark.asyncio
async def test_m4_remains_the_default_realtime_surface():
    from services.voice_instructions import load_m4_instructions

    client = _Client()
    session = VoiceRealtimeSession(
        session_id=uuid4(),
        media_session_id="media-m4",
        client=client,
    )
    await session.start(load_m4_instructions().stable)

    assert session.tool_surface == "m4"
    assert client.tools == [VOICE_RECALL_TOOL]
