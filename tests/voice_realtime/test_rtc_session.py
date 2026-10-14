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


async def test_bootstrap_enables_async_transcription_and_uses_cloud_vad():
    session, _, _ = make_session()
    assert session.session_config["input_audio_transcription"] == {
        "model": "qwen3-asr-flash-realtime"
    }
    assert session.session_config["turn_detection"] == {"type": "semantic_vad"}
    assert session.session_config["instructions"] == "真实会话上下文"
    assert "create_response" not in session.session_config["turn_detection"]


async def test_transcription_branches_do_not_block_response_and_are_idempotent():
    session, history, sent = make_session()
    release = asyncio.Event()
    evaluated = []

    async def evaluate(text):
        evaluated.append(text)
        await release.wait()
        return {"label": "happy", "wordset_version": "v1"}

    async def save(**values):
        await release.wait()
        return {"message_id": str(uuid4())}

    session.emotion_evaluator = evaluate
    history.save_user_transcript = save
    await ready(session)
    transcript = {
        "type": "conversation.item.input_audio_transcription.completed",
        "item_id": "audio-1",
        "transcript": "很高兴见到你",
    }
    await session.provider_event(transcript)
    assert not evaluated
    await session.provider_event(
        {"type": "input_audio_buffer.committed", "item_id": "audio-1"}
    )
    await session.provider_event({**transcript, "event_id": "duplicate-final"})
    await asyncio.sleep(0)
    await asyncio.wait_for(
        session.provider_event(
            {"type": "response.created", "response": {"id": "response-1"}}
        ),
        0.1,
    )
    assert evaluated == ["很高兴见到你"]
    assert sum(m["type"] == "user_text" for m in sent) == 1
    release.set()
    await asyncio.gather(*tuple(session._side_tasks))
    assert any(m["type"] == "user_text_saved" for m in sent)
    assert any(m["type"] == "emotion_result" and m["label"] == "happy" for m in sent)
    assert not any(m["type"] == "provider_command" for m in sent)
    await session.close()


async def test_branch_failure_and_transcription_failure_keep_voice_ready():
    session, history, sent = make_session()

    async def fail(*args, **kwargs):
        raise RuntimeError("受控支路失败")

    session.emotion_evaluator = fail
    history.save_user_transcript = fail
    await ready(session)
    await session.provider_event(
        {"type": "input_audio_buffer.committed", "item_id": "audio-1"}
    )
    await session.provider_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "audio-1",
            "transcript": "你好",
        }
    )
    await asyncio.gather(*tuple(session._side_tasks))
    await session.provider_event(
        {
            "type": "conversation.item.input_audio_transcription.failed",
            "item_id": "other",
        }
    )
    assert session.ready and not session.closed.is_set()
    assert any(m["type"] == "side_task_status" for m in sent)
    assert not any(m["type"] == "error" for m in sent)
    await session.close()


async def test_close_cancels_pending_emotion_without_late_events():
    session, history, sent = make_session()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def evaluate(text):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def save(**values):
        return {"message_id": str(uuid4())}

    session.emotion_evaluator = evaluate
    history.save_user_transcript = save
    await ready(session)
    await session.provider_event(
        {"type": "input_audio_buffer.committed", "item_id": "audio-1"}
    )
    await session.provider_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "audio-1",
            "transcript": "你好",
        }
    )
    await asyncio.wait_for(started.wait(), 0.1)
    await session.close()
    assert cancelled.is_set()
    assert not any(m["type"] == "emotion_result" for m in sent)


@pytest.mark.parametrize("delivery_failure", [False, True])
async def test_final_asr_after_response_done_and_side_delivery_failure(
    delivery_failure,
):
    session, history, sent = make_session()
    ran = []

    async def save(**values):
        ran.append("save")
        return {"message_id": str(uuid4())}

    async def evaluate(text):
        ran.append("emotion")
        return {"label": "curious", "wordset_version": "v1"}

    history.save_user_transcript = save
    session.emotion_evaluator = evaluate
    await ready(session)
    await session.provider_event(
        {"type": "input_audio_buffer.committed", "item_id": "audio-1"}
    )
    await session.provider_event(
        {"type": "response.created", "response": {"id": "reply"}}
    )
    await session.provider_event(
        {
            "type": "response.done",
            "response": {"id": "reply", "status": "completed", "output": []},
        }
    )
    if delivery_failure:

        async def fail_send(message):
            raise RtcProtocolError("受控发送队列已满")

        session.send = fail_send
    await session.provider_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "audio-1",
            "transcript": "迟到的最终转录",
        }
    )
    await asyncio.gather(*tuple(session._side_tasks))
    assert sorted(ran) == ["emotion", "save"]
    assert session.ready and not session.closed.is_set()
    if not delivery_failure:
        assert any(m["type"] == "user_text_saved" for m in sent)
    await session.close()


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
