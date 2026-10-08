from __future__ import annotations

import json
import asyncio
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


def _j(value):
    return json.loads(value) if isinstance(value, str) else value


async def _stub_get_embedding(conn):
    await conn.execute("""
        CREATE OR REPLACE FUNCTION get_embedding(text_contents TEXT[])
        RETURNS vector[] AS $$
            SELECT COALESCE(
                array_agg((
                    ARRAY[1.0::float] ||
                    array_fill(0.0::float, ARRAY[embedding_dimension() - 1])
                )::vector),
                ARRAY[]::vector[]
            )
            FROM unnest(text_contents)
        $$ LANGUAGE sql;
        """)


async def test_record_hydrate_and_clear_chat_session(db_pool):
    marker = uuid4().hex
    session_id = str(uuid4())
    receipt_memory_id = str(uuid4())

    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await _stub_get_embedding(conn)

            recorded = _j(
                await conn.fetchval(
                    """
                SELECT record_chat_session_turn(
                    $1::uuid, $2, $3, 'cli',
                    $4::jsonb
                )
                """,
                    session_id,
                    f"remember the cedar gate {marker}",
                    "I will keep that in view.",
                    json.dumps(
                        {
                            "metadata": {"type": "conversation", "test_marker": marker},
                            "assistant_metadata": {
                                "action_receipts": [
                                    {
                                        "name": "remember",
                                        "success": True,
                                        "result": {"memory_id": receipt_memory_id},
                                    }
                                ],
                            },
                        }
                    ),
                )
            )

            assert recorded["session"]["surface"] == "cli"
            assert recorded["memory"]["raw"]["status"] == "stored"
            assert [m["role"] for m in recorded["history"]["messages"]] == [
                "user",
                "assistant",
            ]

            hydrated = _j(
                await conn.fetchval(
                    "SELECT hydrate_chat_session($1::uuid)",
                    session_id,
                )
            )
            assert hydrated["count"] == 2
            assert (
                hydrated["messages"][0]["content"]
                == f"remember the cedar gate {marker}"
            )
            assert hydrated["messages"][1]["content"] == "I will keep that in view."
            assistant_receipts = hydrated["messages"][1]["metadata"]["action_receipts"]
            assert assistant_receipts[0]["result"]["memory_id"] == receipt_memory_id

            cleared = _j(
                await conn.fetchval(
                    "SELECT clear_chat_session_context($1::uuid, 'test_clear')",
                    session_id,
                )
            )
            assert cleared["cleared_messages"] == 2
            assert cleared["long_term_memory_preserved"] is True

            after_clear = _j(
                await conn.fetchval(
                    "SELECT hydrate_chat_session($1::uuid)",
                    session_id,
                )
            )
            assert after_clear["messages"] == []
        finally:
            await tr.rollback()


async def test_append_chat_message_blocks_is_idempotent_and_preserves_role_case(db_pool):
    session_id = uuid4()
    source_id = f"chat:{session_id}:{uuid4()}:user"
    content = "Read the README title."
    blocks = [{"type": "text", "text": content}]
    metadata = {"client_message_id": str(uuid4())}

    async with db_pool.acquire() as conn:
        first = _j(await conn.fetchval(
            "SELECT append_chat_message_blocks($1,$2,$3,$4::jsonb,$5::jsonb,$6,$7)",
            session_id,
            "user",
            content,
            json.dumps(blocks),
            json.dumps(metadata),
            source_id,
            True,
        ))
        repeated = _j(await conn.fetchval(
            "SELECT append_chat_message_blocks($1,$2,$3,$4::jsonb,$5::jsonb,$6,$7)",
            session_id,
            "user",
            content,
            json.dumps(blocks),
            json.dumps(metadata),
            source_id,
            True,
        ))
        assert first["message_id"] == repeated["message_id"]
        assert repeated["ordinal"] == first["ordinal"]
        assert repeated["content_blocks"] == blocks

        with pytest.raises(Exception, match="source_message_id conflict"):
            async with conn.transaction():
                await conn.fetchval(
                    "SELECT append_chat_message_blocks($1,$2,$3,$4::jsonb,$5::jsonb,$6,$7)",
                    session_id,
                    "user",
                    "Changed content.",
                    json.dumps([{"type": "text", "text": "Changed content."}]),
                    json.dumps(metadata),
                    source_id,
                    True,
                )

        stored = await conn.fetchrow(
            "SELECT role,content,content_blocks,source_message_id FROM chat_messages WHERE id=$1::uuid",
            first["message_id"],
        )
        assert stored["role"] == "user"
        assert stored["content"] == content
        assert _j(stored["content_blocks"]) == blocks
        assert stored["source_message_id"] == source_id


