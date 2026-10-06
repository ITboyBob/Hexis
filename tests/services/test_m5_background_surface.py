"""M5 background skill selection and actual tool-surface audit tests."""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _Acquire:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_):
        return None


class _Pool:
    def acquire(self):
        return _Acquire()


@pytest.mark.asyncio
async def test_background_selection_records_full_reachable_surface(monkeypatch):
    spec = importlib.util.find_spec("services.background_preparation")
    assert spec is not None, "shared M5 background preparation is not implemented"
    from services import background_preparation as module

    # The selector's result is intentionally narrower than the real CHAT registry.
    selection = SimpleNamespace(
        skills=[],
        allowed_tool_names={"recall"},
        available=[SimpleNamespace(name="core-memory")],
        considered=[],
    )
    registry = SimpleNamespace(
        pool=None,
        list_names=lambda: ["recall", "remember", "web_search"],
        get_specs=AsyncMock(return_value=[
            {"type": "function", "function": {"name": "recall"}},
            {"type": "function", "function": {"name": "remember"}},
            {"type": "function", "function": {"name": "web_search"}},
        ]),
    )
    pool = _Pool()
    calls = []

    async def select(*args, **kwargs):
        return selection

    async def record(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(module, "select_skills", select)
    monkeypatch.setattr(module, "record_selection", record)
    monkeypatch.setattr(module, "load_llm_config", AsyncMock(return_value={}))
    monkeypatch.setattr(module, "create_default_registry", lambda _: registry)
    monkeypatch.setattr(module, "get_agent_profile_context", AsyncMock(return_value={}))
    monkeypatch.setattr(module.CognitiveMemory, "hydrate", AsyncMock(return_value={}))
    monkeypatch.setattr(module, "render_chat_memory_context_db", AsyncMock(return_value=""))
    monkeypatch.setattr(module, "render_chat_continuity_context_db", AsyncMock(return_value=""))
    monkeypatch.setattr(module, "build_system_prompt", AsyncMock(return_value="prompt"))

    await module.prepare_background_turn(
        pool, "00000000-0000-0000-0000-000000000001", "task", [],
    )

    assert calls[0][0][0] is pool
    assert calls[0][1]["actual_tool_names"] == {"recall", "remember", "web_search"}
    assert calls[0][1]["decision_kind"] == "background_default"
    assert selection.allowed_tool_names == {"recall"}


@pytest.mark.asyncio(loop_scope="session")
async def test_background_preparation_persists_full_tool_surface(db_pool, monkeypatch):
    from core.tools import create_default_registry
    from services import background_preparation as module

    selection = SimpleNamespace(
        skills=[],
        allowed_tool_names={"recall"},
        available=[SimpleNamespace(name="core-memory")],
        considered=[],
    )
    registry = create_default_registry(db_pool)
    monkeypatch.setattr(module, "select_skills", AsyncMock(return_value=selection))
    monkeypatch.setattr(module, "load_llm_config", AsyncMock(return_value={}))
    monkeypatch.setattr(module, "get_agent_profile_context", AsyncMock(return_value={}))
    monkeypatch.setattr(module.CognitiveMemory, "hydrate", AsyncMock(return_value={}))
    monkeypatch.setattr(module, "render_chat_memory_context_db", AsyncMock(return_value=""))
    monkeypatch.setattr(module, "render_chat_continuity_context_db", AsyncMock(return_value=""))
    monkeypatch.setattr(module, "build_system_prompt", AsyncMock(return_value="prompt"))
    monkeypatch.setattr(module, "create_default_registry", lambda _: registry)
    session_id = "00000000-0000-0000-0000-000000000001"

    await module.prepare_background_turn(
        db_pool,
        session_id,
        "task",
        [],
        loop_options={"is_operator": False, "on_approval": None},
    )

    actual = await module._actual_tool_names(registry)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT decision_kind, allowed_tools, reachable_tools
            FROM public.tool_surface_decision_events
            WHERE session_id = $1 AND surface = 'background'
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            session_id,
        )
    assert row["decision_kind"] == "background_default"
    assert row["allowed_tools"] == sorted(actual)
    assert row["reachable_tools"] == sorted(actual)
    assert "recall" in row["reachable_tools"]
    assert set(selection.allowed_tool_names) == {"recall"}
