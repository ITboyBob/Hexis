"""Stable event envelopes shared by voice persistence and realtime delivery."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

from services.behavior_events import BehaviorEvent


_VOICE_EVENT_NAMESPACE = UUID(int=0)


def build_voice_behavior_event(
    *,
    session_id: UUID,
    media_session_id: str,
    producer_seq: int,
    event_type: str,
    payload: dict[str, Any],
    input_turn_id: str | None = None,
    stimulus_revision: int | None = None,
    response_id: str | None = None,
    output_epoch: int | None = None,
    turn_id: UUID | None = None,
    call_id: str | None = None,
) -> BehaviorEvent:
    """Build one deterministic producer-owned event for both A and live WS."""
    event_id = uuid5(
        _VOICE_EVENT_NAMESPACE,
        f"hexis:voice:{session_id}:{media_session_id}:{producer_seq}:{event_type}",
    )
    return BehaviorEvent(
        event_id=event_id,
        schema_version=1,
        event_type=event_type,
        stream_id="voice-realtime-session",
        session_id=session_id,
        source="server",
        producer_id="hexis.voice-realtime",
        producer_seq=producer_seq,
        channel="voice",
        payload=dict(payload),
        media_session_id=media_session_id,
        input_turn_id=input_turn_id,
        stimulus_revision=stimulus_revision,
        response_id=response_id,
        output_epoch=output_epoch,
        turn_id=turn_id,
        call_id=call_id,
    )


def voice_behavior_envelope(event: BehaviorEvent, *, database_seq: int) -> dict[str, Any]:
    """Return the persisted envelope with its DB-owned stream sequence."""
    if not isinstance(event, BehaviorEvent):
        raise TypeError("event must be a BehaviorEvent")
    if isinstance(database_seq, bool) or not isinstance(database_seq, int) or database_seq <= 0:
        raise ValueError("database_seq must be a positive integer")
    return {
        "event_id": str(event.event_id),
        "schema_version": event.schema_version,
        "event_type": event.event_type,
        "stream_id": event.stream_id,
        "session_id": str(event.session_id),
        "request_id": event.request_id,
        "stimulus_revision": event.stimulus_revision,
        "input_turn_id": event.input_turn_id,
        "response_id": event.response_id,
        "media_session_id": event.media_session_id,
        "output_epoch": event.output_epoch,
        "turn_id": str(event.turn_id) if event.turn_id else None,
        "task_id": event.task_id,
        "call_id": event.call_id,
        "seq": database_seq,
        "ts": event.ts.isoformat() if isinstance(event.ts, datetime) else None,
        "source": event.source,
        "producer_id": event.producer_id,
        "producer_seq": event.producer_seq,
        "channel": event.channel,
        "payload": dict(event.payload),
    }