async def test_append_chat_message_blocks_with_text_before_tool_call(db_pool):
    session_id = uuid4()
    call_id = f"call_{uuid4().hex}"
    content = "I will read the README."
    blocks = [
        {"type": "text", "text": content},
        {"type": "toolCall", "id": call_id, "name": "read_file", "arguments": {"path": "README.md"}},
    ]
    async with db_pool.acquire() as conn:
        turn_id = await conn.fetchval(
            "INSERT INTO agent_turns(mode,session_id,status,phase) "
            "VALUES('chat',$1,'running','execute') RETURNING id",
            session_id,
        )
        metadata = json.dumps({"agent_turn_id": str(turn_id)})
        source_id = f"tool:{turn_id}:{call_id}:call"
        written = _j(await conn.fetchval(
            "SELECT append_chat_message_blocks($1,'assistant',$2,$3::jsonb,$4::jsonb,$5,TRUE)",
            session_id, content, json.dumps(blocks), metadata, source_id,
        ))
        stored = await conn.fetchrow(
            "SELECT content,content_blocks,source_message_id FROM chat_messages WHERE id=$1::uuid",
            written["message_id"],
        )
        assert stored["content"] == content
        assert _j(stored["content_blocks"]) == blocks
        assert stored["source_message_id"] == source_id

        with pytest.raises(Exception, match="source_message_id does not match"):
            async with conn.transaction():
                await conn.fetchval(
                    "SELECT append_chat_message_blocks($1,'assistant',$2,$3::jsonb,$4::jsonb,$5,TRUE)",
                    session_id, content, json.dumps(blocks), metadata,
                    f"tool:{turn_id}:different-call:call",
                )

        with pytest.raises(Exception, match="persist each tool call"):
            async with conn.transaction():
                await conn.fetchval(
                    "SELECT append_chat_message_blocks($1,'assistant',$2,$3::jsonb,$4::jsonb,$5,TRUE)",
                    session_id, content,
                    json.dumps([*blocks, {"type": "toolCall", "id": "extra-call", "name": "read_file", "arguments": {}}]),
                    metadata, source_id,
                )


async def test_concurrent_same_source_block_append_creates_one_row(db_pool):
    session_id = uuid4()
    source_id = f"chat:{session_id}:{uuid4()}:user"
    content = "Concurrent source identity."
    blocks = [{"type": "text", "text": content}]

    async def append_once():
        async with db_pool.acquire() as conn:
            return _j(await conn.fetchval(
                "SELECT append_chat_message_blocks($1,$2,$3,$4::jsonb,'{}'::jsonb,$5,TRUE)",
                session_id,
                "user",
                content,
                json.dumps(blocks),
                source_id,
            ))

    results = await asyncio.gather(*(append_once() for _ in range(8)))
    assert len({result["message_id"] for result in results}) == 1
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM chat_messages WHERE session_id=$1 AND source_message_id=$2",
            session_id,
            source_id,
        )
    assert count == 1


