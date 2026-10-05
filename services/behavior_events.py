"""Typed, thin access to the DB-authoritative behavior event log."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class BehaviorEvent:
    event_id: UUID
    schema_version: int
    event_type: str
    stream_id: str
    session_id: UUID
    source: str
    producer_id: str
    producer_seq: int
    channel: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: datetime | None = None
    request_id: str | None = None
    stimulus_revision: int | None = None
    input_turn_id: str | None = None
    response_id: str | None = None
    media_session_id: str | None = None
    output_epoch: int | None = None
    turn_id: UUID | None = None
    task_id: str | None = None
    call_id: str | None = None


def _json(value: Any) -> dict[str, Any] | list[Any]:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")
    return value


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string when provided")
    return value


def _optional_nonnegative(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer when provided")
    return value


def _validate(event: BehaviorEvent) -> None:
    if not isinstance(event.event_id, UUID):
        raise ValueError("event_id must be a UUID")
    if not isinstance(event.session_id, UUID):
        raise ValueError("session_id must be a UUID")
    if (
        not isinstance(event.schema_version, int)
        or isinstance(event.schema_version, bool)
        or event.schema_version <= 0
    ):
        raise ValueError("schema_version must be a positive integer")
    _required_text(event.event_type, "event_type")
    _required_text(event.stream_id, "stream_id")
    _required_text(event.source, "source")
    _required_text(event.producer_id, "producer_id")
    _required_text(event.channel, "channel")
    if (
        not isinstance(event.producer_seq, int)
        or isinstance(event.producer_seq, bool)
        or event.producer_seq < 0
    ):
        raise ValueError("producer_seq must be non-negative")
    _optional_nonnegative(event.stimulus_revision, "stimulus_revision")
    _optional_nonnegative(event.output_epoch, "output_epoch")
    _optional_text(event.request_id, "request_id")
    _optional_text(event.input_turn_id, "input_turn_id")
    _optional_text(event.response_id, "response_id")
    _optional_text(event.media_session_id, "media_session_id")
    _optional_text(event.task_id, "task_id")
    _optional_text(event.call_id, "call_id")
    if event.turn_id is not None and not isinstance(event.turn_id, UUID):
        raise ValueError("turn_id must be a UUID when provided")
    if event.ts is not None and not isinstance(event.ts, datetime):
        raise ValueError("ts must be a datetime when provided")
    if not isinstance(event.payload, dict):
        raise ValueError("payload must be a JSON object")


def _cursor(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not value:
        raise ValueError("cursor must be a non-empty stream_id to after_seq mapping")
    normalized: dict[str, int] = {}
    for stream_id, after_seq in value.items():
        _required_text(stream_id, "cursor stream_id")
        if (
            not isinstance(after_seq, int)
            or isinstance(after_seq, bool)
            or after_seq < 0
        ):
            raise ValueError("cursor after_seq must be a non-negative integer")
        normalized[stream_id] = after_seq
    return normalized


async def record_behavior_event(pool: Any, event: BehaviorEvent) -> dict[str, Any]:
    """Record one producer-owned event; exact retries deduplicate in Postgres."""
    if pool is None:
        raise ValueError("a database pool is required to record behavior events")
    _validate(event)
    async with pool.acquire() as conn:
        raw = await conn.fetchval(
            """
            SELECT record_behavior_event(
                $1::uuid, $2::int, $3, $4, $5::uuid,
                $6, $7::int, $8, $9, $10, $11::int,
                $12::uuid, $13, $14, $15::timestamptz,
                $16, $17, $18::bigint, $19, $20::jsonb
            )
            """,
            event.event_id,
            event.schema_version,
            event.event_type,
            event.stream_id,
            event.session_id,
            event.request_id,
            event.stimulus_revision,
            event.input_turn_id,
            event.response_id,
            event.media_session_id,
            event.output_epoch,
            event.turn_id,
            event.task_id,
            event.call_id,
            event.ts,
            event.source,
            event.producer_id,
            event.producer_seq,
            event.channel,
            json.dumps(event.payload, default=str),
        )
    result = _json(raw)
    if not isinstance(result, dict):
        raise RuntimeError("the database did not return a behavior event receipt")
    return result


async def query_behavior_events(
    pool: Any,
    *,
    session_id: UUID | None = None,
    stream_id: str | None = None,
    event_type: str | None = None,
    turn_id: UUID | None = None,
    request_id: str | None = None,
    response_id: str | None = None,
    task_id: str | None = None,
    call_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
    cursor: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Query persisted envelopes newest-first for diagnostics and projections."""
    if pool is None:
        raise ValueError("a database pool is required to query behavior events")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    normalized_cursor = _cursor(cursor)
    async with pool.acquire() as conn:
        raw = await conn.fetchval(
            """
            SELECT get_behavior_events(
                $1::uuid, $2, $3, $4::uuid, $5, $6, $7, $8,
                $9::int, $10::int, $11::jsonb
            )
            """,
            session_id,
            stream_id,
            event_type,
            turn_id,
            request_id,
            response_id,
            task_id,
            call_id,
            limit,
            offset,
            json.dumps(normalized_cursor) if normalized_cursor is not None else None,
        )
    result = _json(raw)
    if not isinstance(result, dict):
        raise RuntimeError("the database did not return a behavior event page")
    return result


async def replay_behavior_events(
    pool: Any,
    *,
    session_id: UUID,
    stream_id: str | None = None,
    limit: int = 10000,
    cursor: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Return per-stream replay order and sequence-gap diagnostics."""
    if pool is None:
        raise ValueError("a database pool is required to replay behavior events")
    if not isinstance(session_id, UUID):
        raise ValueError("session_id must be a UUID")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    normalized_cursor = _cursor(cursor)
    async with pool.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT replay_behavior_events($1::uuid, $2, $3::int, $4::jsonb)",
            session_id,
            stream_id,
            limit,
            json.dumps(normalized_cursor) if normalized_cursor is not None else None,
        )
    result = _json(raw)
    if not isinstance(result, dict):
        raise RuntimeError("the database did not return a behavior replay")
    return result


__all__ = [
    "BehaviorEvent",
    "query_behavior_events",
    "record_behavior_event",
    "replay_behavior_events",
]
