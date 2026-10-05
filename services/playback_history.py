"""Playback event recording and history aggregation over behavior events."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from services.behavior_events import (
    BehaviorEvent,
    query_behavior_events,
    record_behavior_event,
)

_SCOPES = {"complete", "partial", "unplayed", "unknown"}
_TERMINAL_EVENT_TYPES = {"voice_playback_ended", "voice_playback_interrupted"}
_PAGE_SIZE = 1000
_MAX_EVENTS = 10000
_EVENT_NAMESPACE = uuid5(UUID(int=0), "hexis:voice-playback")


def _fold_played_ms(played_ms: int, payload: dict[str, Any]) -> int:
    played = payload.get("played_ms")
    if (
        isinstance(played, int)
        and not isinstance(played, bool)
        and played >= 0
    ):
        return max(played_ms, played)
    return played_ms


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")
    return value


def _nonnegative(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _optional_uuid(value: Any, name: str) -> UUID | None:
    if value is None:
        return None
    if not isinstance(value, UUID):
        raise ValueError(f"{name} must be a UUID when provided")
    return value


def _stable_event_id(
    identity: PlaybackIdentity, event_type: str, producer_seq: int
) -> UUID:
    canonical = json.dumps(
        {
            "session_id": str(identity.session_id),
            "response_id": identity.response_id,
            "playback_id": identity.playback_id,
            "output_epoch": identity.output_epoch,
            "media_session_id": identity.media_session_id,
            "producer_seq": producer_seq,
            "event_type": event_type,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return uuid5(_EVENT_NAMESPACE, canonical)


@dataclass(frozen=True)
class PlaybackIdentity:
    session_id: UUID
    response_id: str
    playback_id: str
    output_epoch: int
    media_session_id: str
    turn_id: UUID | None = None


class PlaybackHistory:
    def __init__(self, pool: Any, identity: PlaybackIdentity) -> None:
        if pool is None:
            raise ValueError("a database pool is required to record playback history")
        if not isinstance(identity.session_id, UUID):
            raise ValueError("session_id must be a UUID")
        _text(identity.response_id, "response_id")
        _text(identity.playback_id, "playback_id")
        _nonnegative(identity.output_epoch, "output_epoch")
        _text(identity.media_session_id, "media_session_id")
        _optional_uuid(identity.turn_id, "turn_id")
        self._pool = pool
        self._identity = identity
        self._producer_seq = 0

    async def _record(
        self,
        event_type: str,
        payload: dict[str, Any],
        producer_seq: int | None,
    ) -> dict[str, Any]:
        if producer_seq is None:
            self._producer_seq += 1
        else:
            producer_seq = _nonnegative(producer_seq, "producer_seq")
            self._producer_seq = max(self._producer_seq, producer_seq)
        effective_producer_seq = (
            self._producer_seq if producer_seq is None else producer_seq
        )
        event = BehaviorEvent(
            event_id=_stable_event_id(
                self._identity, event_type, effective_producer_seq
            ),
            schema_version=1,
            event_type=event_type,
            stream_id="voice-playback",
            session_id=self._identity.session_id,
            source="voice-playback",
            producer_id="voice-playback-client",
            producer_seq=effective_producer_seq,
            channel="voice",
            payload={
                **payload,
                "playback_id": self._identity.playback_id,
                "response_id": self._identity.response_id,
                "output_epoch": self._identity.output_epoch,
                "media_session_id": self._identity.media_session_id,
            },
            response_id=self._identity.response_id,
            media_session_id=self._identity.media_session_id,
            output_epoch=self._identity.output_epoch,
            turn_id=self._identity.turn_id,
            ts=None,
        )
        return await record_behavior_event(self._pool, event)

    async def open(
        self, *, start_ms: int, producer_seq: int | None = None
    ) -> dict[str, Any]:
        return await self._record(
            "voice_playback_started",
            {"start_ms": _nonnegative(start_ms, "start_ms")},
            producer_seq,
        )

    async def audible(
        self, *, audible_ms: int, producer_seq: int | None = None
    ) -> dict[str, Any]:
        return await self._record(
            "voice_first_audible",
            {"audible_ms": _nonnegative(audible_ms, "audible_ms")},
            producer_seq,
        )

    async def progress(
        self,
        *,
        played_ms: int,
        interval_ms: int,
        phase: str,
        producer_seq: int | None = None,
    ) -> dict[str, Any]:
        return await self._record(
            "voice_playback_progress",
            {
                "played_ms": _nonnegative(played_ms, "played_ms"),
                "interval_ms": _nonnegative(interval_ms, "interval_ms"),
                "phase": _text(phase, "phase"),
            },
            producer_seq,
        )

    async def end(
        self, *, scope: str, played_ms: int, producer_seq: int | None = None
    ) -> dict[str, Any]:
        if scope not in _SCOPES:
            raise ValueError("scope must be complete, partial, unplayed, or unknown")
        return await self._record(
            "voice_playback_ended",
            {
                "scope": scope,
                "played_ms": _nonnegative(played_ms, "played_ms"),
            },
            producer_seq,
        )

    async def interrupt(
        self, *, reason: str, played_ms: int, producer_seq: int | None = None
    ) -> dict[str, Any]:
        return await self._record(
            "voice_playback_interrupted",
            {
                "reason": _text(reason, "reason"),
                "played_ms": _nonnegative(played_ms, "played_ms"),
            },
            producer_seq,
        )

    async def get(self) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        for offset in range(0, _MAX_EVENTS, _PAGE_SIZE):
            page = await query_behavior_events(
                self._pool,
                session_id=self._identity.session_id,
                response_id=self._identity.response_id,
                limit=_PAGE_SIZE,
                offset=offset,
            )
            events.extend(page["events"])
            if not page["has_more"]:
                break
        receipts = [
            event
            for event in events
            if isinstance(event.get("payload"), dict)
            and event["payload"].get("playback_id") == self._identity.playback_id
            and event.get("response_id") == self._identity.response_id
            and event.get("output_epoch") == self._identity.output_epoch
        ]
        rows = sorted(
            (
                event
                for event in receipts
                if event.get("media_session_id") == self._identity.media_session_id
            ),
            key=lambda event: event["producer_seq"],
        )

        scope = "unknown"
        played_ms = 0
        for event in rows:
            payload = event["payload"]
            event_type = event["event_type"]
            if event_type not in _TERMINAL_EVENT_TYPES:
                played_ms = _fold_played_ms(played_ms, payload)
                continue
            if event_type == "voice_playback_ended":
                ended_scope = payload.get("scope")
                scope = ended_scope if ended_scope in _SCOPES else "unknown"
            else:
                interrupted_ms = payload.get("played_ms", 0)
                if (
                    isinstance(interrupted_ms, int)
                    and not isinstance(interrupted_ms, bool)
                    and interrupted_ms >= 0
                ):
                    scope = "unplayed" if interrupted_ms == 0 else "partial"
            played_ms = _fold_played_ms(played_ms, payload)
            break
        return {
            "playback_id": self._identity.playback_id,
            "response_id": self._identity.response_id,
            "output_epoch": self._identity.output_epoch,
            "media_session_id": self._identity.media_session_id,
            "scope": scope,
            "played_ms": played_ms,
            "event_count": len(receipts),
        }


__all__ = ["PlaybackHistory", "PlaybackIdentity"]