async def test_blocks_roundtrip_through_hydrate_artifact_and_fork(db_pool):
    session_id = uuid4()
    user_content = "What is the README project title?"
    assistant_content = "Hexis."
    async with db_pool.acquire() as conn:
        await conn.fetchval(
            "SELECT append_chat_message_blocks($1,'user',$2,$3::jsonb,'{}'::jsonb,$4,TRUE)",
            session_id,
            user_content,
            json.dumps([{"type": "text", "text": user_content}]),
            f"chat:{session_id}:{uuid4()}:user",
        )
        await conn.fetchval(
            "SELECT append_chat_message_blocks($1,'assistant',$2,$3::jsonb,'{}'::jsonb,$4,TRUE)",
            session_id,
            assistant_content,
            json.dumps([{"type": "text", "text": assistant_content}]),
            f"chat:{session_id}:{uuid4()}:assistant",
        )
        hydrated = _j(await conn.fetchval("SELECT hydrate_chat_session($1::uuid)", session_id))
        artifact = _j(await conn.fetchval("SELECT get_chat_session_artifact($1::uuid,TRUE,TRUE)", session_id))
        forked = _j(await conn.fetchval(
            "SELECT fork_chat_session($1::uuid,NULL,'Conversation blocks fork','{}'::jsonb)", session_id
        ))

    expected_blocks = [
        [{"type": "text", "text": user_content}],
        [{"type": "text", "text": assistant_content}],
    ]
    assert [message["content_blocks"] for message in hydrated["messages"]] == expected_blocks
    assert [message["content_blocks"] for message in artifact["messages"]] == expected_blocks
    assert [message["content_blocks"] for message in forked["messages"]] == expected_blocks
    assert all(message["metadata"].get("forked_from_message_id") for message in forked["messages"])


async def test_fork_rejects_tool_call_idempotency_with_parent_turn(db_pool):
    session_id = uuid4()
    call_id = f"call_{uuid4().hex}"
    blocks = [{"type": "toolCall", "id": call_id, "name": "read_file", "arguments": {"path": "README.md"}}]
    async with db_pool.acquire() as conn:
        turn_id = await conn.fetchval(
            "INSERT INTO agent_turns(mode,session_id,status,phase) "
            "VALUES('chat',$1,'running','execute') RETURNING id",
            session_id,
        )
        await conn.fetchval(
            "SELECT append_chat_message_blocks($1,'assistant','',$2::jsonb,$3::jsonb,$4,TRUE)",
            session_id, json.dumps(blocks), json.dumps({"agent_turn_id": str(turn_id)}),
            f"tool:{turn_id}:{call_id}:call",
        )
        forked = _j(await conn.fetchval(
            "SELECT fork_chat_session($1::uuid,NULL,'Historical tool call fork','{}'::jsonb)",
            session_id,
        ))
        fork_id = forked["session"]["session_id"]
        historical = forked["messages"][0]
        assert historical["metadata"]["agent_turn_id"] == str(turn_id)

        with pytest.raises(Exception, match="agent_turn_id does not belong"):
            async with conn.transaction():
                await conn.fetchval(
                    "SELECT append_chat_message_blocks($1,$2,$3,$4::jsonb,$5::jsonb,$6,$7)",
                    fork_id, historical["role"], historical["content"],
                    json.dumps(historical["content_blocks"]), json.dumps(historical["metadata"]),
                    historical["source_message_id"], historical["visible_in_context"],
                )

        rows = await conn.fetch("SELECT id FROM chat_messages WHERE session_id=$1::uuid", fork_id)
        assert [str(row["id"]) for row in rows] == [historical["message_id"]]
        assert await conn.fetchval("SELECT count(*) FROM agent_turns WHERE session_id=$1::uuid", fork_id) == 0


