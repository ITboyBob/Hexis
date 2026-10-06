from __future__ import annotations

from uuid import uuid4

import pytest

from services.behavior_events import query_behavior_events, record_behavior_event, replay_behavior_events
from services.voice_avatar_events import build_voice_behavior_event, voice_behavior_envelope


pytestmark = [pytest.mark.asyncio(loop_scope="session")]


async def test_avatar_live_envelopes_match_database_pages_and_replay(db_pool):
    session_id = uuid4()
    media_session_id = "avatar-replay-media"
    input_turn_id = str(uuid4())
    identity = {
        "session_id": str(session_id),
        "input_turn_id": input_turn_id,
        "input_order": 3,
        "stimulus_revision": 0,
    }
    facts = [
        ("voice_input_committed", {"identity": identity}),
        (
            "voice_visual_window",
            {
                "identity": identity,
                "wordset_version": "v1",
                "fact": {"status": "open", "owner": "service", "decision_seq": 0},
            },
        ),
        (
            "voice_visual_window",
            {
                "identity": identity,
                "wordset_version": "v1",
                "fact": {"status": "accepted", "owner": "service", "decision_seq": 1},
                "label": "curious",
            },
        ),
        (
            "voice_model_call_started",
            {"identity": identity, "activity_id": "response-1", "response_id": "response-1"},
        ),
        (
            "voice_recall_started",
            {"identity": identity, "activity_id": "call-1", "call_id": "call-1"},
        ),
        (
            "voice_recall_ended",
            {"identity": identity, "activity_id": "call-1", "call_id": "call-1"},
        ),
        (
            "voice_model_call_ended",
            {"identity": identity, "activity_id": "response-1", "response_id": "response-1"},
        ),
    ]

    live_envelopes = []
    for index, (event_type, payload) in enumerate(facts, start=1):
        event = build_voice_behavior_event(
            session_id=session_id,
            media_session_id=media_session_id,
            producer_seq=index + 20,
            event_type=event_type,
            payload=payload,
            input_turn_id=input_turn_id,
            stimulus_revision=0,
            response_id=payload.get("response_id"),
            call_id=payload.get("call_id"),
        )
        receipt = await record_behavior_event(db_pool, event)
        live_envelopes.append(
            voice_behavior_envelope(event, database_seq=receipt["seq"])
        )

    page = await query_behavior_events(
        db_pool, session_id=session_id, stream_id="voice-realtime-session", limit=20
    )
    replay = await replay_behavior_events(
        db_pool, session_id=session_id, stream_id="voice-realtime-session", limit=20
    )
    page_by_id = {event["event_id"]: event for event in page["events"]}
    replay_by_id = {
        event["event_id"]: event
        for stream in replay["streams"]
        for event in stream["events"]
    }

    assert replay["event_count"] == len(live_envelopes)
    assert [item["seq"] for item in live_envelopes] == list(range(1, len(facts) + 1))
    assert [item["producer_seq"] for item in live_envelopes] == list(range(21, 21 + len(facts)))
    for envelope in live_envelopes:
        assert {key: page_by_id[envelope["event_id"]][key] for key in envelope} == envelope
        assert {key: replay_by_id[envelope["event_id"]][key] for key in envelope} == envelope
