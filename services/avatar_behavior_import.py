"""Adapt isolated Avatar preview evidence into the global behavior log."""

from __future__ import annotations

import asyncpg
from typing import Any
from uuid import UUID

from services.behavior_events import BehaviorEvent, record_behavior_event


def _uuid(value: Any, name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{name} must be a UUID") from exc


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _nonnegative(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _event_from_avatar(item: Any, session_id: UUID) -> BehaviorEvent:
    if not isinstance(item, dict):
        raise ValueError("each Avatar event must be an object")
    payload = item.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        raise ValueError("Avatar payload must contain a non-empty type")

    event_session = _uuid(item.get("session_id"), "session_id")
    if event_session != session_id:
        raise ValueError("Avatar event session_id does not match evidence session_id")

    identity = payload.get("identity")
    if identity is not None:
        if not isinstance(identity, dict):
            raise ValueError("Avatar identity must be an object")
        if _uuid(identity.get("session_id"), "identity.session_id") != session_id:
            raise ValueError(
                "Avatar identity session_id does not match evidence session_id"
            )

    task = payload.get("task")
    task_id = None
    if isinstance(task, dict):
        task_id = task.get("task_id")
        if task_id is not None:
            task_id = _text(task_id, "task.task_id")

    feedback = payload.get("feedback")
    output_epoch = None
    if isinstance(feedback, dict):
        output_epoch = _nonnegative(
            feedback.get("output_epoch"), "feedback.output_epoch"
        )

    stimulus_revision = None
    input_turn_id = None
    if isinstance(identity, dict):
        stimulus_revision = _nonnegative(
            identity.get("stimulus_revision"), "identity.stimulus_revision"
        )
        input_turn_id = _text(identity.get("input_turn_id"), "identity.input_turn_id")

    return BehaviorEvent(
        event_id=_uuid(item.get("event_id"), "event_id"),
        schema_version=1,
        event_type=f"avatar.{_text(payload['type'], 'payload.type')}",
        stream_id=_text(item.get("stream_id"), "stream_id"),
        session_id=session_id,
        source="avatar-preview",
        producer_id="avatar-preview-ui",
        producer_seq=_nonnegative(item.get("producer_seq"), "producer_seq"),
        channel="avatar-preview",
        payload=payload,
        stimulus_revision=stimulus_revision,
        input_turn_id=input_turn_id,
        output_epoch=output_epoch,
        task_id=task_id,
    )


async def import_avatar_preview_evidence(pool: Any, evidence: Any) -> dict[str, Any]:
    """Import only Avatar evidence events; diagnostics and projection stay local."""
    if not isinstance(evidence, dict):
        raise ValueError("Avatar evidence must be an object")
    if evidence.get("schema_version") != "avatar-preview-evidence-v1":
        raise ValueError("unsupported Avatar evidence schema_version")
    events = evidence.get("events")
    if not isinstance(events, list):
        raise ValueError("Avatar evidence events must be an array")
    session_id = _uuid(evidence.get("session_id"), "session_id")

    accepted = 0
    deduplicated = 0
    rejected = 0
    rejections: list[dict[str, str]] = []
    rejection_samples_dropped = 0
    for index, item in enumerate(events):
        try:
            receipt = await record_behavior_event(
                pool, _event_from_avatar(item, session_id)
            )
            if receipt["deduplicated"]:
                deduplicated += 1
            else:
                accepted += 1
        except (ValueError, asyncpg.RaiseError) as exc:
            rejected += 1
            if len(rejections) < 250:
                rejections.append(
                    {
                        "index": str(index),
                        "reason": str(exc),
                        "error_type": type(exc).__name__,
                    }
                )
            else:
                rejection_samples_dropped += 1

    return {
        "schema_version": "avatar-behavior-import-v1",
        "session_id": str(session_id),
        "accepted": accepted,
        "deduplicated": deduplicated,
        "rejected": rejected,
        "rejections": rejections,
        "rejection_samples_dropped": rejection_samples_dropped,
    }


__all__ = ["import_avatar_preview_evidence"]