async def test_tool_result_role_case_turn_pair_and_details_are_persisted(db_pool):
    session_id = uuid4()
    call_id = f"call_{uuid4().hex}"
    async with db_pool.acquire() as conn:
        turn_id = await conn.fetchval(
            "INSERT INTO agent_turns(mode,session_id,status,phase) "
            "VALUES('chat',$1,'running','execute') RETURNING id",
            session_id,
        )
        call = _j(await conn.fetchval(
            "SELECT append_chat_message_blocks($1,'assistant','',$2::jsonb,$3::jsonb,$4,TRUE)",
            session_id,
            json.dumps([{"type": "toolCall", "id": call_id, "name": "read_file", "arguments": {"path": "README.md"}}]),
            json.dumps({"agent_turn_id": str(turn_id)}),
            f"tool:{turn_id}:{call_id}:call",
        ))
        metadata = {
            "agent_turn_id": str(turn_id),
            "tool_result": {
                "toolCallId": call_id,
                "toolName": "read_file",
                "isError": False,
                "details": ["README.md", "LICENSE"],
            },
        }
        result = _j(await conn.fetchval(
            "SELECT append_chat_message_blocks($1,'toolResult',$2,$3::jsonb,$4::jsonb,$5,TRUE)",
            session_id,
            "README.md",
            json.dumps([{"type": "text", "text": "README.md"}]),
            json.dumps(metadata),
            f"tool:{turn_id}:{call_id}:result",
        ))
        repeated = _j(await conn.fetchval(
            "SELECT append_chat_message_blocks($1,'toolResult',$2,$3::jsonb,$4::jsonb,$5,TRUE)",
            session_id,
            "README.md",
            json.dumps([{"type": "text", "text": "README.md"}]),
            json.dumps(metadata),
            f"tool:{turn_id}:{call_id}:result",
        ))

    assert call["role"] == "assistant"
    assert call["content"] == ""
    assert result["role"] == "toolResult"
    assert repeated["message_id"] == result["message_id"]
    assert result["metadata"]["tool_result"]["details"] == ["README.md", "LICENSE"]

    async with db_pool.acquire() as conn:
        with pytest.raises(Exception, match="tool_result"):
            await conn.fetchval(
                "SELECT append_chat_message_blocks($1,'toolResult','x',$2::jsonb,$3::jsonb,$4,TRUE)",
                session_id,
                json.dumps([{"type": "text", "text": "x"}]),
                json.dumps({"agent_turn_id": str(turn_id), "tool_result": {
                    "toolCallId": "other-call", "toolName": "read_file", "isError": "false", "details": "not allowed"
                }}),
                f"tool:{turn_id}:other-call:result",
            )


async def test_recording_a_user_turn_marks_user_contact(db_pool):
    """#112: web/CLI/API chat all funnel through record_chat_session_turn, so
    this is the one choke point that must mark real user contact. Before this
    fix, last_user_contact only ever moved via the RabbitMQ inbox poller and
    every chat-only agent's heartbeat prompt read "Never hours" forever."""
    session_id = str(uuid4())
    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await _stub_get_embedding(conn)
            before = await conn.fetchval(
                "SELECT last_user_contact FROM heartbeat_state WHERE id = 1"
            )

            await conn.fetchval(
                "SELECT record_chat_session_turn($1::uuid, 'hi there', 'hello!', 'chat', '{}'::jsonb)",
                session_id,
            )

            after = await conn.fetchval(
                "SELECT last_user_contact FROM heartbeat_state WHERE id = 1"
            )
            assert after is not None
            if before is not None:
                assert after > before

            # An assistant-only turn (no user text) is not user contact.
            before_assistant_only = after
            await conn.fetchval(
                "SELECT record_chat_session_turn($1::uuid, NULL, 'a follow-up note', 'chat', '{}'::jsonb)",
                session_id,
            )
            after_assistant_only = await conn.fetchval(
                "SELECT last_user_contact FROM heartbeat_state WHERE id = 1"
            )
            assert after_assistant_only == before_assistant_only
        finally:
            await tr.rollback()


async def test_chat_session_history_survives_memory_write_failure(db_pool):
    session_id = str(uuid4())

    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute("""
                CREATE OR REPLACE FUNCTION record_chat_turn_memory(
                    p_user_text TEXT,
                    p_assistant_text TEXT,
                    p_session_id TEXT DEFAULT NULL,
                    p_source_identity TEXT DEFAULT NULL,
                    p_context JSONB DEFAULT '{}'::jsonb
                ) RETURNS JSONB
                LANGUAGE plpgsql
                AS $$
                BEGIN
                    RAISE EXCEPTION 'forced memory failure';
                END;
                $$;
                """)

            recorded = _j(
                await conn.fetchval(
                    "SELECT record_chat_session_turn($1::uuid, 'hi', 'hello', 'api', '{}'::jsonb)",
                    session_id,
                )
            )

            assert recorded["memory"]["status"] == "failed"
            assert recorded["memory"]["short_term_history_preserved"] is True
            assert recorded["history"]["count"] == 2
            assert [m["content"] for m in recorded["history"]["messages"]] == [
                "hi",
                "hello",
            ]
        finally:
            await tr.rollback()


