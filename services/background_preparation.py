"""Shared preparation of authoritative background agent turns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from core.agent_api import get_agent_profile_context
from core.cognitive_memory_api import CognitiveMemory, render_chat_memory_context_db
from core.llm_config import load_llm_config
from core.tools import ToolContext, create_default_registry
from services.agent import (
    _user_content_with_visuals,
    build_system_prompt,
    render_chat_continuity_context_db,
)
from services.agent_questions import question_timeout_seconds
from services.operator_approval import create_operator_approval_callback
from services.skill_runtime import record_selection, select_skills


@dataclass(frozen=True)
class PreparedBackgroundTurn:
    system_prompt: str
    user_message: str
    user_content: str | list[dict[str, Any]]
    history: list[dict[str, Any]]
    llm_config: dict[str, Any]
    registry: Any
    selection: Any
    session_id: str
    loop_options: dict[str, Any]


async def _actual_tool_names(registry: Any) -> set[str]:
    specs = await registry.get_specs(ToolContext.CHAT)
    specified = {
        str(item["function"]["name"])
        for item in specs
        if isinstance(item, dict)
        and isinstance(item.get("function"), dict)
        and item["function"].get("name")
    }
    return specified & {str(name) for name in registry.list_names()}


async def _loop_options(
    pool: Any,
    session_id: str,
    llm_config: dict[str, Any],
    supplied: dict[str, Any] | None,
) -> dict[str, Any]:
    options = dict(supplied or {})
    approval_extension = 0
    if options.get("on_approval") is None:
        callback, approval_extension = await create_operator_approval_callback(
            pool,
            tool_context=ToolContext.CHAT,
            session_id=session_id,
            heartbeat_id=None,
            surface="background",
        )
        options["on_approval"] = callback

    def configured_number(name: str, fallback: float, cast: type) -> Any:
        value = options.get(name, llm_config.get(name))
        try:
            return cast(value) if value is not None else cast(fallback)
        except (TypeError, ValueError):
            return cast(fallback)

    base_timeout = configured_number("timeout_seconds", 300.0, float)
    options["timeout_seconds"] = (
        base_timeout + approval_extension + await question_timeout_seconds(pool)
    )
    options["max_tokens"] = configured_number("max_tokens", 4096, int)
    options["temperature"] = configured_number("temperature", 0.7, float)
    options.setdefault("is_operator", False)
    return options


async def prepare_background_turn(
    pool: Any,
    session_id: UUID | str,
    user_message: str,
    history: list[dict[str, Any]],
    *,
    prompt_addenda: list[str] | None = None,
    visual_attachments: list[dict[str, Any]] | None = None,
    loop_options: dict[str, Any] | None = None,
) -> PreparedBackgroundTurn:
    """Prepare one turn from DB-owned state and trusted host options."""
    session_id = str(session_id)
    async with pool.acquire() as conn:
        llm_config = await load_llm_config(conn, "llm.chat", fallback_key="llm")
    registry = create_default_registry(pool)
    profile = await get_agent_profile_context(pool=pool)
    context = await CognitiveMemory(pool).hydrate(
        user_message,
        memory_limit=10,
        include_partial=True,
        include_identity=True,
        include_worldview=True,
        include_emotional_state=True,
        include_goals=True,
        include_drives=True,
        session_id=session_id,
        exclude_sensitive=False,
    )
    async with pool.acquire() as conn:
        memory_context = await render_chat_memory_context_db(
            conn, context, max_memories=10
        )
        continuity = await render_chat_continuity_context_db(
            conn, session_id, strict=True
        )

    selection = await select_skills(
        registry,
        ToolContext.CHAT,
        query=user_message,
    )
    await record_selection(
        pool,
        selection,
        registry=registry,
        session_id=session_id,
        surface="background",
        tool_context=ToolContext.CHAT,
        query=user_message,
        actual_tool_names=await _actual_tool_names(registry),
        decision_kind="background_default",
    )

    system_prompt = await build_system_prompt(
        "chat",
        registry,
        profile,
        active_skills=selection.skills,
        available_skills=selection.available,
        allowed_tool_names=None,
        surface="background",
        prompt_addenda=prompt_addenda,
    )
    assembled_user_message = "\n\n".join(
        part
        for part in (continuity, memory_context, user_message)
        if part
    )
    user_content = _user_content_with_visuals(
        assembled_user_message, visual_attachments
    )
    resolved_loop_options = await _loop_options(
        pool, session_id, llm_config, loop_options
    )
    return PreparedBackgroundTurn(
        system_prompt=system_prompt,
        user_message=assembled_user_message,
        user_content=user_content,
        history=history,
        llm_config=llm_config,
        registry=registry,
        selection=selection,
        session_id=session_id,
        loop_options=resolved_loop_options,
    )
