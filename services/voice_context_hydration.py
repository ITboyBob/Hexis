"""Deterministic pre-loop hydration for the realtime voice path."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from core.cognitive_memory_api import (
    CognitiveMemory,
    render_chat_memory_context_db,
)
from services.agent import render_chat_continuity_context_db
from services.voice_instructions import M4InstructionBundle, VoiceInstructionError, render_per_turn


class HydrationUnavailable(RuntimeError):
    """The authoritative context needed for a response is unavailable."""


@dataclass(frozen=True)
class VoiceHydrationResult:
    per_response_instructions: str
    instructions_hash: str
    hydrate_ms: float
    memory_count: int
    persona_context: str
    continuity_context: str
    memory_context: str
    temporal_context: str
    token_count: int
    tokenizer: str
    tokenizer_is_model_exact: bool


def _temporal_line(raw: Any) -> str:
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict) or not value.get("now"):
        raise HydrationUnavailable("temporal context is unavailable")
    timezone = value.get("timezone", "UTC")
    return f"Current date and time: {value['now']} ({timezone})."


async def hydrate_voice_context(
    pool: Any,
    *,
    session_id: UUID,
    message: str,
    stable_instructions_hash: str,
    memory_limit: int = 10,
    instruction_bundle: M4InstructionBundle | None = None,
) -> VoiceHydrationResult:
    """Hydrate and render the per-response context without inventing fallbacks."""
    if not isinstance(stable_instructions_hash, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", stable_instructions_hash
    ):
        raise ValueError("stable_instructions_hash must be a SHA-256 hex string")
    if not message.strip():
        raise ValueError("message must not be empty")
    if isinstance(memory_limit, bool) or not isinstance(memory_limit, int) or not 1 <= memory_limit <= 10:
        raise ValueError("memory_limit must be an integer between 1 and 10")

    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        client = CognitiveMemory(pool)
        context = await client.hydrate(
            message,
            memory_limit=memory_limit,
            include_partial=True,
            include_identity=True,
            include_worldview=True,
            include_emotional_state=True,
            include_goals=True,
            include_drives=True,
            session_id=session_id,
        )
        async with pool.acquire() as conn:
            memory_context = await render_chat_memory_context_db(
                conn,
                context,
                max_memories=memory_limit,
            )
            continuity_context = await render_chat_continuity_context_db(
                conn,
                str(session_id),
                exclude_sensitive=False,
                strict=True,
            )
            temporal_raw = await conn.fetchval("SELECT get_temporal_context()")
            profile_raw = await conn.fetchval("SELECT get_agent_profile_context()")
            profile = json.loads(profile_raw) if isinstance(profile_raw, str) else profile_raw
            persona_source = profile.get("persona") if isinstance(profile, dict) else None
            if not isinstance(persona_source, dict) or not persona_source:
                raise HydrationUnavailable("active persona is unavailable")
            persona_raw = await conn.fetchval(
                "SELECT render_active_persona($1::jsonb)", json.dumps(persona_source)
            )
    except Exception as exc:
        if isinstance(exc, HydrationUnavailable):
            raise
        raise HydrationUnavailable("voice context hydration failed") from exc

    temporal = _temporal_line(temporal_raw)
    if not isinstance(persona_raw, str) or not persona_raw.strip():
        raise HydrationUnavailable("active persona renderer returned no required identity content")
    continuity = continuity_context.strip()
    if len(continuity) > 8000:
        raise HydrationUnavailable(
            f"continuity context is {len(continuity)} characters; exceeds the 8000-character M4 budget without safe section-level trimming"
        )
    if instruction_bundle is None:
        from services.voice_instructions import load_m4_instructions

        instruction_bundle = load_m4_instructions()
    try:
        rendered = render_per_turn(
            instruction_bundle,
            now=temporal.removeprefix("Current date and time: "),
            persona=persona_raw.strip(),
            continuity=continuity or "## Conversation Continuity Packet\nNo continuity records are available.",
            memory=memory_context.strip() or "## Memory Context\nNo relevant memories were retrieved.",
        )
    except VoiceInstructionError as exc:
        raise HydrationUnavailable(str(exc)) from exc
    return VoiceHydrationResult(
        per_response_instructions=rendered.text,
        instructions_hash=stable_instructions_hash,
        hydrate_ms=(loop.time() - started) * 1000,
        memory_count=len(context.memories),
        persona_context=persona_raw.strip(),
        continuity_context=continuity or "## Conversation Continuity Packet\nNo continuity records are available.",
        memory_context=memory_context.strip() or "## Memory Context\nNo relevant memories were retrieved.",
        temporal_context=temporal.removeprefix("Current date and time: "),
        token_count=rendered.token_count,
        tokenizer=rendered.tokenizer,
        tokenizer_is_model_exact=rendered.tokenizer_is_model_exact,
    )
