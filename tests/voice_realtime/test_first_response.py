from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.behavior_events import BehaviorEvent
from services.qwen_realtime_protocol import ProtocolEvent
from services.voice_first_response import (
    FirstResponseGate,
    GateNotReady,
    run_first_response_turn,
)
from services.voice_input_authorization import (
    CaptureRange,
    FinalTranscript,
    InputAuthorizationService,
    InputIdentity,
)


def test_response_requires_commit_release_and_context():
    gate = FirstResponseGate()
    with pytest.raises(GateNotReady, match="正式输入"):
        gate.assert_ready()
    gate.mark_committed()
    with pytest.raises(GateNotReady, match="评估"):
        gate.assert_ready()
    gate.mark_emotion_released()
    with pytest.raises(GateNotReady, match="上下文"):
        gate.assert_ready()
    gate.mark_context_ready()
    gate.assert_ready()


@pytest.mark.asyncio(loop_scope="function")
async def test_gate_waits_until_all_three_facts_arrive():
    gate = FirstResponseGate()
    waiter = asyncio.create_task(gate.wait_until_ready())
    await asyncio.sleep(0)
    assert not waiter.done()
    gate.mark_committed()
    gate.mark_emotion_released()
    gate.mark_context_ready()
    await asyncio.wait_for(waiter, timeout=0.1)


@pytest.mark.asyncio(loop_scope="function")
async def test_single_shot_gate_cannot_be_reused_for_new_input():
    gate = FirstResponseGate()
    gate.mark_committed()
    gate.mark_emotion_released()
    gate.mark_context_ready()
    await gate.wait_until_ready()
    gate.close()
    with pytest.raises(GateNotReady, match="已关闭"):
        gate.assert_ready()


class FakeClient:
    def __init__(self, final_event: ProtocolEvent, committed_item_id: str = "item-1"):
        self.final_event = final_event
        self.committed_item_id = committed_item_id
        self.commit_calls = 0
        self.create_calls: list[tuple[str, str]] = []
        self.pending_instructions: str | None = None

    async def commit_audio(self) -> None:
        self.commit_calls += 1

    async def wait_for_input_committed(self) -> ProtocolEvent:
        return ProtocolEvent(
            type="input_audio_buffer_committed",
            item_id=self.committed_item_id,
        )

    async def wait_for_final(self, item_id: str | None = None) -> ProtocolEvent:
        if item_id is not None and item_id != self.final_event.item_id:
            raise AssertionError("final 未绑定到 commit 回执 item_id")
        return self.final_event

    async def update_session_instructions(self, instructions: str) -> None:
        self.pending_instructions = instructions

    async def create_first_response(self, response_id: str) -> None:
        self.create_calls.append((response_id, self.pending_instructions or ""))


class FakeSink:
    def __init__(self):
        self.events: list[BehaviorEvent] = []

    async def __call__(self, event: BehaviorEvent) -> None:
        self.events.append(event)


def make_turn_fakes():
    identity = InputIdentity(
        session_id=uuid4(),
        input_turn_id=uuid4(),
        input_order=0,
        stimulus_revision=0,
    )
    source = CaptureRange(
        session_id=identity.session_id,
        media_session_id="media-1",
        capture_instance_id=uuid4(),
        start_frame=100,
        end_frame=300,
        start_monotonic_ms=1_000,
        end_monotonic_ms=2_000,
        audio_sha256="a" * 64,
    )
    final = FinalTranscript(
        identity=identity,
        capture_instance_id=source.capture_instance_id,
        media_session_id=source.media_session_id,
        item_id="item-1",
        transcript="请继续",
        transcript_version=1,
        is_final=True,
    )
    event = ProtocolEvent(
        type="input_transcript_final",
        item_id=final.item_id,
        transcript=final.transcript,
        is_final=True,
    )
    return identity, source, final, FakeClient(event)


@pytest.mark.asyncio(loop_scope="function")
async def test_first_response_waits_for_release_and_hydration_in_parallel():
    identity, source, final, client = make_turn_fakes()
    sink = FakeSink()

    async def release() -> object:
        await asyncio.sleep(0.01)
        return SimpleNamespace(released=True, context_ready=True)

    async def hydration() -> object:
        await asyncio.sleep(0.02)
        return SimpleNamespace(per_response_instructions="每轮上下文")

    started = time.monotonic()
    result = await run_first_response_turn(
        authorization_service=InputAuthorizationService(),
        final_transcript=final,
        capture_range=source,
        release=release(),
        hydration=hydration(),
        client=client,
        event_sink=sink,
        response_id="resp-1",
    )
    elapsed = time.monotonic() - started

    assert result.ok is True
    assert result.gate.closed is True
    assert client.create_calls == [("resp-1", "每轮上下文")]
    assert 0.018 <= elapsed < 0.09
    assert result.release_wait_ms >= 0
    assert result.hydration_wait_ms >= 0
    assert [event.event_type for event in sink.events] == [
        "voice_commit_sent",
        "voice_input_committed",
        "voice_response_created",
    ]
    assert all(isinstance(event, BehaviorEvent) for event in sink.events)
    assert all(event.session_id == identity.session_id for event in sink.events)
    assert sink.events[2].input_turn_id == str(identity.input_turn_id)
    assert sink.events[2].stimulus_revision == identity.stimulus_revision


