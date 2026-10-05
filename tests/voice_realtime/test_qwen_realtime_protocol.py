from __future__ import annotations

import pytest

from services.qwen_realtime_protocol import (
    ProtocolEvent,
    UnknownProtocolEvent,
    build_cancel_response,
    build_function_call_output,
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


def test_parses_updated_session_instructions_and_event_id():
    raw = {
        "type": "session.updated",
        "event_id": "evt-1",
        "session": {"id": "qwen-1", "instructions": "稳定层\n\n每轮层"},
    }
    event = parse_event(raw)
    assert event.event_id == "evt-1"
    assert event.instructions == "稳定层\n\n每轮层"


def test_parses_session_created_as_known_readiness_preamble():
    raw = {"type": "session.created", "session": {"id": "qwen-1"}}
    event = parse_event(raw)
    assert event.type == "session.created"
    assert event.session_id == "qwen-1"


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


def test_response_create_only_triggers_response_and_uses_explicit_response_id():
    value = build_first_response("resp-1", event_id="evt-1")
    assert value == {
        "type": "response.create",
        "event_id": "evt-1",
        "response": {"id": "resp-1"},
    }


def test_response_create_does_not_redefine_session_tools():
    value = build_first_response("resp-1", event_id="evt-1")
    assert "tools" not in value["response"]
    assert "tool_choice" not in value["response"]


def test_cancel_response_is_an_explicit_control_message():
    assert build_cancel_response() == {"type": "response.cancel"}


def test_function_call_output_is_bound_to_exact_call_id():
    assert build_function_call_output("call-1", '{"status":"no_results"}') == {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": '{"status":"no_results"}',
        },
    }


def test_parses_qwen_final_input_transcription_delta_and_function_call():
    partial = parse_event(
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "item-1",
            "delta": "请查找",
        }
    )
    call = parse_event(
        {
            "type": "response.function_call_arguments.done",
            "response_id": "resp-1",
            "item_id": "item-2",
            "call_id": "call-1",
            "name": "voice_recall",
            "arguments": '{"query":"project decision"}',
        }
    )
    assert partial.type == "input_transcript_partial"
    assert partial.text == "请查找"
    assert partial.item_id == "item-1"
    assert call.response_id == "resp-1"
    assert call.item_id == "item-2"
    assert call.call_id == "call-1"
    assert call.tool_name == "voice_recall"
    assert call.tool_arguments == '{"query":"project decision"}'


def test_parses_generated_audio_transcript_delta_and_done():
    delta = parse_event(
        {
            "type": "response.audio_transcript.delta",
            "response_id": "resp-1",
            "delta": "你好",
        }
    )
    done = parse_event(
        {
            "type": "response.audio_transcript.done",
            "response_id": "resp-1",
            "transcript": "你好，我在。",
        }
    )

    assert delta.type == "response_audio_transcript_delta"
    assert delta.response_id == "resp-1"
    assert delta.text == "你好"
    assert done.type == "response_audio_transcript_done"
    assert done.response_id == "resp-1"
    assert done.text == "你好，我在。"


def test_update_session_uses_nested_audio_contract():
    value = build_update_session("稳定层", voice="Tina")
    assert value == {
        "type": "session.update",
        "session": {
            "modalities": ["text", "audio"],
            "instructions": "稳定层",
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
                    "voice": "Tina",
                    "format": {"type": "pcm", "sample_rate": 24000},
                },
            },
        },
    }


def test_update_session_can_attach_event_id_for_serialized_turn_updates():
    value = build_update_session(
        "稳定层\n\n每轮层", voice="Tina", event_id="evt-1"
    )
    assert value["event_id"] == "evt-1"
    assert value["session"]["instructions"] == "稳定层\n\n每轮层"


def test_update_session_registers_qwen_function_tools_at_session_scope():
    tool = {
        "type": "function",
        "function": {
            "name": "voice_recall",
            "description": "read-only recall",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    value = build_update_session("稳定层", voice="Tina", tools=[tool])
    assert value["session"]["tools"] == [tool]
    assert value["session"]["tool_choice"] == "auto"


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


@pytest.mark.parametrize(
    "event_type",
    [
        "response.content_part.added",
        "response.content_part.done",
        "response.audio.done",
    ],
)
def test_parses_non_terminal_qwen_response_lifecycle_events(event_type):
    raw = {
        "type": event_type,
        "response_id": "resp-1",
        "item_id": "item-1",
        "output_index": 0,
    }

    event = parse_event(raw)

    assert event.type == event_type
    assert event.response_id == "resp-1"
    assert event.item_id == "item-1"
    assert event.raw == raw


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
