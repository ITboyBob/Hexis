"""M5 background skill selection and actual tool-surface audit tests."""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


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
    pool = object()
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

    assert calls[0][1]["actual_tool_names"] == {"recall", "remember", "web_search"}
    assert calls[0][1]["decision_kind"] == "background_default"
    assert selection.allowed_tool_names == {"recall"}
