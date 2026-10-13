"""验证云端轮次、无转录输入以及控制命令的真实边界。"""

from types import SimpleNamespace
import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from services.voice_rtc import RtcConfig, RtcProtocolError, RtcVoiceSession

pytestmark = pytest.mark.asyncio(loop_scope="function")


class History:
    def __init__(self):
        self.inputs = []
        self.outputs = []

    async def start_audio_input(self, **values):
        self.inputs.append(values)
        return uuid4()

    async def save_generated_output(self, **values):
        self.outputs.append(values)


def make_session():
    history = History()
    sent = []

    async def send(message):
        sent.append(message)

    config = RtcConfig.from_mapping(
        {
            "endpoint": "wss://workspace.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime",
            "webrtc_endpoint": "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1/webrtc/realtime",
            "model": "selected-model",
            "voice": "selected-voice",
            "api_key_env": "TEST_RTC_KEY",
            "timeout": 30,
        }
    )
    session = RtcVoiceSession(
        session_id=uuid4(),
        media_session_id=uuid4(),
        config=config,
        context=SimpleNamespace(
            instructions="真实会话上下文", context_version="a" * 64
        ),
        history=history,
        tools=[],
        send=send,
    )
    return session, history, sent


async def ready(session):
    await session.provider_event(
        {"event_id": "created", "type": "session.created", "session": {"id": "cloud-1"}}
    )
    await session.provider_event(
        {
            "event_id": "updated",
            "type": "session.updated",
            "session": session.session_config,
        }
    )


async def test_bootstrap_explicitly_disables_transcription_and_uses_cloud_vad():
    session, _, _ = make_session()
    assert session.session_config["input_audio_transcription"] is None
    assert session.session_config["turn_detection"] == {"type": "semantic_vad"}
    assert session.session_config["instructions"] == "真实会话上下文"
    assert "create_response" not in session.session_config["turn_detection"]


@pytest.mark.parametrize(
    "changed",
    [
        {"audio": {}, "voice": "wrong-voice"},
        {"audio": {"output": {"voice": "wrong-voice"}}},
        {"id": "another-cloud"},
        {"model": "another-model"},
    ],
)
async def test_config_confirmation_rejects_voice_or_cloud_identity_drift(changed):
    session, _, sent = make_session()
    await session.provider_event(
        {"type": "session.created", "session": {"id": "cloud-1"}}
    )
    with pytest.raises(RtcProtocolError):
        await session.provider_event(
            {
                "type": "session.updated",
                "session": {
                    **session.session_config,
                    **changed,
                },
            }
        )
    assert not session.ready
    assert not any(message.get("type") == "ready" for message in sent)


async def test_config_confirmation_accepts_official_output_voice_field():
    session, _, _ = make_session()
    await session.provider_event(
        {"type": "session.created", "session": {"id": "cloud-1"}}
    )
    config = {
        **session.session_config,
        "voice": "compatibility-value",
        "audio": {"output": {"voice": session.config.voice}},
    }
    await session.provider_event({"type": "session.updated", "session": config})
    assert session.ready


async def test_config_confirmation_accepts_legacy_voice_echo():
    session, _, _ = make_session()
    await session.provider_event(
        {"type": "session.created", "session": {"id": "cloud-1"}}
    )
    config = {**session.session_config, "voice": session.config.voice}
    config.pop("audio")
    await session.provider_event({"type": "session.updated", "session": config})
    assert session.ready


async def test_committed_input_is_metadata_only_and_duplicate_is_idempotent():
    session, history, sent = make_session()
    await ready(session)
    event = {
        "event_id": "commit-1",
        "type": "input_audio_buffer.committed",
        "item_id": "audio-1",
    }
    await session.provider_event(event)
    await session.provider_event(event)
    assert len(history.inputs) == 1
    assert history.inputs[0]["provider_item_id"] == "audio-1"
    assert "user_text" not in history.inputs[0]
    assert not any(message.get("type") == "provider_command" for message in sent)