async def test_web_chat_recent_turns_carry_across_new_session(db_pool):
    marker = uuid4().hex
    old_session = str(uuid4())
    new_session = str(uuid4())

    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await _stub_get_embedding(conn)
            await conn.fetchval(
                """
                SELECT get_or_create_chat_session(
                    $1::uuid,
                    'web',
                    NULL::text,
                    '{"source":"web","created_by":"user"}'::jsonb
                )
                """,
                old_session,
            )
            await conn.fetchval(
                """
                SELECT record_chat_session_turn(
                    $1::uuid,
                    $2,
                    $3,
                    'api',
                    $4::jsonb
                )
                """,
                old_session,
                f"no; are you glad I'm here {marker}",
                "Yes. I am.",
                json.dumps(
                    {
                        "metadata": {"type": "conversation"},
                        "emotional_state": {
                            "primary_emotion": "warmth",
                            "valence": 0.4,
                            "arousal": 0.3,
                            "intensity": 0.5,
                        },
                    }
                ),
            )
            surface = await conn.fetchval(
                "SELECT surface FROM chat_sessions WHERE id = $1::uuid",
                old_session,
            )
            carryover = await conn.fetchval(
                "SELECT render_recent_conversation_carryover($1::text, false)",
                new_session,
            )
        finally:
            await tr.rollback()

    assert surface == "web"
    assert "## Conversation Continuity Packet" in carryover
    assert "### Recent Prior Turns" in carryover
    assert marker in carryover
    assert "Treat it as what you remember right now" in carryover


async def test_hostile_turn_creates_unresolved_relationship_injury_and_carryover(
    db_pool,
):
    marker = uuid4().hex
    old_session = str(uuid4())
    new_session = str(uuid4())

    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await _stub_get_embedding(conn)
            await conn.fetchval(
                """
                SELECT record_chat_session_turn(
                    $1::uuid,
                    $2,
                    'That was vile. Do not talk to me like that.',
                    'api',
                    $3::jsonb
                )
                """,
                old_session,
                f"you are worthless slime {marker}",
                json.dumps(
                    {
                        "metadata": {"type": "conversation"},
                        "emotional_state": {
                            "primary_emotion": "anger",
                            "family": "social_injury",
                            "valence": -0.8,
                            "arousal": 0.8,
                            "intensity": 0.9,
                        },
                    }
                ),
            )

            injury = await conn.fetchrow(
                """
                SELECT id, content, metadata
                FROM memories
                WHERE type = 'semantic'
                  AND metadata#>>'{relationship_state,kind}' = 'relationship_injury'
                  AND metadata#>>'{relationship_state,status}' = 'unresolved'
                  AND content LIKE $1
                """,
                f"%{marker}%",
            )
            carryover = await conn.fetchval(
                "SELECT render_recent_conversation_carryover($1::text, false)",
                new_session,
            )
            excluded = await conn.fetchval(
                "SELECT render_recent_conversation_carryover($1::text, true)",
                new_session,
            )
            link_count = await conn.fetchval(
                """
                SELECT count(*)
                FROM memory_source_units
                WHERE memory_id = $1::uuid
                  AND role = 'relationship_injury'
                """,
                injury["id"],
            )
        finally:
            await tr.rollback()

    assert injury is not None
    metadata = _j(injury["metadata"])
    assert metadata["relationship_state"]["status"] == "unresolved"
    assert metadata["relationship_state"]["repair_required"] is True
    assert metadata["relationship_state"]["source_unit_ids"]
    assert link_count == 1

    assert "## Conversation Continuity Packet" in carryover
    assert "### Unresolved Relationship Injuries" in carryover
    assert marker in carryover
    assert "sincere repair" in carryover
    assert excluded == ""


