from __future__ import annotations

import json
import hashlib
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


@pytest.fixture(scope="module", autouse=True)
async def apply_repo_migrations():
    """Keep the survivor clone at 0267 until this test applies 0268 through 0272."""
    yield


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


async def _legacy_chat_digest(connection):
    rows = await connection.fetch(
        "SELECT (to_jsonb(message) - 'content_blocks')::text AS row_json "
        "FROM public.chat_messages AS message ORDER BY message.id"
    )
    digest = hashlib.sha256()
    for row in rows:
        digest.update(row["row_json"].encode("utf-8"))
        digest.update(b"\n")
    return len(rows), digest.hexdigest()


def _migration_digest(rows):
    digest = hashlib.sha256()
    for row in rows:
        digest.update(f"{row['version']}\0{row['checksum'] or ''}\n".encode("utf-8"))
    return digest.hexdigest()


async def test_0268_through_0272_preserve_old_rows_and_add_block_aware_chat_messages(db_pool):
    from core.migrations import apply_pending_migrations

    session_id = uuid4()
    source_message_id = f"bob107-migration:{session_id}:legacy-assistant"
    old_metadata = {
        "action_receipts": [{
            "name": "remember",
            "success": True,
            "result": {"memory_id": str(uuid4())},
        }]
    }
    async with db_pool.acquire() as connection:
        old_columns = await connection.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='chat_messages' AND column_name='content_blocks'"
        )
        assert old_columns == 0
        old_latest_migration = await connection.fetchval(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        )
        assert old_latest_migration == "0267_m5_selections"

        old_message = _json(await connection.fetchval(
            "SELECT append_chat_message($1,'assistant',$2,$3::jsonb,$4,TRUE)",
            session_id,
            "Historical assistant text with a receipt.",
            json.dumps(old_metadata),
            source_message_id,
        ))
        before = await connection.fetchrow(
            "SELECT id,session_id,ordinal,role,content,visible_in_context,source_message_id,metadata "
            "FROM chat_messages WHERE id=$1::uuid",
            old_message["message_id"],
        )
        before_rows_digest = await _legacy_chat_digest(connection)
        before_ledger = await connection.fetch(
            "SELECT version,checksum FROM schema_migrations ORDER BY version"
        )

        await apply_pending_migrations(connection)

        after = await connection.fetchrow(
            "SELECT id,session_id,ordinal,role,content,content_blocks,visible_in_context,source_message_id,metadata "
            "FROM chat_messages WHERE id=$1::uuid",
            old_message["message_id"],
        )
        latest_migration = await connection.fetchval(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        )
        after_rows_digest = await _legacy_chat_digest(connection)
        after_ledger = await connection.fetch(
            "SELECT version,checksum FROM schema_migrations "
            "WHERE version NOT IN ('0268_chat_message_tool_blocks', '0269_agent_chat_tool_projection', "
            "'0270_action_claim_tool_pair_evidence', '0271_m5_work_turn_chat_projection', "
            "'0272_m5_work_conversation_memory') "
            "ORDER BY version"
        )

    assert latest_migration == "0272_m5_work_conversation_memory"
    assert after_rows_digest == before_rows_digest
    assert _migration_digest(after_ledger) == _migration_digest(before_ledger)
    assert str(after["id"]) == str(before["id"])
    assert str(after["session_id"]) == str(before["session_id"])
    assert after["ordinal"] == before["ordinal"]
    assert after["role"] == before["role"]
    assert after["content"] == before["content"]
    assert after["visible_in_context"] is before["visible_in_context"]
    assert after["source_message_id"] == before["source_message_id"]
    assert _json(after["metadata"]) == _json(before["metadata"])
    assert _json(after["content_blocks"]) == []

    async with db_pool.acquire() as connection:
        replay = _json(await connection.fetchval(
            "SELECT append_chat_message($1,'assistant',$2,$3::jsonb,$4,TRUE)",
            session_id,
            "Historical assistant text with a receipt.",
            json.dumps(old_metadata),
            source_message_id,
        ))
        assert replay["message_id"] == str(before["id"])

        with pytest.raises(Exception, match="content_blocks"):
            async with connection.transaction():
                await connection.execute(
                    "INSERT INTO chat_messages(session_id,ordinal,role,content,content_blocks) "
                    "VALUES($1,99,'assistant','bad','{}'::jsonb)",
                    session_id,
                )

        await apply_pending_migrations(connection)
        repeated_latest = await connection.fetchval(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        )
    assert repeated_latest == latest_migration