async def test_generated_output_is_saved_with_unknown_playback_without_receipts():
    session, history, _ = make_session()
    await ready(session)
    await session.provider_event(
        {"type": "input_audio_buffer.committed", "item_id": "audio-1"}
    )
    await session.provider_event(
        {"type": "response.created", "response": {"id": "response-1"}}
    )
    await session.provider_event(
        {
            "type": "response.audio_transcript.delta",
            "response_id": "response-1",
            "item_id": "output-1",
            "delta": "你好",
        }
    )
    await session.provider_event(
        {
            "type": "response.done",
            "response": {
                "id": "response-1",
                "status": "completed",
                "output": [
                    {
                        "id": "output-1",
                        "type": "message",
                        "content": [{"type": "audio", "transcript": "你好"}],
                    }
                ],
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        }
    )
    assert len(history.outputs) == 1
    assert history.outputs[0]["assistant_text"] == "你好"
    assert history.outputs[0]["playback_scope"] == "unknown"


async def test_config_mismatch_and_same_event_id_changed_payload_fail_loudly():
    session, _, _ = make_session()
    await session.provider_event(
        {"type": "session.created", "session": {"id": "cloud-1"}}
    )
    with pytest.raises(RtcProtocolError):
        await session.provider_event(
            {
                "type": "session.updated",
                "session": {
                    **session.session_config,
                    "input_audio_transcription": {"model": "asr"},
                },
            }
        )
    session, _, _ = make_session()
    await ready(session)
    await session.provider_event(
        {"event_id": "same", "type": "input_audio_buffer.committed", "item_id": "one"}
    )
    with pytest.raises(RtcProtocolError):
        await session.provider_event(
            {
                "event_id": "same",
                "type": "input_audio_buffer.committed",
                "item_id": "two",
            }
        )


async def test_cloud_speech_never_dispatches_local_stop_or_cancel():
    session, _, sent = make_session()
    await ready(session)
    await session.provider_event(
        {"type": "input_audio_buffer.speech_started", "item_id": "candidate"}
    )
    await session.provider_event(
        {"type": "input_audio_buffer.speech_stopped", "item_id": "candidate"}
    )
    assert not any(message.get("type") == "provider_command" for message in sent)


async def test_signaling_endpoint_must_use_https_and_match_selected_workspace():
    session, _, _ = make_session()
    mapping = {
        **session.config.__dict__,
        "webrtc_endpoint": "https://other.example/api/v1/webrtc/realtime",
    }
    with pytest.raises(ValueError):
        RtcConfig.from_mapping(mapping)


@pytest.mark.parametrize("surface", ["m4", "m5"])
async def test_tool_binding_keeps_selected_surface_and_canceled_tools_finish_turn(
    surface,
):
    session, history, _ = make_session()
    session.tool_surface = surface
    await ready(session)
    await session.provider_event(
        {"type": "input_audio_buffer.committed", "item_id": "audio"}
    )
    await session.provider_event(
        {"type": "response.created", "response": {"id": "response"}}
    )
    await session.provider_event(
        {
            "type": "response.function_call_arguments.done",
            "response_id": "response",
            "call_id": "call",
            "name": "voice_recall",
            "arguments": '{"query":"真实问题"}',
        }
    )
    assert session.calls["call"].tool_surface == surface
    await session.provider_event(
        {"type": "response.done", "response": {"id": "response", "status": "cancelled"}}
    )
    assert history.outputs[0]["final_for_turn"] is True
    assert not session._tool_tasks


async def test_browser_sent_receipt_does_not_count_as_cloud_acceptance():
    session, _, sent = make_session()
    session.config = replace(session.config, timeout=0.02)

    async def send(message):
        sent.append(message)
        if message.get("type") == "provider_command":
            session.command_ack(message["command_id"], "sent")

    async def tool(*args):
        return '{"status":"no_results"}'

    session.send = send
    session.tool_executor = tool
    await ready(session)
    await session.provider_event(
        {"type": "input_audio_buffer.committed", "item_id": "input"}
    )
    await session.provider_event(
        {"type": "response.created", "response": {"id": "parent"}}
    )
    await session.provider_event(
        {
            "type": "response.function_call_arguments.done",
            "response_id": "parent",
            "call_id": "call",
            "name": "voice_recall",
            "arguments": '{"query":"问题"}',
        }
    )
    await session.provider_event(
        {"type": "response.done", "response": {"id": "parent", "status": "completed"}}
    )
    await asyncio.wait_for(session.closed.wait(), 1)
    commands = [
        message["event"]["type"]
        for message in sent
        if message.get("type") == "provider_command"
    ]
    assert commands == ["conversation.item.create", "response.create"]
    assert "TimeoutError" in next(
        message["message"] for message in sent if message.get("type") == "error"
    )
