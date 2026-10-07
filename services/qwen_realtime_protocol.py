"""Small, fail-loud mapping for the Qwen Realtime events used by BOB-101."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class UnknownProtocolEvent(ValueError):
    """A vendor event is outside the explicitly verified BOB-101 subset."""


_FINAL_TRANSCRIPT_EVENTS = {
    "input_audio_transcription.completed",
    "conversation.item.input_audio_transcription.completed",
}
_PARTIAL_TRANSCRIPT_EVENTS = {
    "input_audio_transcription.partial",
    "conversation.item.input_audio_transcription.partial",
    "conversation.item.input_audio_transcription.delta",
}
_FAILED_TRANSCRIPT_EVENTS = {
    "input_audio_transcription.failed",
    "conversation.item.input_audio_transcription.failed",
}


@dataclass(frozen=True)
class ProtocolEvent:
    type: str
    raw: dict[str, Any] | None = None
    event_id: str | None = None
    session_id: str | None = None
    instructions: str | None = None
    item_id: str | None = None
    transcript: str | None = None
    is_final: bool | None = None
    response_id: str | None = None
    audio: str | None = None
    text: str | None = None
    status: str | None = None
    output_index: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: str | None = None


def _error_fields(value: dict[str, Any]) -> dict[str, str | None]:
    error = value.get("error")
    if isinstance(error, dict):
        return {
            "error_code": error.get("code"),
            "error_message": error.get("message"),
        }
    if isinstance(error, str):
        return {"error_code": None, "error_message": error}
    return {"error_code": None, "error_message": None}


def parse_event(value: dict[str, Any]) -> ProtocolEvent:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise ValueError("Qwen Realtime 事件必须是带 type 的 JSON 对象")

    event_type = value["type"]
    if event_type == "session.created":
        session = value.get("session") or {}
        return ProtocolEvent(
            type="session.created",
            raw=value,
            session_id=session.get("id"),
        )
    if event_type == "session.updated":
        session = value.get("session") or {}
        return ProtocolEvent(
            type="session.updated",
            raw=value,
            event_id=value.get("event_id"),
            session_id=session.get("id"),
            instructions=session.get("instructions"),
        )
    if event_type == "input_audio_buffer.committed":
        return ProtocolEvent(
            type="input_audio_buffer_committed",
            raw=value,
            item_id=value.get("item_id"),
        )
    if event_type == "input_audio_buffer.cleared":
        return ProtocolEvent(
            type="input_audio_buffer_cleared",
            raw=value,
            event_id=value.get("event_id"),
        )
    if event_type == "conversation.item.created":
        item = value.get("item") or {}
        return ProtocolEvent(
            type="conversation.item.created",
            raw=value,
            item_id=item.get("id"),
            call_id=item.get("call_id"),
            tool_name=item.get("name"),
            tool_arguments=item.get("arguments"),
        )
    if event_type in _FINAL_TRANSCRIPT_EVENTS:
        return ProtocolEvent(
            type="input_transcript_final",
            raw=value,
            item_id=value.get("item_id"),
            transcript=value.get("transcript"),
            is_final=True,
        )
    if event_type in _PARTIAL_TRANSCRIPT_EVENTS:
        return ProtocolEvent(
            type="input_transcript_partial",
            raw=value,
            item_id=value.get("item_id"),
            transcript=value.get("transcript") or value.get("delta"),
            text=value.get("delta"),
            is_final=False,
        )
    if event_type in _FAILED_TRANSCRIPT_EVENTS:
        return ProtocolEvent(
            type="input_transcript_failed",
            raw=value,
            item_id=value.get("item_id"),
            **_error_fields(value),
        )
    if event_type == "response.created":
        response = value.get("response") or {}
        return ProtocolEvent(
            type="response.created",
            raw=value,
            response_id=response.get("id"),
        )
    if event_type in {
        "response.output_item.added",
        "response.output_item.done",
    }:
        item = value.get("item") or {}
        return ProtocolEvent(
            type=event_type,
            raw=value,
            response_id=value.get("response_id"),
            item_id=item.get("id"),
            call_id=item.get("call_id"),
            tool_name=item.get("name"),
            tool_arguments=item.get("arguments"),
        )
    if event_type in {
        "response.content_part.added",
        "response.content_part.done",
        "response.audio.done",
    }:
        return ProtocolEvent(
            type=event_type,
            raw=value,
            response_id=value.get("response_id"),
            item_id=value.get("item_id"),
            output_index=value.get("output_index"),
        )
    if event_type == "response.function_call_arguments.done":
        return ProtocolEvent(
            type=event_type,
            raw=value,
            event_id=value.get("event_id"),
            response_id=value.get("response_id"),
            item_id=value.get("item_id"),
            call_id=value.get("call_id"),
            tool_name=value.get("name"),
            tool_arguments=value.get("arguments"),
        )
    if event_type == "response.function_call_arguments.delta":
        return ProtocolEvent(
            type=event_type,
            raw=value,
            event_id=value.get("event_id"),
            response_id=value.get("response_id"),
            item_id=value.get("item_id"),
            call_id=value.get("call_id"),
            text=value.get("delta"),
        )
    if event_type == "response.done":
        response = value.get("response") or {}
        return ProtocolEvent(
            type="response.done",
            raw=value,
            response_id=response.get("id") or value.get("response_id"),
            status=response.get("status"),
        )
    if event_type == "response.failed":
        response = value.get("response") or {}
        error = response.get("error") if isinstance(response, dict) else None
        error_value = {"error": error} if error is not None else value
        return ProtocolEvent(
            type="response.failed",
            raw=value,
            response_id=response.get("id") or value.get("response_id"),
            **_error_fields(error_value),
        )
    if event_type == "error":
        return ProtocolEvent(
            type="response_failed",
            raw=value,
            **_error_fields(value),
        )
    if event_type in {
        "response.text.delta",
        "response.audio.delta",
        "response.audio_transcript.delta",
        "response.audio_transcript.done",
    }:
        if event_type in {"response.audio.delta", "response.audio_transcript.delta"}:
            return ProtocolEvent(
                type=(
                    "response_audio_transcript_delta"
                    if event_type == "response.audio_transcript.delta"
                    else event_type
                ),
                raw=value,
                response_id=value.get("response_id"),
                item_id=value.get("item_id"),
                output_index=value.get("output_index"),
                audio=value.get("delta") if event_type == "response.audio.delta" else None,
                text=value.get("delta") if event_type == "response.audio_transcript.delta" else None,
            )
        if event_type == "response.audio_transcript.done":
            return ProtocolEvent(
                type="response_audio_transcript_done",
                raw=value,
                response_id=value.get("response_id"),
                item_id=value.get("item_id"),
                output_index=value.get("output_index"),
                text=value.get("transcript") or value.get("text"),
            )
        return ProtocolEvent(
            type=event_type, raw=value, response_id=value.get("response_id")
        )
    raise UnknownProtocolEvent(event_type)


def build_update_session(
    instructions: str,
    *,
    voice: str,
    tools: list[dict[str, Any]] | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    session: dict[str, Any] = {
        "type": "session.update",
        "session": {
            "modalities": ["text", "audio"],
            "instructions": instructions,
            "turn_detection": None,
            "input_audio_transcription": {
                "model": "qwen3-asr-flash-realtime"
            },
            "audio": {
                "input": {
                    "format": {
                        "type": "pcm",
                        "sample_rate": 16000,
                        "sample_format": "s16le",
                        "channels": 1,
                        "packing": "interleaved",
                        "channel_layout": "mono",
                    }
                },
                "output": {
                    "voice": voice,
                    "format": {"type": "pcm", "sample_rate": 24000},
                },
            },
        },
    }
    if tools:
        session["session"]["tools"] = tools
        session["session"]["tool_choice"] = "auto"
    if event_id is not None:
        session["event_id"] = event_id
    return session


def build_first_response(
    response_id: str,
    *,
    event_id: str,
) -> dict[str, Any]:
    return {
        "type": "response.create",
        "event_id": event_id,
        "response": {"id": response_id},
    }


def build_function_call_output(call_id: str, output: str) -> dict[str, Any]:
    if not isinstance(call_id, str) or not call_id.strip():
        raise ValueError("call_id must be a non-empty string")
    if not isinstance(output, str):
        raise ValueError("function call output must be a string")
    return {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        },
    }


def build_cancel_response() -> dict[str, Any]:
    return {"type": "response.cancel"}
