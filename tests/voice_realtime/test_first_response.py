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
    def __init__(self, final_event: ProtocolEvent):
        self.final_event = final_event
        self.commit_calls = 0
        self.create_calls: list[tuple[str, str]] = []

    async def commit_audio(self) -> None:
        self.commit_calls += 1

    async def wait_for_final(self) -> ProtocolEvent:
        return self.final_event

    async def create_first_response(self, response_id: str, instructions: str) -> None:
        self.create_calls.append((response_id, instructions))


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

    async def release() -> None:
        await asyncio.sleep(0.01)

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


@pytest.mark.asyncio(loop_scope="function")
async def test_hydration_failure_returns_structured_error_without_response():
    _, source, final, client = make_turn_fakes()
    sink = FakeSink()

    async def release() -> None:
        return None

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

    async def release() -> None:
        return None

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
