"""Shared preparation of authoritative background agent turns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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


async def prepare_background_turn(*args: Any, **kwargs: Any) -> PreparedBackgroundTurn:
    """Prepare a turn from DB-owned state and trusted host options."""
    raise NotImplementedError
