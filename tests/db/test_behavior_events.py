from __future__ import annotations

import json
from uuid import uuid4

import asyncpg
import pytest

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


async def _record(conn, event_id, stream_id, session_id, payload, *, producer_seq=0):
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
            assert await conn.fetchval(
                "SELECT count(*) FROM behavior_events WHERE session_id=$1", session_id
            ) == 2
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
                conn, uuid4(), "a", session_id,
                {"type": "control", "action": "reset"}, producer_seq=0,
            )
            await _record(
                conn, uuid4(), "a", session_id,
                {"type": "control", "action": "destroy"}, producer_seq=2,
            )
            await _record(
                conn, uuid4(), "b", session_id,
                {"type": "control", "action": "reset"}, producer_seq=0,
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
            assert await conn.fetchval(
                "SELECT count(*) FROM behavior_events WHERE turn_id IS NOT NULL"
            ) == 0
        finally:
            await tr.rollback()
