from __future__ import annotations

import pytest

from services.qwen_realtime_protocol import (
    ProtocolEvent,
    UnknownProtocolEvent,
    build_first_response,
    build_update_session,
    parse_event,
)


def test_parses_session_updated_gate_and_preserves_raw():
    raw = {"type": "session.updated", "session": {"id": "qwen-1"}}
    event = parse_event(raw)
    assert event == ProtocolEvent(
        type="session.updated",
        raw=raw,
        session_id="qwen-1",
    )


def test_parses_input_final_and_rejects_partial_as_authorization_source():
    event = parse_event(
        {
            "type": "input_audio_transcription.completed",
            "item_id": "item-1",
            "transcript": "请继续",
            "is_final": True,
        }
    )
    assert event.type == "input_transcript_final"
    assert event.item_id == "item-1"
    assert event.transcript == "请继续"
    partial = parse_event(
        {
            "type": "input_audio_transcription.partial",
            "item_id": "item-1",
            "transcript": "请",
        }
    )
    assert partial.type == "input_transcript_partial"


def test_response_create_uses_nested_instructions_and_explicit_response_id():
    value = build_first_response("resp-1", "每轮上下文")
    assert value == {
        "type": "response.create",
        "response": {"id": "resp-1", "instructions": "每轮上下文"},
    }


def test_update_session_uses_nested_audio_contract():
    value = build_update_session("稳定层", model="qwen-realtime", voice="Tina")
    assert value == {
        "type": "session.update",
        "session": {
            "model": "qwen-realtime",
            "instructions": "稳定层",
            "audio": {
                "input": {"format": "pcm 16000/s16le/mono"},
                "output": {"format": "pcm 24000", "voice": "Tina"},
            },
        },
    }


def test_unknown_event_is_diagnostic_not_crash():
    with pytest.raises(UnknownProtocolEvent):
        parse_event({"type": "vendor.new.event"})
