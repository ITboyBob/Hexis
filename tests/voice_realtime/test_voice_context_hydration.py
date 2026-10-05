from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.voice_context_hydration import (
    HydrationUnavailable,
    hydrate_voice_context,
)

pytestmark = [pytest.mark.asyncio(loop_scope="function")]


class FakeConnection:
    def __init__(self):
        self.queries = []

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return False

    async def fetchval(self, query, *args):
        self.queries.append((query, args))
        if "get_temporal_context" in query:
            return json.dumps({"now": "2026-10-04 10:00", "timezone": "Asia/Shanghai"})
        if "get_agent_profile_context" in query:
            return json.dumps({"persona": {"name": "Hexis", "values": ["honesty"]}})
        if "render_active_persona" in query:
            return "Name: Hexis\nValues: honesty"
        raise AssertionError(query)


class FakeHydrator:
    def __init__(self, connection):
        self.connection = connection

    async def hydrate(self, message, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(memories=[object()])


async def _fake_render(connection, name):
    return {
        "memory": "## Memory Context\nrendered",
        "continuity": "## Conversation Continuity Packet\nrendered",
    }[name]


async def test_hydrates_renders_and_reports_interval(monkeypatch):
    connection = FakeConnection()
    hydrator = FakeHydrator(connection)
    monkeypatch.setattr(
        "services.voice_context_hydration.CognitiveMemory",
        lambda pool: hydrator,
    )
    monkeypatch.setattr(
        "services.voice_context_hydration.render_chat_memory_context_db",
        lambda conn, context, **kwargs: _fake_render(conn, "memory"),
    )
    monkeypatch.setattr(
        "services.voice_context_hydration.render_chat_continuity_context_db",
        lambda conn, session_id, **kwargs: _fake_render(conn, "continuity"),
    )
    session_id = uuid4()
    result = await hydrate_voice_context(
        connection,
        session_id=session_id,
        message="请继续",
        stable_instructions_hash="a" * 64,
    )
    assert "## Now" in result.per_response_instructions
    assert "## Conversation Continuity Packet" in result.per_response_instructions
    assert "## Memory Context" in result.per_response_instructions
    assert result.instructions_hash == "a" * 64
    assert result.hydrate_ms >= 0
    assert result.memory_count == 1
    assert "## Active Persona" in result.per_response_instructions
    assert result.token_count > 0
    assert result.tokenizer_is_model_exact is False
    assert hydrator.kwargs["memory_limit"] == 10


async def test_memory_limit_cannot_exceed_ten():
    with pytest.raises(ValueError, match="between 1 and 10"):
        await hydrate_voice_context(
            object(),
            session_id=uuid4(),
            message="请继续",
            stable_instructions_hash="a" * 64,
            memory_limit=11,
        )


async def test_over_budget_continuity_fails_without_truncating(monkeypatch):
    connection = FakeConnection()
    hydrator = FakeHydrator(connection)
    monkeypatch.setattr("services.voice_context_hydration.CognitiveMemory", lambda pool: hydrator)
    monkeypatch.setattr(
        "services.voice_context_hydration.render_chat_memory_context_db",
        lambda conn, context, **kwargs: _fake_render(conn, "memory"),
    )
    monkeypatch.setattr(
        "services.voice_context_hydration.render_chat_continuity_context_db",
        lambda *args, **kwargs: _large_continuity(),
    )
    with pytest.raises(HydrationUnavailable, match="8000-character M4 budget"):
        await hydrate_voice_context(
            connection,
            session_id=uuid4(),
            message="请继续",
            stable_instructions_hash="a" * 64,
        )


async def _large_continuity():
    return "x" * 8001


async def test_hydration_failure_is_explicit_not_empty_context(monkeypatch):
    class BrokenHydrator:
        async def hydrate(self, *_args, **_kwargs):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "services.voice_context_hydration.CognitiveMemory",
        lambda pool: BrokenHydrator(),
    )
    with pytest.raises(HydrationUnavailable):
        await hydrate_voice_context(
            object(),
            session_id=uuid4(),
            message="请继续",
            stable_instructions_hash="a" * 64,
        )


async def test_continuity_failure_is_hydration_unavailable(monkeypatch):
    connection = FakeConnection()
    hydrator = FakeHydrator(connection)
    monkeypatch.setattr(
        "services.voice_context_hydration.CognitiveMemory",
        lambda pool: hydrator,
    )
    monkeypatch.setattr(
        "services.voice_context_hydration.render_chat_memory_context_db",
        lambda conn, context, **kwargs: _fake_render(conn, "memory"),
    )

    with pytest.raises(HydrationUnavailable):
        await hydrate_voice_context(
            connection,
            session_id=uuid4(),
            message="请继续",
            stable_instructions_hash="a" * 64,
        )
