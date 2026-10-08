from __future__ import annotations

import asyncio
import json
from uuid import uuid4, uuid5, UUID

import pytest

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


async def _seed_voice_turn(db_pool, *, user_text="authorized voice input"):
    from services.voice_conversation_history import VoiceConversationHistory

    session_id = uuid4()
    input_turn_id = uuid4()
    async with db_pool.acquire() as conn:
        await conn.fetchval(
            "SELECT get_or_create_chat_session($1::uuid, 'web', NULL, '{}'::jsonb)",
            session_id,
        )
    turn_id = await VoiceConversationHistory(db_pool).start_turn(
        session_id=session_id,
        input_turn_id=input_turn_id,
        input_order=0,
        stimulus_revision=0,
        user_text=user_text,
        context_version="ctx-v1",
    )
    return session_id, input_turn_id, turn_id


async def _save_voice_input(
    db_pool, session_id, input_turn_id, *, input_order=0, revision=0,
    user_text="authorized voice input", context_version="ctx-v1",
):
    source_identity = f"voice:{session_id}:{input_turn_id}:{revision}"
    async with db_pool.acquire() as conn:
        await conn.fetchval(
            "SELECT append_chat_message_blocks($1::uuid,'user',$2,$3::jsonb,$4::jsonb,$5,TRUE)",
            session_id,
            user_text,
            json.dumps([{"type": "text", "text": user_text}]),
            json.dumps({
                "channel": "voice",
                "input_turn_id": str(input_turn_id),
                "input_order": input_order,
                "stimulus_revision": revision,
                "context_version": context_version,
            }),
            f"{source_identity}:user",
        )
    return source_identity


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
    await _save_voice_input(db_pool, other_session, other_input)
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
    assert message_count == 1
    async with db_pool.acquire() as conn:
        input_source = await conn.fetchval(
            "SELECT source_message_id FROM chat_messages WHERE session_id=$1::uuid",
            other_session,
        )
    assert input_source == f"voice:{other_session}:{other_input}:0:user"
    assert turn_status == "running"


async def test_voice_completion_reuses_the_identity_bound_preexisting_user_message(db_pool):
    session_id, input_turn_id, turn_id = await _seed_voice_turn(db_pool)
    source_identity = f"voice:{session_id}:{input_turn_id}:0"
    await _save_voice_input(db_pool, session_id, input_turn_id)
    async with db_pool.acquire() as conn:
        contact_before_completion = await conn.fetchval(
            "SELECT last_user_contact FROM heartbeat_state WHERE id=1"
        )

    result = await _complete(db_pool, session_id, input_turn_id, turn_id)

    assert result["status"] == "completed"
    assert result["deduplicated"] is False
    async with db_pool.acquire() as conn:
        messages = await conn.fetch(
            "SELECT role,content,source_message_id FROM chat_messages "
            "WHERE session_id=$1::uuid ORDER BY ordinal",
            session_id,
        )
        contact_after_completion = await conn.fetchval(
            "SELECT last_user_contact FROM heartbeat_state WHERE id=1"
        )
    assert [row["role"] for row in messages] == ["user", "assistant"]
    assert [row["source_message_id"] for row in messages] == [
        f"{source_identity}:user",
        f"{source_identity}:assistant",
    ]
    assert contact_after_completion == contact_before_completion


async def test_voice_completion_does_not_recreate_a_missing_preexisting_input(db_pool):
    session_id, input_turn_id, _turn_id = await _seed_voice_turn(db_pool)
    source_identity = f"voice:{session_id}:{input_turn_id}:0"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM chat_messages WHERE session_id=$1::uuid AND source_message_id=$2",
            session_id,
            f"{source_identity}:user",
        )

    result = await _complete(db_pool, session_id, input_turn_id, _turn_id)

    assert result["status"] == "conflict"
    assert result["message_kind"] == "user"
    async with db_pool.acquire() as conn:
        message_count = await conn.fetchval(
            "SELECT count(*) FROM chat_messages WHERE session_id=$1::uuid",
            session_id,
        )
        receipt_count = await conn.fetchval(
            "SELECT count(*) FROM voice_turn_completions WHERE session_id=$1::uuid",
            session_id,
        )
    assert message_count == 0
    assert receipt_count == 0


async def test_voice_completion_rejects_a_preexisting_user_from_another_identity(db_pool):
    session_id, input_turn_id, turn_id = await _seed_voice_turn(db_pool)
    source_identity = f"voice:{session_id}:{input_turn_id}:0"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE chat_messages SET metadata=jsonb_set(metadata,'{input_order}','1'::jsonb) "
            "WHERE session_id=$1::uuid AND source_message_id=$2",
            session_id,
            f"{source_identity}:user",
        )

    result = await _complete(db_pool, session_id, input_turn_id, turn_id)

    assert result["status"] == "conflict"
    assert result["source_identity"] == source_identity
    assert result["message_kind"] == "user"
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role,source_message_id FROM chat_messages WHERE session_id=$1::uuid ORDER BY ordinal",
            session_id,
        )
        receipt_count = await conn.fetchval(
            "SELECT count(*) FROM voice_turn_completions WHERE session_id=$1::uuid",
            session_id,
        )
    assert [(row["role"], row["source_message_id"]) for row in rows] == [
        ("user", f"{source_identity}:user")
    ]
    assert receipt_count == 0
