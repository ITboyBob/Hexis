from __future__ import annotations

import asyncio
import json
from uuid import uuid4, uuid5, UUID

import pytest

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


async def _seed_voice_turn(db_pool, *, user_text="authorized voice input"):
    session_id = uuid4()
    input_turn_id = uuid4()
    async with db_pool.acquire() as conn:
        await conn.fetchval(
            "SELECT get_or_create_chat_session($1::uuid, 'web', NULL, '{}'::jsonb)",
            session_id,
        )
        raw_turn = await conn.fetchval(
            "SELECT start_agent_turn('voice', $1, $2::uuid, '{}'::jsonb)",
            user_text,
            session_id,
        )
    return session_id, input_turn_id, UUID(_decode(raw_turn)["turn_id"])


async def _complete(
    db_pool,
    session_id,
    input_turn_id,
    turn_id,
    *,
    assistant_text="confirmed spoken answer",
    events=None,
):
    async with db_pool.acquire() as conn:
        raw = await conn.fetchval(
            """SELECT complete_voice_turn(
                $1::uuid, $2::uuid, 0, 0, $3::uuid, $4, $5, 'ctx-v1',
                'complete', $6::jsonb, $7::jsonb
            )""",
            session_id,
            input_turn_id,
            turn_id,
            "authorized voice input",
            assistant_text,
            json.dumps({"playback_id": "play-1"}),
            json.dumps(events or []),
        )
    return _decode(raw)


async def test_parallel_voice_turn_completion_writes_one_history_pair_and_receipt(db_pool):
    session_id, input_turn_id, turn_id = await _seed_voice_turn(db_pool)
    event_id = uuid5(UUID(int=0), f"voice-turn:{session_id}:{input_turn_id}")
    events = [
        {
            "event_id": str(event_id),
            "event_type": "voice_turn_completed",
            "stream_id": "voice-realtime-session",
            "producer_seq": 1,
            "payload": {"scope": "complete"},
        }
    ]

    results = await asyncio.gather(
        *[
            _complete(
                db_pool,
                session_id,
                input_turn_id,
                turn_id,
                events=events,
            )
            for _ in range(20)
        ]
    )

    assert sum(not result["deduplicated"] for result in results) == 1
    assert sum(result["deduplicated"] for result in results) == 19
    async with db_pool.acquire() as conn:
        messages = await conn.fetch(
            "SELECT role, source_message_id FROM chat_messages WHERE session_id=$1 ORDER BY ordinal",
            session_id,
        )
        receipt_count = await conn.fetchval(
            "SELECT count(*) FROM voice_turn_completions WHERE session_id=$1",
            session_id,
        )
        event_count = await conn.fetchval(
            "SELECT count(*) FROM behavior_events WHERE event_id=$1",
            event_id,
        )
    assert [row["role"] for row in messages] == ["user", "assistant"]
    assert [row["source_message_id"] for row in messages] == [
        f"voice:{session_id}:{input_turn_id}:0:user",
        f"voice:{session_id}:{input_turn_id}:0:assistant",
    ]
    assert receipt_count == 1
    assert event_count == 1


async def test_voice_turn_retry_conflict_and_transaction_rollback(db_pool):
    session_id, input_turn_id, turn_id = await _seed_voice_turn(db_pool)
    await _complete(db_pool, session_id, input_turn_id, turn_id)

    with pytest.raises(Exception, match="fingerprint conflicts"):
        await _complete(
            db_pool,
            session_id,
            input_turn_id,
            turn_id,
            assistant_text="different output",
        )

    other_session, other_input, other_turn = await _seed_voice_turn(db_pool)
    broken_event = [{
        "event_id": "not-a-uuid",
        "event_type": "voice_turn_completed",
        "stream_id": "voice-realtime-session",
        "producer_seq": 1,
        "payload": {},
    }]
    with pytest.raises(Exception):
        await _complete(
            db_pool,
            other_session,
            other_input,
            other_turn,
            events=broken_event,
        )
    async with db_pool.acquire() as conn:
        receipt_count = await conn.fetchval(
            "SELECT count(*) FROM voice_turn_completions WHERE session_id=$1",
            other_session,
        )
        message_count = await conn.fetchval(
            "SELECT count(*) FROM chat_messages WHERE session_id=$1",
            other_session,
        )
        turn_status = await conn.fetchval(
            "SELECT status FROM agent_turns WHERE id=$1", other_turn
        )
    assert receipt_count == 0
    assert message_count == 0
    assert turn_status == "running"
