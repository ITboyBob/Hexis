"""M5 controlled background preparation contract tests."""

from __future__ import annotations

import importlib.util
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


class _Acquire:
    def __init__(self, conn: object) -> None:
        self.conn = conn

    async def __aenter__(self) -> object:
        return self.conn

    async def __aexit__(self, *_: object) -> None:
        return None


class _Pool:
    def __init__(self) -> None:
        self.conn = object()

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


@pytest.mark.asyncio
async def test_prepare_orders_hydration_continuity_selection_and_record(monkeypatch):
    spec = importlib.util.find_spec("services.background_preparation")
    assert spec is not None, "shared M5 background preparation is not implemented"
    from services import background_preparation as module

    events: list[str] = []
    session_id = uuid4()
    context = {"identity": "kept", "worldview": "kept", "drives": "kept"}
    registry = SimpleNamespace(
        get_specs=AsyncMock(return_value=[
            {"type": "function", "function": {"name": "recall"}},
        ]),
        list_names=lambda: ["recall"],
    )
    selection = SimpleNamespace(
        skills=[SimpleNamespace(name="core-memory")],
        allowed_tool_names={"recall"},
        available=[SimpleNamespace(name="core-memory")],
        considered=[{"name": "core-memory", "score": 1}],
    )

    async def load_config(_conn, key, *, fallback_key):
        events.append("config")
        assert (key, fallback_key) == ("llm.chat", "llm")
        return {"provider": "test", "model": "test-model"}

    monkeypatch.setattr(module, "load_llm_config", load_config)
    monkeypatch.setattr(module, "create_default_registry", lambda pool: registry)
    monkeypatch.setattr(
        module,
        "get_agent_profile_context",
        AsyncMock(side_effect=lambda **_: events.append("profile") or {"persona": "p"}),
    )

    async def hydrate(*args, **kwargs):
        events.append("hydrate")
        assert args[1] == "summarize this"
        assert kwargs["session_id"] == str(session_id)
        assert all(kwargs[name] is True for name in (
            "include_identity", "include_worldview", "include_emotional_state",
            "include_goals", "include_drives",
        ))
        return context

    monkeypatch.setattr(module.CognitiveMemory, "hydrate", hydrate)

    async def render_memory(conn, hydrated, *, max_memories):
        events.append("memory")
        assert hydrated is context and max_memories == 10
        return "memory context"

    async def render_continuity(conn, current_session, *, strict):
        events.append("continuity")
        assert current_session == str(session_id) and strict is True
        return "continuity context"

    monkeypatch.setattr(module, "render_chat_memory_context_db", render_memory)
    monkeypatch.setattr(module, "render_chat_continuity_context_db", render_continuity)

    async def select(*args, **kwargs):
        events.append("select")
        assert args[:2] == (registry, module.ToolContext.CHAT)
        assert kwargs["query"] == "summarize this"
        return selection

    async def record(*args, **kwargs):
        events.append("record")
        assert args[0] is not None
        assert kwargs["surface"] == "background"

    monkeypatch.setattr(module, "select_skills", select)
    monkeypatch.setattr(module, "record_selection", record)

    async def build_prompt(*args, **kwargs):
        events.append("prompt")
        assert kwargs["active_skills"] == selection.skills
        assert kwargs["available_skills"] == selection.available
        assert kwargs["allowed_tool_names"] is None
        return "system prompt"

    monkeypatch.setattr(module, "build_system_prompt", build_prompt)
    prepared = await module.prepare_background_turn(
        _Pool(),
        session_id,
        "summarize this",
        [{"role": "user", "content": "authoritative history"}],
        loop_options={"is_operator": True, "on_approval": object()},
        prompt_addenda=["background addendum"],
        visual_attachments=[{"name": "chart", "mime_type": "image/png", "data_url": "data:image/png;base64,abc"}],
    )

    assert events.index("hydrate") < events.index("continuity") < events.index("select") < events.index("record")
    assert "appraisal" not in events and "subconscious" not in events
    assert prepared.system_prompt == "system prompt"
    assert prepared.session_id == str(session_id)
    assert prepared.history == [{"role": "user", "content": "authoritative history"}]
    assert prepared.selection is selection
    assert prepared.user_message == (
        "continuity context\n\nmemory context\n\n[USER MESSAGE]\nsummarize this"
    )
    assert prepared.user_content[0] == {"type": "input_text", "text": prepared.user_message}
    assert prepared.user_content[2]["type"] == "input_image"
    with pytest.raises(FrozenInstanceError):
        prepared.user_message = "changed"


@pytest.mark.asyncio
async def test_execute_work_passes_authoritative_turn_and_host_options(monkeypatch):
    spec = importlib.util.find_spec("services.background_preparation")
    assert spec is not None, "shared M5 background preparation is not implemented"
    from services.background_preparation import PreparedBackgroundTurn
    from services.background_executor import BackgroundExecutor
    from core.agent_loop import AgentLoop

    registry = SimpleNamespace(
        get_specs=AsyncMock(return_value=[
            {"type": "function", "function": {"name": "recall"}},
        ]),
        list_names=lambda: ["recall"],
    )
    selection = SimpleNamespace(skills=[], available=[])
    callback = AsyncMock()
    host_operator = object()
    prepared = PreparedBackgroundTurn(
        system_prompt="prompt",
        user_message="continuity + user task",
        user_content=[{"type": "input_text", "text": "continuity + user task"}],
        history=[{"role": "user", "content": "DB history"}],
        llm_config={"provider": "test", "model": "test-model"},
        registry=registry,
        selection=selection,
        session_id=str(uuid4()),
        loop_options={
            "is_operator": True,
            "on_approval": host_operator,
            "timeout_seconds": 360.0,
            "temperature": 0.4,
            "max_tokens": 2048,
        },
    )
    captured: dict[str, Any] = {}

    async def fake_run(self, user_message, *, history, user_content):
        captured.update(config=self.config, user_message=user_message, history=history, user_content=user_content)
        return "result"

    monkeypatch.setattr(AgentLoop, "run", fake_run)
    result = await BackgroundExecutor(_Pool()).execute_work(prepared, on_event=callback)

    assert result == "result"
    assert captured["user_message"] == prepared.user_message
    assert captured["history"] is prepared.history
    assert captured["user_content"] is prepared.user_content
    assert captured["config"].allowed_tool_names is None
    assert captured["config"].is_operator is True
    assert captured["config"].on_approval is host_operator
    assert captured["config"].on_event is callback


@pytest.mark.asyncio
async def test_background_loop_options_do_not_treat_missing_approval_as_opt_out(monkeypatch):
    from services import background_preparation as module

    pool = _Pool()
    approval_callback = object()
    callback_factory = AsyncMock(return_value=(approval_callback, 12))
    question_timeout = AsyncMock(return_value=34)
    monkeypatch.setattr(module, "create_operator_approval_callback", callback_factory)
    monkeypatch.setattr(module, "question_timeout_seconds", question_timeout)

    options = await module._loop_options(
        pool,
        "session-id",
        {"timeout_seconds": 120, "max_tokens": 3000, "temperature": 0.2},
        {"on_approval": None},
    )

    callback_factory.assert_awaited_once()
    assert options["on_approval"] is approval_callback
    assert options["timeout_seconds"] == 166
    assert options["max_tokens"] == 3000
    assert options["temperature"] == 0.2
