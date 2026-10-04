"""Small, fail-loud mapping for the Qwen Realtime events used by BOB-101."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class UnknownProtocolEvent(ValueError):
    """A vendor event is outside the explicitly verified BOB-101 subset."""


@dataclass(frozen=True)
class ProtocolEvent:
    type: str
    raw: dict[str, Any] | None = None
    session_id: str | None = None
    item_id: str | None = None
    transcript: str | None = None
    is_final: bool | None = None
    response_id: str | None = None


def parse_event(value: dict[str, Any]) -> ProtocolEvent:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise ValueError("Qwen Realtime 事件必须是带 type 的 JSON 对象")

    event_type = value["type"]
    if event_type == "session.updated":
        session = value.get("session") or {}
        return ProtocolEvent(
            type="session.updated",
            raw=value,
            session_id=session.get("id"),
        )
    if event_type == "input_audio_transcription.completed":
        return ProtocolEvent(
            type="input_transcript_final",
            raw=value,
            item_id=value.get("item_id"),
            transcript=value.get("transcript"),
            is_final=True,
        )
    if event_type == "input_audio_transcription.partial":
        return ProtocolEvent(
            type="input_transcript_partial",
            raw=value,
            item_id=value.get("item_id"),
            transcript=value.get("transcript"),
            is_final=False,
        )
    if event_type in {
        "response.text.delta",
        "response.audio.delta",
        "response.done",
        "response.failed",
    }:
        return ProtocolEvent(type=event_type, raw=value, response_id=value.get("response_id"))
    raise UnknownProtocolEvent(event_type)


def build_update_session(
    instructions: str, *, model: str, voice: str
) -> dict[str, Any]:
    return {
        "type": "session.update",
        "session": {
            "model": model,
            "instructions": instructions,
            "audio": {
                "input": {"format": "pcm 16000/s16le/mono"},
                "output": {"format": "pcm 24000", "voice": voice},
            },
        },
    }


def build_first_response(
    response_id: str, per_response_instructions: str
) -> dict[str, Any]:
    return {
        "type": "response.create",
        "response": {"id": response_id, "instructions": per_response_instructions},
    }
