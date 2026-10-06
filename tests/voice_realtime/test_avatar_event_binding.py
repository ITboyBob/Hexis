from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.behavior_events import BehaviorEvent
from services.voice_emotion_gate import VoiceEmotionCoordinator
from services.voice_avatar_events import build_voice_behavior_event, voice_behavior_envelope


pytestmark = [pytest.mark.asyncio(loop_scope="function")]


def make_identity():
    return {
        "session_id": str(uuid4()),
        "input_turn_id": str(uuid4()),
        "input_order": 4,
        "stimulus_revision": 2,
    }


async def test_gate_publishes_service_owned_open_and_accepted_facts():
    updates = []
    identity = make_identity()

    async def publish(value, fact, label=None):
        updates.append((dict(value), dict(fact), label))

    coordinator = VoiceEmotionCoordinator(
        context_reader=lambda _identity: _resolved({}),
        evaluator=lambda _message, _context: _resolved({"label": "happy"}),
        hydrator=lambda _identity, _message: _resolved(
            SimpleNamespace(per_response_instructions="context", hydrate_ms=1.0)
        ),
        emotion_timeout_ms=1000,
        on_visual_window=publish,
    )

    result = await coordinator.run(identity, "继续")

    assert [fact["status"] for _value, fact, _label in updates] == ["open", "accepted"]
    assert all(value == identity for value, _fact, _label in updates)
    assert all(fact["owner"] == "service" for _value, fact, _label in updates)
    assert updates[-1][2] == "happy"
    assert result.visual_window["status"] == "accepted"


async def test_gate_publishes_closed_fact_on_timeout_and_never_a_late_label():
    updates = []
    identity = make_identity()

    async def publish(value, fact, label=None):
        updates.append((dict(value), dict(fact), label))

    async def evaluator(_message, _context):
        await asyncio.sleep(0.04)
        return {"label": "sad"}

    coordinator = VoiceEmotionCoordinator(
        context_reader=lambda _identity: _resolved({}),
        evaluator=evaluator,
        hydrator=lambda _identity, _message: _resolved(
            SimpleNamespace(per_response_instructions="context", hydrate_ms=1.0)
        ),
        emotion_timeout_ms=1,
        on_visual_window=publish,
    )

    result = await coordinator.run(identity, "超时")
    await asyncio.sleep(0.06)

    assert [fact["status"] for _value, fact, _label in updates] == ["open", "closed"]
    assert updates[-1][2] is None
    assert result.visual_window["status"] == "closed"


async def test_persisted_and_live_event_share_stable_identity_and_stream_sequence():
    session_id = uuid4()
    payload = {"identity": make_identity(), "status": "open"}
    event = build_voice_behavior_event(
        session_id=session_id,
        media_session_id="media-1",
        producer_seq=5,
        event_type="voice_visual_window",
        payload=payload,
        input_turn_id=payload["identity"]["input_turn_id"],
        stimulus_revision=payload["identity"]["stimulus_revision"],
    )
    retry = build_voice_behavior_event(
        session_id=session_id,
        media_session_id="media-1",
        producer_seq=5,
        event_type="voice_visual_window",
        payload=payload,
        input_turn_id=payload["identity"]["input_turn_id"],
        stimulus_revision=payload["identity"]["stimulus_revision"],
    )

    assert isinstance(event, BehaviorEvent)
    assert event.event_id == retry.event_id
    assert event.producer_seq == 5
    assert event.stream_id == "voice-realtime-session"
    assert event.payload == payload
    assert event.session_id == session_id
    envelope = voice_behavior_envelope(event, database_seq=33)
    assert envelope["seq"] == 33
    assert envelope["producer_seq"] == 5
    assert envelope["event_id"] == str(event.event_id)


async def _resolved(value):
    return value
