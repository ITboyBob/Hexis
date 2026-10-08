"""Persisted tool Activity is scoped to the explicitly selected chat session."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import httpx
import pytest

import apps.hexis_api as hexis_api
from services.behavior_events import BehaviorEvent, record_behavior_event

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _event(*, session_id: UUID, stream_id: str, event_type: str, seq: int, call_id: str, payload=None) -> BehaviorEvent:
    return BehaviorEvent(
        event_id=uuid4(),
        schema_version=1,
        event_type=event_type,
        stream_id=stream_id,
        session_id=session_id,
        source="bob108-api-test",
        producer_id=f"{stream_id}-producer",
        producer_seq=seq,
        channel="chat",
        payload=payload or {"tool_name": "recall", "kind": event_type.lower()},
        task_id="task-a" if session_id.hex.endswith("1") else None,
        call_id=call_id,
    )


@pytest.fixture
async def client(db_pool, monkeypatch):
    monkeypatch.setattr(hexis_api, "_pool", db_pool)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=hexis_api.app),
        base_url="http://test",
    ) as value:
        yield value


async def test_tool_activity_requires_a_valid_selected_session(client):
    missing = await client.get("/api/chat/activity")
    malformed = await client.get("/api/chat/activity", params={"session_id": "not-a-uuid"})
    assert missing.status_code == 422
    assert malformed.status_code == 422


async def test_tool_activity_returns_persisted_ids_and_advances_per_stream_cursor(client, db_pool):
    session_a = UUID("10800000-0000-4000-8000-000000000001")
    session_b = UUID("10800000-0000-4000-8000-000000000011")
    stream_a = f"tool-activity:{session_a}"
    events = [
        _event(session_id=session_a, stream_id=stream_a, event_type="TOOL_START", seq=1, call_id="call-a"),
        _event(session_id=session_a, stream_id=stream_a, event_type="TOOL_RESULT", seq=2, call_id="call-a"),
        _event(session_id=session_a, stream_id=stream_a, event_type="TOOL_RESULT", seq=3, call_id="call-accepted"),
        _event(session_id=session_a, stream_id=stream_a, event_type="avatar.input_committed", seq=4, call_id="ignored"),
        _event(session_id=session_b, stream_id=f"tool-activity:{session_b}", event_type="TOOL_START", seq=1, call_id="call-b"),
    ]
    receipts = [await record_behavior_event(db_pool, event) for event in events]

    first = await client.get(
        "/api/chat/activity",
        params={"session_id": str(session_a), "limit": 2},
    )
    assert first.status_code == 200
    first_body = first.json()
    assert [row["event_id"] for row in first_body["events"]] == [
        str(events[0].event_id), str(events[1].event_id)
    ]
    assert [row["call_id"] for row in first_body["events"]] == ["call-a", "call-a"]
    assert [row["activity"]["id"] for row in first_body["events"]] == [
        str(events[0].event_id), str(events[1].event_id)
    ]
    assert first_body["cursor"] == {stream_a: 2}
    assert first_body["has_more"] is True

    second = await client.get(
        "/api/chat/activity",
        params={
            "session_id": str(session_a),
            "cursor": json.dumps(first_body["cursor"]),
        },
    )
    assert second.status_code == 200
    second_body = second.json()
    assert [row["event_id"] for row in second_body["events"]] == [str(events[2].event_id)]
    assert second_body["cursor"] == {stream_a: 4}
    assert second_body["has_more"] is False
    assert all(row["session_id"] == str(session_a) for row in first_body["events"] + second_body["events"])
    assert receipts[2]["deduplicated"] is False


async def test_cursor_replay_never_returns_old_or_foreign_session_tool_events(client, db_pool):
    session_a = uuid4()
    session_b = uuid4()
    stream_a = f"replay:{session_a}"
    event_a = _event(session_id=session_a, stream_id=stream_a, event_type="TOOL_START", seq=1, call_id="same")
    event_b = _event(session_id=session_b, stream_id=f"replay:{session_b}", event_type="TOOL_RESULT", seq=1, call_id="same")
    await record_behavior_event(db_pool, event_a)
    await record_behavior_event(db_pool, event_b)

    response = await client.get(
        "/api/chat/activity",
        params={"session_id": str(session_a), "cursor": json.dumps({stream_a: 1})},
    )
    assert response.status_code == 200
    assert response.json()["events"] == []
    assert response.json()["cursor"] == {stream_a: 1}


@pytest.mark.parametrize("new_stream_prefix", ["activity-0", "activity-b"])
async def test_cursor_discovers_a_new_tool_stream_after_the_first_page(client, db_pool, new_stream_prefix):
    session_id = uuid4()
    stream_a = f"activity-a:{session_id}"
    stream_b = f"{new_stream_prefix}:{session_id}"
    first_event = _event(session_id=session_id, stream_id=stream_a, event_type="TOOL_START", seq=1, call_id="first")
    await record_behavior_event(db_pool, first_event)
    first = await client.get("/api/chat/activity", params={"session_id": str(session_id)})
    assert first.status_code == 200
    first_cursor = first.json()["cursor"]
    assert first_cursor == {stream_a: 1}

    new_stream_event = _event(session_id=session_id, stream_id=stream_b, event_type="TOOL_RESULT", seq=1, call_id="later")
    await record_behavior_event(db_pool, new_stream_event)
    second = await client.get(
        "/api/chat/activity",
        params={"session_id": str(session_id), "cursor": json.dumps(first_cursor)},
    )
    assert second.status_code == 200
    assert [row["event_id"] for row in second.json()["events"]] == [str(new_stream_event.event_id)]
    assert second.json()["cursor"] == {stream_a: 1, stream_b: 1}


async def test_cursor_pages_known_and_new_streams_without_replay_or_session_leaks(client, db_pool):
    session_id = uuid4()
    stream_a = f"page-a:{session_id}"
    stream_b = f"page-b:{session_id}"
    first_event = _event(session_id=session_id, stream_id=stream_a, event_type="TOOL_START", seq=1, call_id="known")
    await record_behavior_event(db_pool, first_event)
    first = await client.get("/api/chat/activity", params={"session_id": str(session_id)})
    assert first.status_code == 200
    first_cursor = first.json()["cursor"]

    known_result = _event(session_id=session_id, stream_id=stream_a, event_type="TOOL_RESULT", seq=2, call_id="known")
    new_start = _event(session_id=session_id, stream_id=stream_b, event_type="TOOL_START", seq=1, call_id="new")
    foreign_event = _event(session_id=uuid4(), stream_id="page-0:foreign", event_type="TOOL_START", seq=1, call_id="foreign")
    for event in [known_result, new_start, foreign_event]:
        await record_behavior_event(db_pool, event)

    second = await client.get(
        "/api/chat/activity",
        params={"session_id": str(session_id), "cursor": json.dumps(first_cursor), "limit": 1},
    )
    assert second.status_code == 200
    assert [row["event_id"] for row in second.json()["events"]] == [str(known_result.event_id)]
    assert second.json()["cursor"] == {stream_a: 2}
    assert second.json()["has_more"] is True

    third = await client.get(
        "/api/chat/activity",
        params={"session_id": str(session_id), "cursor": json.dumps(second.json()["cursor"]), "limit": 1},
    )
    assert third.status_code == 200
    assert [row["event_id"] for row in third.json()["events"]] == [str(new_start.event_id)]
    assert third.json()["events"][0]["activity"]["id"] == str(new_start.event_id)
    assert third.json()["cursor"] == {stream_a: 2, stream_b: 1}
    assert third.json()["has_more"] is False

    replay = await client.get(
        "/api/chat/activity",
        params={"session_id": str(session_id), "cursor": json.dumps(third.json()["cursor"])},
    )
    assert replay.status_code == 200
    assert replay.json()["events"] == []
    assert replay.json()["cursor"] == {stream_a: 2, stream_b: 1}


async def test_accepted_result_is_returned_without_activity_projection_but_advances_cursor(client, db_pool):
    session_id = uuid4()
    stream_id = f"accepted:{session_id}"
    accepted = _event(
        session_id=session_id,
        stream_id=stream_id,
        event_type="TOOL_RESULT",
        seq=1,
        call_id="accepted-call",
        payload={"tool_name": "remember", "success": True, "state": "accepted", "output": {}},
    )
    await record_behavior_event(db_pool, accepted)

    response = await client.get("/api/chat/activity", params={"session_id": str(session_id)})

    assert response.status_code == 200
    body = response.json()
    assert [row["event_id"] for row in body["events"]] == [str(accepted.event_id)]
    assert body["events"][0]["activity"] is None
    assert body["cursor"] == {stream_id: 1}
