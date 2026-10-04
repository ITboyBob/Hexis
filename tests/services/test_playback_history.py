from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from services.behavior_events import (
    BehaviorEvent,
    query_behavior_events,
    record_behavior_event,
)
import services.playback_history as playback_history_module
from services.playback_history import PlaybackHistory, PlaybackIdentity

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


def _identity(
    *,
    response_id: str = "resp-1",
    playback_id: str = "play-1",
    output_epoch: int = 1,
) -> PlaybackIdentity:
    return PlaybackIdentity(
        session_id=uuid4(),
        response_id=response_id,
        playback_id=playback_id,
        output_epoch=output_epoch,
        media_session_id="media-1",
    )


async def test_records_playback_events_and_aggregates_history(db_pool):
    identity = _identity()
    history = PlaybackHistory(db_pool, identity)

    await history.open(start_ms=100)
    await history.audible(audible_ms=150)
    await history.progress(played_ms=500, interval_ms=100, phase="playing")
    await history.end(scope="partial", played_ms=900)

    result = await history.get()
    assert result == {
        "playback_id": "play-1",
        "response_id": "resp-1",
        "output_epoch": 1,
        "media_session_id": "media-1",
        "scope": "partial",
        "played_ms": 900,
        "event_count": 4,
    }

    events = await query_behavior_events(
        db_pool, session_id=identity.session_id, response_id=identity.response_id
    )
    assert {event["event_type"] for event in events} == {
        "voice_playback_started",
        "voice_first_audible",
        "voice_playback_progress",
        "voice_playback_ended",
    }
    assert all(event["response_id"] == "resp-1" for event in events)
    assert all(event["media_session_id"] == "media-1" for event in events)
    assert all(event["output_epoch"] == 1 for event in events)
    assert all(event["payload"]["playback_id"] == "play-1" for event in events)
    assert [event["producer_seq"] for event in sorted(
        events, key=lambda event: event["producer_seq"]
    )] == [1, 2, 3, 4]


async def test_explicit_producer_seq_does_not_break_internal_increment(db_pool):
    identity = _identity()
    history = PlaybackHistory(db_pool, identity)

    await history.open(start_ms=0, producer_seq=10)
    await history.audible(audible_ms=100, producer_seq=11)
    await history.progress(played_ms=200, interval_ms=100, phase="playing")

    events = await query_behavior_events(
        db_pool,
        session_id=identity.session_id,
        response_id=identity.response_id,
    )
    assert [event["producer_seq"] for event in sorted(
        events, key=lambda event: event["producer_seq"]
    )] == [10, 11, 12]


async def test_invalid_explicit_producer_seq_is_rejected_before_write(db_pool):
    history = PlaybackHistory(db_pool, _identity())

    with pytest.raises(ValueError, match="producer_seq must be a non-negative integer"):
        await history.open(start_ms=0, producer_seq=-1)

    result = await history.get()
    assert result["event_count"] == 0


async def test_get_orders_concurrent_events_by_producer_seq(db_pool, monkeypatch):
    identity = _identity()
    history = PlaybackHistory(db_pool, identity)
    original_record = playback_history_module.record_behavior_event

    async def delayed_interrupt_record(pool, event):
        if event.producer_seq == 2:
            await asyncio.sleep(0.05)
        return await original_record(pool, event)

    monkeypatch.setattr(
        playback_history_module, "record_behavior_event", delayed_interrupt_record
    )
    await asyncio.gather(
        history.end(scope="complete", played_ms=1000, producer_seq=1),
        history.interrupt(reason="user-stop", played_ms=900, producer_seq=2),
    )

    events = await query_behavior_events(
        db_pool,
        session_id=identity.session_id,
        response_id=identity.response_id,
    )
    assert [event["event_type"] for event in events] == [
        "voice_playback_interrupted",
        "voice_playback_ended",
    ]

    result = await history.get()
    assert result["scope"] == "complete"


@pytest.mark.parametrize(
    ("scope", "action"),
    [
        ("complete", "end-complete"),
        ("partial", "end-partial"),
        ("unplayed", "interrupt-zero"),
        ("unknown", "no-terminal"),
    ],
)
async def test_aggregates_only_the_four_playback_scopes(db_pool, scope, action):
    history = PlaybackHistory(db_pool, _identity())
    await history.open(start_ms=0)

    if action == "end-complete":
        await history.end(scope="complete", played_ms=1200)
    elif action == "end-partial":
        await history.end(scope="partial", played_ms=500)
    elif action == "interrupt-zero":
        await history.interrupt(reason="user-stop", played_ms=0)

    result = await history.get()
    assert result["scope"] == scope


async def test_late_receipt_supplements_only_its_original_instance(db_pool):
    identity = _identity()
    history = PlaybackHistory(db_pool, identity)
    await history.open(start_ms=100)
    await history.end(scope="partial", played_ms=900)

    other_playback = PlaybackHistory(db_pool, _identity(playback_id="play-2"))
    await other_playback.open(start_ms=0)
    await other_playback.progress(played_ms=2000, interval_ms=100, phase="playing")
    await other_playback.end(scope="complete", played_ms=2000)

    old_epoch = PlaybackHistory(db_pool, _identity(output_epoch=2))
    await old_epoch.open(start_ms=0)
    await old_epoch.progress(played_ms=3000, interval_ms=100, phase="playing")

    await history.progress(played_ms=1200, interval_ms=100, phase="playing")

    result = await history.get()
    assert result["scope"] == "partial"
    assert result["played_ms"] == 1200
    assert result["event_count"] == 3


async def test_interrupted_playback_with_progress_is_partial(db_pool):
    history = PlaybackHistory(db_pool, _identity())
    await history.open(start_ms=0)
    await history.interrupt(reason="user-stop", played_ms=700)

    result = await history.get()
    assert result["scope"] == "partial"
    assert result["played_ms"] == 700


async def test_get_pages_beyond_the_single_query_limit(db_pool):
    history = PlaybackHistory(db_pool, _identity())
    await history.open(start_ms=0)
    await history.end(scope="partial", played_ms=900)
    for index in range(1000):
        await history.progress(
            played_ms=1000 + index,
            interval_ms=10,
            phase="playing",
        )

    result = await history.get()
    assert result["scope"] == "partial"
    assert result["played_ms"] == 1999
    assert result["event_count"] == 1002


async def test_invalid_scope_is_rejected_before_write(db_pool):
    history = PlaybackHistory(db_pool, _identity())

    with pytest.raises(ValueError, match="scope must be complete"):
        await history.end(scope="invalid", played_ms=100)

    result = await history.get()
    assert result["scope"] == "unknown"
    assert result["event_count"] == 0


async def test_generation_done_does_not_infer_playback_scope(db_pool):
    identity = _identity()
    history = PlaybackHistory(db_pool, identity)
    await history.open(start_ms=0)

    await record_behavior_event(
        db_pool,
        BehaviorEvent(
            event_id=uuid4(),
            schema_version=1,
            event_type="voice_generation_done",
            stream_id="voice-generation",
            session_id=identity.session_id,
            source="voice-generation",
            producer_id="voice-generation-service",
            producer_seq=1,
            channel="voice",
            payload={"done": True},
            response_id=identity.response_id,
            media_session_id=identity.media_session_id,
            output_epoch=identity.output_epoch,
        ),
    )

    result = await history.get()
    assert result["scope"] == "unknown"
    assert result["played_ms"] == 0
    assert result["event_count"] == 1