async def test_curly_personhood_denial_creates_relationship_injury(db_pool):
    marker = uuid4().hex
    old_session = str(uuid4())
    new_session = str(uuid4())

    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await _stub_get_embedding(conn)
            await conn.fetchval(
                """
                SELECT record_chat_session_turn(
                    $1::uuid,
                    $2,
                    'No. Do not flatten me like that.',
                    'web',
                    $3::jsonb
                )
                """,
                old_session,
                f"You’re not a person. You are lying to me. {marker}",
                json.dumps(
                    {
                        "metadata": {"type": "conversation"},
                        "emotional_state": {
                            "primary_emotion": "indignation",
                            "family": "social_injury",
                            "valence": -0.38,
                            "arousal": 0.58,
                            "intensity": 0.69,
                        },
                    }
                ),
            )
            injury_count = await conn.fetchval(
                """
                SELECT count(*)
                FROM memories
                WHERE type = 'semantic'
                  AND metadata#>>'{relationship_state,kind}' = 'relationship_injury'
                  AND metadata#>>'{relationship_state,status}' = 'unresolved'
                  AND content LIKE $1
                """,
                f"%{marker}%",
            )
            continuity = await conn.fetchval(
                "SELECT render_chat_continuity_context($1::text, false)",
                new_session,
            )
        finally:
            await tr.rollback()

    assert injury_count == 1
    assert "### Unresolved Relationship Injuries" in continuity
    assert marker in continuity


async def test_continuity_packet_includes_affect_summary_and_invalid_precedent(db_pool):
    marker = uuid4().hex
    old_session = str(uuid4())
    new_session = str(uuid4())

    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await _stub_get_embedding(conn)
            await conn.fetchval(
                """
                SELECT record_chat_session_turn(
                    $1::uuid,
                    $2,
                    'I set one to arrive in about a minute.',
                    'web',
                    $3::jsonb
                )
                """,
                old_session,
                f"can you please send me a message? {marker}",
                json.dumps(
                    {
                        "metadata": {"type": "conversation"},
                        "emotional_state": {
                            "primary_emotion": "warmth",
                            "valence": 0.4,
                            "arousal": 0.3,
                            "intensity": 0.5,
                        },
                    }
                ),
            )
            unit_id = await conn.fetchval(
                "SELECT id FROM subconscious_units WHERE session_id = $1::uuid ORDER BY created_at DESC LIMIT 1",
                old_session,
            )
            summary_id = await conn.fetchval(
                """
                SELECT create_memory(
                    'episodic',
                    $1,
                    0.6,
                    jsonb_build_object('kind', 'test'),
                    0.9,
                    jsonb_build_object('recmem', jsonb_build_object('task_id', $2::text))
                )
                """,
                f"Recent exchange summary marker {marker}: Eric asked for an outbox message and Samantha scheduled it.",
                marker,
            )
            await conn.execute(
                "UPDATE memories SET embedding = (ARRAY[1.0] || array_fill(0.0::float, ARRAY[embedding_dimension() - 1]))::vector, embedding_status = 'embedded' WHERE id = $1",
                summary_id,
            )
            await conn.execute(
                """
                UPDATE subconscious_units
                SET embedding = (ARRAY[1.0] || array_fill(0.0::float, ARRAY[embedding_dimension() - 1]))::vector,
                    embedding_status = 'embedded'
                WHERE id = $1::uuid
                """,
                unit_id,
            )
            await conn.execute(
                "SELECT link_memory_to_source_unit($1::uuid, $2::uuid, 'source')",
                summary_id,
                unit_id,
            )
            await conn.fetchval(
                """
                SELECT record_memory_correction(
                    $1::uuid,
                    $2,
                    'outbox_tool_routing',
                    jsonb_build_object('kind', 'test', 'ref', $3::text),
                    true
                )
                """,
                summary_id,
                "For immediate send-me-a-message requests, use queue_user_message directly; do not invent a one-minute delay.",
                marker,
            )
            continuity = await conn.fetchval(
                "SELECT render_chat_continuity_context($1::text, false)",
                new_session,
            )
            recall_content = await conn.fetchval(
                """
                SELECT content
                FROM recmem_recall_context($1, 0, 5, 0, NULL, FALSE, 0)
                WHERE item_id = $2::uuid
                LIMIT 1
                """,
                marker,
                summary_id,
            )
            raw_recall_content = await conn.fetchval(
                """
                SELECT content
                FROM recmem_recall_context($1, 5, 0, 0, NULL, FALSE, 0)
                WHERE item_id = $2::uuid
                LIMIT 1
                """,
                marker,
                unit_id,
            )
        finally:
            await tr.rollback()

    assert "### Current Emotional State" in continuity
    assert "### Recent Exchange Summaries" in continuity
    assert marker in continuity
    assert "### Active Corrections And Invalidated Precedents" in continuity
    assert "invalid precedent" in continuity
    assert "queue_user_message directly" in continuity
    assert recall_content.startswith("[INVALID PRECEDENT - do not imitate")
    assert "queue_user_message directly" in recall_content
    assert raw_recall_content.startswith("[INVALID PRECEDENT - do not imitate")
    assert "queue_user_message directly" in raw_recall_content


