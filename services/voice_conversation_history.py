"""Transactional voice-turn lifecycle over the existing chat and agent SoT."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid5


_EVENT_NAMESPACE = uuid5(UUID(int=0), "hexis:voice-turn-completion")


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("database function returned a non-object result")
    return value


class VoiceConversationHistory:
    def __init__(self, pool: Any) -> None:
        if pool is None:
            raise ValueError("a database pool is required for voice history")
        self._pool = pool

    async def start_turn(
        self,
        *,
        session_id: UUID,
        input_turn_id: UUID,
        input_order: int,
        user_text: str,
        context_version: str,
    ) -> UUID:
        if not isinstance(session_id, UUID) or not isinstance(input_turn_id, UUID):
            raise ValueError("voice turn identity must use UUID values")
        if isinstance(input_order, bool) or not isinstance(input_order, int) or input_order < 0:
            raise ValueError("input_order must be a non-negative integer")
        if not user_text.strip() or not context_version.strip():
            raise ValueError("voice turn needs authorized user text and context version")
        context = json.dumps(
            {
                "messages": [],
                "runtime_state": {
                    "channel": "voice",
                    "input_turn_id": str(input_turn_id),
                    "input_order": input_order,
                    "context_version": context_version,
                },
            },
            separators=(",", ":"),
        )
        async with self._pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT start_agent_turn($1, $2, $3::uuid, $4::jsonb)",
                "voice",
                user_text,
                session_id,
                context,
            )
        turn_id = _object(raw).get("turn_id")
        try:
            return UUID(str(turn_id))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("start_agent_turn returned no usable turn_id") from exc

    async def complete_turn(
        self,
        *,
        session_id: UUID,
        input_turn_id: UUID,
        input_order: int,
        stimulus_revision: int,
        turn_id: UUID,
        user_text: str,
        assistant_text: str | None,
        context_version: str,
        playback_scope: str,
        metadata: dict[str, Any],
        media_session_id: str,
        response_ids: list[str],
        playback_ids: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if playback_scope != "complete":
            assistant_text = None
        if assistant_text is not None and not assistant_text.strip():
            assistant_text = None
        source_identity = f"voice:{session_id}:{input_turn_id}:{stimulus_revision}"
        envelopes = [
            {
                "event_id": str(uuid5(_EVENT_NAMESPACE, f"{source_identity}:{event_type}")),
                "schema_version": 1,
                "event_type": event_type,
                "stream_id": "voice-realtime-session",
                "request_id": str(input_turn_id),
                "response_id": response_ids[-1] if response_ids else None,
                "media_session_id": media_session_id,
                "producer_seq": index,
                "source": "server",
                "producer_id": "hexis.voice-realtime",
                "channel": "voice",
                "payload": {
                    "source_identity": source_identity,
                    "playback_scope": playback_scope,
                    "response_ids": response_ids,
                    "playback_ids": playback_ids,
                    **({"played_response_texts": metadata["played_response_texts"]}
                       if metadata.get("played_response_texts") else {}),
                },
            }
            for index, event_type in enumerate(
                ("voice_turn_completed",), start=1
            )
        ]
        completion_metadata = {
            **metadata,
            "channel": "voice",
            "context_version": context_version,
            "media_session_id": media_session_id,
            "response_ids": response_ids,
            "playback_ids": playback_ids,
        }
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                raw = await conn.fetchval(
                    """SELECT complete_voice_turn(
                        $1::uuid, $2::uuid, $3::int, $4::int, $5::uuid,
                        $6::text, $7::text, $8::text, $9::text,
                        $10::jsonb, $11::jsonb
                    )""",
                    session_id,
                    input_turn_id,
                    input_order,
                    stimulus_revision,
                    turn_id,
                    user_text,
                    assistant_text,
                    context_version,
                    playback_scope,
                    json.dumps(completion_metadata, separators=(",", ":")),
                    json.dumps(envelopes, separators=(",", ":")),
                )
        return _object(raw)
