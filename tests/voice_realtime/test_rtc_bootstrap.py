"""初始化只查询已有真实文字，不编造当前用户话语。"""

from uuid import uuid4
from types import SimpleNamespace

import pytest

from services.voice_rtc_bootstrap import prepare_rtc_context

pytestmark = pytest.mark.asyncio(loop_scope="function")


class Pool:
    def __init__(self, message):
        self.message = message

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def fetchval(self, query, *args):
        if "FROM chat_messages" in query:
            return self.message
        if "get_temporal_context" in query:
            return {"now": "2026-10-12", "timezone": "Asia/Shanghai"}
        if "get_agent_profile_context" in query:
            return {"persona": {"name": "真实人格"}}
        if "render_active_persona" in query:
            return "真实人格"
        if "gather_turn_context" in query:
            return {"worldview": ["真实世界观"]}
        raise AssertionError(query)


@pytest.mark.parametrize("message", [None, "已有真实用户文字"])
async def test_bootstrap_uses_history_once_and_never_calls_emotion(
    monkeypatch, message
):
    recalled = []

    async def hydrate(pool, **kwargs):
        recalled.append(kwargs["message"])
        return SimpleNamespace(per_response_instructions="已有历史检索出的上下文")

    async def continuity(*args, **kwargs):
        return "真实会话连续性"

    monkeypatch.setattr("services.voice_rtc_bootstrap.hydrate_voice_context", hydrate)
    monkeypatch.setattr(
        "services.voice_rtc_bootstrap.render_chat_continuity_context_db", continuity
    )
    monkeypatch.setattr(
        "services.voice_rtc_bootstrap.load_m5_instructions",
        lambda: SimpleNamespace(stable="稳定人格指令"),
    )
    result = await prepare_rtc_context(Pool(message), session_id=uuid4())
    assert recalled == ([message] if message else [])
    assert len(result.context_version) == 64
    assert "稳定人格指令" in result.instructions
    assert "{temporal" not in result.instructions
