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
    for event_type in [
        "input_audio_transcription.completed",
        "conversation.item.input_audio_transcription.completed",
    ]:
        event = parse_event(
            {
                "type": event_type,
                "item_id": "item-1",
                "transcript": "请继续",
                "is_final": True,
            }
        )
        assert event.type == "input_transcript_final"
        assert event.item_id == "item-1"
        assert event.transcript == "请继续"
        assert event.is_final is True

    for event_type in [
        "input_audio_transcription.partial",
        "conversation.item.input_audio_transcription.partial",
    ]:
        partial = parse_event(
            {
                "type": event_type,
                "item_id": "item-1",
                "transcript": "请",
            }
        )
        assert partial.type == "input_transcript_partial"
        assert partial.is_final is False


def test_response_create_uses_nested_instructions_and_explicit_response_id():
    value = build_first_response("resp-1", "每轮上下文")
    assert value == {
        "type": "response.create",
        "response": {"id": "resp-1", "instructions": "每轮上下文"},
    }


def test_update_session_uses_nested_audio_contract():
    value = build_update_session("稳定层", voice="Tina")
    assert value == {
        "type": "session.update",
        "session": {
            "modalities": ["text", "audio"],
            "instructions": "稳定层",
            "turn_detection": None,
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
                    "voice": "Tina",
                    "format": {"type": "pcm", "sample_rate": 24000},
                },
            },
        },
    }


def test_parses_input_audio_buffer_committed_as_typed_binding():
    raw = {"type": "input_audio_buffer.committed", "item_id": "item-1"}
    event = parse_event(raw)
    assert event == ProtocolEvent(
        type="input_audio_buffer_committed",
        raw=raw,
        item_id="item-1",
    )


def test_parses_response_created_with_nested_response_id():
    raw = {"type": "response.created", "response": {"id": "resp-1"}}
    event = parse_event(raw)
    assert event == ProtocolEvent(
        type="response.created",
        raw=raw,
        response_id="resp-1",
    )


def test_parses_response_done_with_nested_response_id():
    raw = {"type": "response.done", "response": {"id": "resp-1"}}
    event = parse_event(raw)
    assert event == ProtocolEvent(
        type="response.done",
        raw=raw,
        response_id="resp-1",
    )


def test_parses_vendor_error_as_structured_failure():
    raw = {
        "type": "error",
        "error": {"code": "server_error", "message": "Cloud response failed"},
    }
    event = parse_event(raw)
    assert event == ProtocolEvent(
        type="response_failed",
        raw=raw,
        error_code="server_error",
        error_message="Cloud response failed",
    )


def test_parses_transcription_failed_as_structured_failure():
    raw = {
        "type": "input_audio_transcription.failed",
        "item_id": "item-1",
        "error": {"code": "transcription_failed", "message": "Audio unusable"},
    }
    event = parse_event(raw)
    assert event == ProtocolEvent(
        type="input_transcript_failed",
        raw=raw,
        item_id="item-1",
        error_code="transcription_failed",
        error_message="Audio unusable",
    )


def test_unknown_event_is_diagnostic_not_crash():
    with pytest.raises(UnknownProtocolEvent):
        parse_event({"type": "vendor.new.event"})
