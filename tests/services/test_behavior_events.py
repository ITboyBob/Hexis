from __future__ import annotations

import pytest
from uuid import uuid4

from services.behavior_events import (
    BehaviorEvent,
    query_behavior_events,
    record_behavior_event,
    replay_behavior_events,
)

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


def _event(session_id=None, event_id=None, producer_seq=0):
    return BehaviorEvent(
        event_id=event_id or uuid4(),
        schema_version=1,
        event_type="avatar.input_committed",
        stream_id="avatar",
        session_id=session_id or uuid4(),
        source="avatar-preview",
        producer_id="avatar-preview-ui",
        producer_seq=producer_seq,
        channel="avatar-preview",
        payload={"type": "input_committed"},
    )


async def test_python_service_records_queries_and_replays(db_pool):
    session_id = uuid4()
    event_id = uuid4()
    event = _event(session_id=session_id, event_id=event_id)

    first = await record_behavior_event(db_pool, event)
    retry = await record_behavior_event(db_pool, event)
    page = await query_behavior_events(db_pool, session_id=session_id)
    replay = await replay_behavior_events(db_pool, session_id=session_id)
    rows = page["events"]

    assert first["deduplicated"] is False
    assert retry["deduplicated"] is True
    assert page["has_more"] is False
    assert page["truncated"] is False
    assert page["next_cursor"] is None
    assert rows[0]["event_id"] == str(event_id)
    assert replay["event_count"] == 1
    assert replay["streams"][0]["events"][0]["event_id"] == str(event_id)


async def test_python_service_rejects_invalid_identity(db_pool):
    event = _event()
    object.__setattr__(event, "producer_seq", -1)
    with pytest.raises(ValueError, match="producer_seq must be non-negative"):
        await record_behavior_event(db_pool, event)