@pytest.mark.asyncio(loop_scope="function")
async def test_release_failure_returns_structured_error_without_response():
    _, source, final, client = make_turn_fakes()
    sink = FakeSink()

    async def release() -> None:
        raise RuntimeError("emotion service unavailable")

    async def hydration() -> object:
        return SimpleNamespace(per_response_instructions="每轮上下文")

    result = await run_first_response_turn(
        authorization_service=InputAuthorizationService(),
        final_transcript=final,
        capture_range=source,
        release=release(),
        hydration=hydration(),
        client=client,
        event_sink=sink,
        response_id="resp-1",
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.stage == "release"
    assert result.error.error_type == "RuntimeError"
    assert result.error.message == "emotion service unavailable"
    assert result.gate.closed is True
    assert client.create_calls == []
    assert [event.event_type for event in sink.events] == [
        "voice_commit_sent",
        "voice_input_committed",
    ]

    _, source, final, client = make_turn_fakes()
    sink = FakeSink()

    async def blocked_release() -> object:
        return SimpleNamespace(released=False)

    result = await run_first_response_turn(
        authorization_service=InputAuthorizationService(),
        final_transcript=final,
        capture_range=source,
        release=blocked_release(),
        hydration=hydration(),
        client=client,
        event_sink=sink,
        response_id="resp-1",
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.stage == "release"
    assert result.error.error_type == "RuntimeError"
    assert result.error.message == "C 节点未放行"
    assert result.gate.closed is True
    assert client.create_calls == []
    assert [event.event_type for event in sink.events] == [
        "voice_commit_sent",
        "voice_input_committed",
    ]


def _future(value):
    future = asyncio.get_running_loop().create_future()
    future.set_result(value)
    return future


@pytest.mark.asyncio(loop_scope="function")
async def test_same_complete_identity_returns_existing_result_without_resend():
    _, source, final, client = make_turn_fakes()
    sink = FakeSink()
    service = InputAuthorizationService()

    release_fact = SimpleNamespace(released=True, context_ready=True)
    hydration_fact = SimpleNamespace(per_response_instructions="每轮上下文")

    first = await run_first_response_turn(
        authorization_service=service,
        final_transcript=final,
        capture_range=source,
        release=_future(release_fact),
        hydration=_future(hydration_fact),
        client=client,
        event_sink=sink,
        response_id="resp-1",
    )
    second = await run_first_response_turn(
        authorization_service=service,
        final_transcript=final,
        capture_range=source,
        release=_future(release_fact),
        hydration=_future(hydration_fact),
        client=client,
        event_sink=sink,
        response_id="resp-1",
    )

    assert first.ok is True
    assert second is first
    assert client.create_calls == [("resp-1", "每轮上下文")]


@pytest.mark.asyncio(loop_scope="function")
async def test_failed_not_sent_turn_can_retry_and_send_once():
    _, source, final, client = make_turn_fakes()
    sink = FakeSink()
    service = InputAuthorizationService()
    hydration_fact = SimpleNamespace(per_response_instructions="每轮上下文")

    async def failing_release() -> object:
        raise RuntimeError("emotion service unavailable")

    first = await run_first_response_turn(
        authorization_service=service,
        final_transcript=final,
        capture_range=source,
        release=failing_release(),
        hydration=_future(hydration_fact),
        client=client,
        event_sink=sink,
        response_id="resp-1",
    )

    assert first.ok is False
    assert first.response_send_status == "not_sent"
    assert client.create_calls == []

    retry = await run_first_response_turn(
        authorization_service=service,
        final_transcript=final,
        capture_range=source,
        release=_future(SimpleNamespace(released=True, context_ready=True)),
        hydration=_future(hydration_fact),
        client=client,
        event_sink=sink,
        response_id="resp-1",
    )

    assert retry.ok is True
    assert client.create_calls == [("resp-1", "每轮上下文")]


@pytest.mark.asyncio(loop_scope="function")
async def test_send_unknown_is_terminal_and_never_resent():
    _, source, final, client = make_turn_fakes()
    sink = FakeSink()
    service = InputAuthorizationService()
    hydration_fact = SimpleNamespace(per_response_instructions="每轮上下文")
    release_fact = SimpleNamespace(released=True, context_ready=True)

    async def failing_create(_response_id, _instructions) -> None:
        raise OSError("connection reset while sending")

    client.create_first_response = failing_create

    first = await run_first_response_turn(
        authorization_service=service,
        final_transcript=final,
        capture_range=source,
        release=_future(release_fact),
        hydration=_future(hydration_fact),
        client=client,
        event_sink=sink,
        response_id="resp-1",
    )

    assert first.ok is False
    assert first.error is not None
    assert first.error.stage == "response_send"
    assert first.response_send_status == "send_unknown"

    retry = await run_first_response_turn(
        authorization_service=service,
        final_transcript=final,
        capture_range=source,
        release=_future(release_fact),
        hydration=_future(hydration_fact),
        client=client,
        event_sink=sink,
        response_id="resp-1",
    )

    assert retry is first
    assert retry.response_send_status == "send_unknown"


@pytest.mark.asyncio(loop_scope="function")
async def test_runner_rejects_release_without_context_ready_fact():
    _, source, final, client = make_turn_fakes()
    sink = FakeSink()

    async def release() -> object:
        return SimpleNamespace(released=True)

    async def hydration() -> object:
        return SimpleNamespace(per_response_instructions="每轮上下文")

    result = await run_first_response_turn(
        authorization_service=InputAuthorizationService(),
        final_transcript=final,
        capture_range=source,
        release=release(),
        hydration=hydration(),
        client=client,
        event_sink=sink,
        response_id="resp-1",
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.stage == "release"
    assert client.create_calls == []


@pytest.mark.asyncio(loop_scope="function")
async def test_runner_rejects_release_missing_field_fail_closed():
    _, source, final, client = make_turn_fakes()
    sink = FakeSink()

    async def release() -> object:
        return SimpleNamespace(context_ready=True)

    async def hydration() -> object:
        return SimpleNamespace(per_response_instructions="每轮上下文")

    result = await run_first_response_turn(
        authorization_service=InputAuthorizationService(),
        final_transcript=final,
        capture_range=source,
        release=release(),
        hydration=hydration(),
        client=client,
        event_sink=sink,
        response_id="resp-1",
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.stage == "release"
    assert client.create_calls == []


@pytest.mark.asyncio(loop_scope="function")
async def test_runner_rejects_final_not_bound_to_authorized_item():
    _, source, final, client = make_turn_fakes()
    client.committed_item_id = "other-item"
    client.final_event = ProtocolEvent(
        type="input_transcript_final",
        item_id="other-item",
        transcript="请继续",
        is_final=True,
    )
    sink = FakeSink()

    async def release() -> object:
        return SimpleNamespace(released=True, context_ready=True)

    async def hydration() -> object:
        return SimpleNamespace(per_response_instructions="每轮上下文")

    result = await run_first_response_turn(
        authorization_service=InputAuthorizationService(),
        final_transcript=final,
        capture_range=source,
        release=release(),
        hydration=hydration(),
        client=client,
        event_sink=sink,
        response_id="resp-1",
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.stage == "final_transcript"
    assert client.create_calls == []


@pytest.mark.asyncio(loop_scope="function")
async def test_hydration_failure_returns_structured_error_without_response():
    _, source, final, client = make_turn_fakes()
    sink = FakeSink()

    async def release() -> object:
        return SimpleNamespace(released=True, context_ready=True)

    async def hydration() -> object:
        raise RuntimeError("database unavailable")

    result = await run_first_response_turn(
        authorization_service=InputAuthorizationService(),
        final_transcript=final,
        capture_range=source,
        release=release(),
        hydration=hydration(),
        client=client,
        event_sink=sink,
        response_id="resp-1",
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.stage == "hydration"
    assert result.error.error_type == "RuntimeError"
    assert result.error.message == "database unavailable"
    assert result.gate.closed is True
    assert client.create_calls == []
    assert [event.event_type for event in sink.events] == [
        "voice_commit_sent",
        "voice_input_committed",
    ]

    _, source, final, client = make_turn_fakes()
    sink = FakeSink()

    async def release() -> object:
        return SimpleNamespace(released=True, context_ready=True)

    async def hydration_without_instructions() -> object:
        return SimpleNamespace()

    result = await run_first_response_turn(
        authorization_service=InputAuthorizationService(),
        final_transcript=final,
        capture_range=source,
        release=release(),
        hydration=hydration_without_instructions(),
        client=client,
        event_sink=sink,
        response_id="resp-1",
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.stage == "hydration"
    assert result.error.error_type == "ValueError"
    assert result.error.message == "水合结果缺少可用的 per_response_instructions"
    assert result.gate.closed is True
    assert client.create_calls == []
    assert [event.event_type for event in sink.events] == [
        "voice_commit_sent",
        "voice_input_committed",
    ]
