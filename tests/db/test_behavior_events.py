from __future__ import annotations

import json
from uuid import uuid4

import asyncpg
import pytest

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


async def _record(
    conn,
    event_id,
    stream_id,
    session_id,
    payload,
    *,
    producer_seq=0,
):
    return _json(
        await conn.fetchval(
            """
            SELECT record_behavior_event(
                $1::uuid, 1, 'avatar.input_committed', $2, $3::uuid,
                NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                NULL, 'avatar-preview', 'avatar-preview-ui', $4,
                'avatar-preview', $5::jsonb
            )
            """,
            event_id,
            stream_id,
            session_id,
            producer_seq,
            json.dumps(payload),
        )
    )


async def test_record_behavior_event_is_idempotent_and_allocates_stream_seq(db_pool):
    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            session_id = uuid4()
            event_id = uuid4()
            payload = {
                "type": "input_committed",
                "identity": {
                    "session_id": str(session_id),
                    "input_turn_id": str(uuid4()),
                    "input_order": 0,
                    "stimulus_revision": 0,
                },
            }

            first = await _record(conn, event_id, "avatar", session_id, payload)
            retry = await _record(conn, event_id, "avatar", session_id, payload)
            other = await _record(
                conn,
                uuid4(),
                "avatar",
                session_id,
                {"type": "control", "action": "reset"},
                producer_seq=1,
            )

            assert first == {"event_id": str(event_id), "seq": 1, "deduplicated": False}
            assert retry == {"event_id": str(event_id), "seq": 1, "deduplicated": True}
            assert other["seq"] == 2
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM behavior_events WHERE session_id=$1",
                    session_id,
                )
                == 2
            )
        finally:
            await tr.rollback()


async def test_rejects_conflicting_retry_for_same_event_id(db_pool):
    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            session_id = uuid4()
            event_id = uuid4()
            await _record(
                conn,
                event_id,
                "avatar",
                session_id,
                {"type": "control", "action": "reset"},
            )
            with pytest.raises(asyncpg.RaiseError, match="behavior event id conflict"):
                await _record(
                    conn,
                    event_id,
                    "avatar",
                    session_id,
                    {"type": "control", "action": "destroy"},
                )
        finally:
            await tr.rollback()


async def test_replay_groups_streams_and_reports_sequence_gaps(db_pool):
    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            session_id = uuid4()
            await _record(
                conn,
                uuid4(),
                "a",
                session_id,
                {"type": "control", "action": "reset"},
                producer_seq=0,
            )
            await _record(
                conn,
                uuid4(),
                "a",
                session_id,
                {"type": "control", "action": "destroy"},
                producer_seq=2,
            )
            await _record(
                conn,
                uuid4(),
                "b",
                session_id,
                {"type": "control", "action": "reset"},
                producer_seq=0,
            )

            replay = _json(
                await conn.fetchval(
                    "SELECT replay_behavior_events($1::uuid)", session_id
                )
            )
            streams = {item["stream_id"]: item for item in replay["streams"]}
            assert replay["event_count"] == 3
            assert [item["seq"] for item in streams["a"]["events"]] == [1, 2]
            assert streams["a"]["sequence_gaps"] == []
            assert [item["seq"] for item in streams["b"]["events"]] == [1]
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM behavior_events WHERE turn_id IS NOT NULL"
                )
                == 0
            )
        finally:
            await tr.rollback()


async def test_get_behavior_events_returns_page_contract_and_stream_order(db_pool):
    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            session_id = uuid4()
            for producer_seq in range(3):
                await _record(
                    conn,
                    uuid4(),
                    "a",
                    session_id,
                    {"type": "control", "action": str(producer_seq)},
                    producer_seq=producer_seq,
                )

            page = _json(
                await conn.fetchval(
                    "SELECT get_behavior_events($1::uuid, $2, NULL, NULL, NULL, NULL, NULL, NULL, 2)",
                    session_id,
                    "a",
                )
            )

            assert set(page) == {
                "events",
                "has_more",
                "next_cursor",
                "truncated",
                "filter_omitted_count",
            }
            assert [event["seq"] for event in page["events"]] == [1, 2]
            assert page["has_more"] is True
            assert page["truncated"] is True
            assert page["next_cursor"] == {"a": 2}
            assert page["filter_omitted_count"] == 0
        finally:
            await tr.rollback()


async def test_replay_separates_filter_omissions_from_sequence_gaps(db_pool):
    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            session_id = uuid4()
            other_session_id = uuid4()
            await _record(
                conn,
                uuid4(),
                "a",
                session_id,
                {"type": "control", "action": "one"},
                producer_seq=0,
            )
            await _record(
                conn,
                uuid4(),
                "a",
                other_session_id,
                {"type": "control", "action": "omitted"},
                producer_seq=1,
            )
            await _record(
                conn,
                uuid4(),
                "a",
                session_id,
                {"type": "control", "action": "three"},
                producer_seq=2,
            )

            replay = _json(
                await conn.fetchval(
                    "SELECT replay_behavior_events($1::uuid, $2)", session_id, "a"
                )
            )

            assert replay["filter_omitted_count"] == 1
            assert replay["has_more"] is False
            assert replay["truncated"] is False
            assert replay["next_cursor"] is None
            assert [event["seq"] for event in replay["events"]] == [1, 3]
            assert replay["streams"][0]["sequence_gaps"] == []
        finally:
            await tr.rollback()


async def test_recorded_at_advances_within_transaction(db_pool):
    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            session_id = uuid4()
            await _record(
                conn,
                uuid4(),
                "a",
                session_id,
                {"type": "control", "action": "first"},
            )
            await conn.execute("SELECT pg_sleep(0.002)")
            await _record(
                conn,
                uuid4(),
                "a",
                session_id,
                {"type": "control", "action": "second"},
                producer_seq=1,
            )

            times = await conn.fetch(
                """
                SELECT recorded_at
                FROM behavior_events
                WHERE session_id = $1
                ORDER BY seq
                """,
                session_id,
            )

            assert times[1]["recorded_at"] > times[0]["recorded_at"]
        finally:
            await tr.rollback()