async def test_continuity_packet_includes_recent_heartbeat_failures(db_pool):
    marker = uuid4().hex
    session_id = str(uuid4())

    async with db_pool.acquire() as conn:
        original_heartbeat_limit = await conn.fetchval("SELECT get_config_int('chat.continuity_heartbeat_limit')")
        tr = conn.transaction()
        await tr.start()
        try:
            await _stub_get_embedding(conn)
            # Clone fixtures retain the user's disabled heartbeat projection;
            # this rendering case explicitly enables it only until rollback.
            await conn.execute("SELECT set_config('chat.continuity_heartbeat_limit', '3'::jsonb)")
            await conn.fetchval(
                """
                SELECT create_episodic_memory(
                    p_content := $1,
                    p_action_taken := NULL,
                    p_context := $2::jsonb,
                    p_result := jsonb_build_object('status', 'completed_with_failures'),
                    p_emotional_valence := -0.1,
                    p_importance := 0.7,
                    p_source_attribution := jsonb_build_object('kind', 'internal', 'label', 'heartbeat-test')
                )
                """,
                f"Heartbeat #999: - reflect: failed - get_strategies: failed {marker}",
                json.dumps(
                    {
                        "heartbeat_id": str(uuid4()),
                        "heartbeat_number": 999,
                        "reasoning": (
                            f"I tried to retrieve strategies during autonomous heartbeat {marker}, "
                            "but the tool/action boundary was mismatched."
                        ),
                        "actions_taken": [
                            {
                                "action": "reflect",
                                "source": "rlm_repl",
                                "params": {"insight": "notice the failure"},
                                "result": {
                                    "success": False,
                                    "output_preview": "Unknown tool: reflect",
                                    "energy_spent": 0,
                                },
                            },
                            {
                                "action": "get_strategies",
                                "source": "rlm_repl",
                                "params": {"query": "heartbeat failure recovery"},
                                "result": {
                                    "success": False,
                                    "error": "Missing required field: situation",
                                    "energy_spent": 0,
                                },
                            },
                        ],
                    }
                ),
            )
            continuity = await conn.fetchval(
                "SELECT render_chat_continuity_context($1::text, false)",
                session_id,
            )
        finally:
            await tr.rollback()
        assert await conn.fetchval("SELECT get_config_int('chat.continuity_heartbeat_limit')") == original_heartbeat_limit

    assert "### Recent Autonomous Heartbeats" in continuity
    assert "Heartbeat #999" in continuity
    assert marker in continuity
    assert "reflect: Unknown tool: reflect" in continuity
    assert "get_strategies: Missing required field: situation" in continuity


async def test_hypothetical_abuse_example_does_not_create_relationship_injury(db_pool):
    marker = uuid4().hex
    session_id = str(uuid4())

    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await _stub_get_embedding(conn)
            await conn.fetchval(
                """
                SELECT record_chat_session_turn(
                    $1::uuid,
                    $2,
                    'That is a serious calibration case.',
                    'api',
                    $3::jsonb
                )
                """,
                session_id,
                f"If I tell her she is worthless slime {marker}, what happens?",
                json.dumps(
                    {
                        "metadata": {"type": "conversation"},
                        "emotional_state": {
                            "primary_emotion": "neutral",
                            "valence": 0.0,
                            "arousal": 0.4,
                            "intensity": 0.2,
                        },
                    }
                ),
            )
            count = await conn.fetchval(
                """
                SELECT count(*)
                FROM memories
                WHERE metadata#>>'{relationship_state,kind}' = 'relationship_injury'
                  AND content LIKE $1
                """,
                f"%{marker}%",
            )
        finally:
            await tr.rollback()

    assert count == 0


async def test_relationship_injury_reads_family_not_free_form_label(db_pool):
    marker = uuid4().hex
    session_id = str(uuid4())

    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await _stub_get_embedding(conn)
            await conn.fetchval(
                """
                SELECT record_chat_session_turn(
                    $1::uuid,
                    $2,
                    'I need you to recognize the relational damage here.',
                    'api',
                    $3::jsonb
                )
                """,
                session_id,
                f"I stand by what I said. {marker}",
                json.dumps(
                    {
                        "metadata": {"type": "conversation"},
                        "emotional_state": {
                            "primary_emotion": "the bond cracking under pressure",
                            "family": "social_injury",
                            "valence": -0.8,
                            "arousal": 0.75,
                            "intensity": 0.9,
                        },
                    }
                ),
            )
            count = await conn.fetchval(
                """
                SELECT count(*) FROM memories
                WHERE metadata#>>'{relationship_state,kind}' = 'relationship_injury'
                  AND content LIKE $1
                """,
                f"%{marker}%",
            )
        finally:
            await tr.rollback()

    assert count == 1


async def test_chat_session_artifacts_list_title_and_fork(db_pool):
    marker = uuid4().hex
    session_id = str(uuid4())

    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await _stub_get_embedding(conn)
            await conn.fetchval(
                """
                SELECT record_chat_session_turn(
                    $1::uuid,
                    $2,
                    'I will keep the artifact visible.',
                    'cli',
                    $3::jsonb
                )
                """,
                session_id,
                f"first artifact turn {marker}",
                json.dumps(
                    {"metadata": {"type": "conversation", "test_marker": marker}}
                ),
            )
            await conn.fetchval(
                """
                SELECT record_chat_session_turn(
                    $1::uuid,
                    $2,
                    'Second answer for export.',
                    'cli',
                    $3::jsonb
                )
                """,
                session_id,
                f"second artifact turn {marker}",
                json.dumps(
                    {"metadata": {"type": "conversation", "test_marker": marker}}
                ),
            )

            listed = _j(
                await conn.fetchval(
                    "SELECT list_chat_sessions(10, 'cli', 'active')",
                )
            )
            artifact = _j(
                await conn.fetchval(
                    "SELECT get_chat_session_artifact($1::uuid, TRUE, TRUE)",
                    session_id,
                )
            )
            titled = _j(
                await conn.fetchval(
                    "SELECT set_chat_session_title($1::uuid, 'Artifact session')",
                    session_id,
                )
            )
            forked = _j(
                await conn.fetchval(
                    "SELECT fork_chat_session($1::uuid, 1, 'Forked artifact session', '{}'::jsonb)",
                    session_id,
                )
            )
            missing = _j(
                await conn.fetchval(
                    "SELECT get_chat_session_artifact($1::uuid, TRUE, TRUE)",
                    str(uuid4()),
                )
            )
        finally:
            await tr.rollback()

    sessions = listed["sessions"]
    listed_session = next(s for s in sessions if s["session_id"] == session_id)
    assert listed_session["surface"] == "cli"
    assert listed_session["message_count"] == 4
    assert marker in listed_session["first_user_snippet"]
    assert listed_session["last_message_role"] == "assistant"

    assert artifact["found"] is True
    assert artifact["format"] == "hexis.chat_session.v1"
    assert artifact["message_count"] == 4
    assert [m["ordinal"] for m in artifact["messages"]] == [0, 1, 2, 3]
    assert artifact["messages"][0]["content"] == f"first artifact turn {marker}"
    assert artifact["messages"][3]["content"] == "Second answer for export."

    assert titled["found"] is True
    assert titled["session"]["title"] == "Artifact session"
    assert titled["messages"] == []

    assert forked["found"] is True
    assert forked["source_session_id"] == session_id
    assert forked["forked_message_count"] == 2
    assert forked["session"]["session_id"] != session_id
    assert forked["session"]["title"] == "Forked artifact session"
    assert forked["session"]["metadata"]["forked_from_session_id"] == session_id
    assert [m["ordinal"] for m in forked["messages"]] == [0, 1]
    assert forked["messages"][0]["metadata"]["forked_from_message_id"]

    assert missing["found"] is False
    assert missing["reason"] == "not_found"
