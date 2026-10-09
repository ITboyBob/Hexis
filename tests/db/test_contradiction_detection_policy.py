from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.db]

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "db/migrations/0278_contradiction_detection_eight_hour_episodic.sql"
)
_DETECTED_TYPES = ("episodic", "semantic", "worldview")
_INTERVAL_KEY = "contradictions.detection_interval_seconds"


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


@pytest.fixture
async def policy_conn(db_pool):
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            await conn.execute("DELETE FROM contradiction_cases")
            await conn.execute("DELETE FROM contradiction_detection_queue")
            await conn.execute("DELETE FROM state WHERE key='contradiction_detection_state'")
            await conn.execute("DELETE FROM config WHERE key=$1", _INTERVAL_KEY)
            await conn.execute("SELECT set_config('contradictions.enabled', 'true'::jsonb)")
            yield conn
        finally:
            await transaction.rollback()


async def _memory(conn, memory_type, *, status="active", expired=False):
    valid_until = datetime.now(timezone.utc) - timedelta(days=1) if expired else None
    return await conn.fetchval(
        """
        INSERT INTO memories (
            type, content, embedding, embedding_status, status,
            valid_from, valid_until, source_attribution, trust_level
        ) VALUES (
            $1::memory_type, 'The Manning invoice is paid monthly.',
            array_fill(0.1::float, ARRAY[embedding_dimension()])::vector,
            'embedded', $2::memory_status, CURRENT_TIMESTAMP - INTERVAL '2 days', $3,
            '{"kind":"user_testimony","ref":"policy_test"}'::jsonb, 0.9
        ) RETURNING id
        """,
        memory_type,
        status,
        valid_until,
    )


async def _set_last_start(conn, age_seconds):
    await conn.execute(
        """
        SELECT set_state('contradiction_detection_state', jsonb_build_object(
            'last_batch_started_at', CURRENT_TIMESTAMP - make_interval(secs => $1::int),
            'last_batch_size', 7
        ))
        """,
        age_seconds,
    )


async def test_default_interval_is_eight_hours(policy_conn):
    assert await policy_conn.fetchval("SELECT get_config_int($1)", _INTERVAL_KEY) == 28800


@pytest.mark.parametrize(("age_seconds", "due"), [(28799, False), (28800, True)])
@pytest.mark.parametrize("use_fallback", [False, True])
async def test_eight_hour_boundary_and_fallback(policy_conn, age_seconds, due, use_fallback):
    if use_fallback:
        await policy_conn.execute("DELETE FROM config_defaults WHERE key=$1", _INTERVAL_KEY)
    await _memory(policy_conn, "semantic")
    await _set_last_start(policy_conn, age_seconds)
    before = await policy_conn.fetchval(
        "SELECT value FROM state WHERE key='contradiction_detection_state'"
    )
    result = _json(await policy_conn.fetchval("SELECT claim_contradiction_detection_batch()"))
    assert bool(result["items"]) is due
    if not due:
        assert result["reason"] == "not_due"
        assert await policy_conn.fetchval(
            "SELECT value FROM state WHERE key='contradiction_detection_state'"
        ) == before


async def test_explicit_interval_override_is_preserved(policy_conn):
    await policy_conn.execute("SELECT set_config($1, '43200'::jsonb)", _INTERVAL_KEY)
    await _memory(policy_conn, "semantic")
    await _set_last_start(policy_conn, 28800)
    result = _json(await policy_conn.fetchval("SELECT claim_contradiction_detection_batch()"))
    assert result["reason"] == "not_due"
    assert await policy_conn.fetchval("SELECT get_config_int($1)", _INTERVAL_KEY) == 43200


async def test_empty_queue_refreshes_the_due_window(policy_conn):
    await _set_last_start(policy_conn, 28800)
    first = _json(await policy_conn.fetchval("SELECT claim_contradiction_detection_batch()"))
    second = _json(await policy_conn.fetchval("SELECT claim_contradiction_detection_batch()"))
    assert first["reason"] == "empty_queue"
    assert second["reason"] == "not_due"
    assert await policy_conn.fetchval(
        """
        SELECT (value->>'last_batch_started_at')::timestamptz = CURRENT_TIMESTAMP
        FROM state WHERE key='contradiction_detection_state'
        """
    )


@pytest.mark.parametrize("memory_type", [*_DETECTED_TYPES, "procedural", "strategic", "goal"])
async def test_only_detection_types_enter_queue(policy_conn, memory_type):
    memory_id = await _memory(policy_conn, memory_type)
    queued = await policy_conn.fetchval(
        "SELECT count(*) FROM contradiction_detection_queue WHERE memory_id=$1", memory_id
    )
    assert queued == int(memory_type in _DETECTED_TYPES)
    subject_id = await _memory(policy_conn, "semantic")
    candidates = await policy_conn.fetch(
        "SELECT * FROM contradiction_candidate_memories($1, 30)", subject_id
    )
    assert (memory_id in {row["memory_id"] for row in candidates}) is (
        memory_type in _DETECTED_TYPES
    )


@pytest.mark.parametrize("memory_type", _DETECTED_TYPES)
@pytest.mark.parametrize(("status", "expired"), [("archived", False), ("active", True)])
async def test_ineligible_memories_are_not_queued_or_candidates(policy_conn, memory_type, status, expired):
    memory_id = await _memory(policy_conn, memory_type, status=status, expired=expired)
    subject_id = await _memory(policy_conn, "semantic")
    assert await policy_conn.fetchval(
        "SELECT enqueue_contradiction_detection($1)", memory_id
    ) is None
    candidates = await policy_conn.fetch("SELECT * FROM contradiction_candidate_memories($1, 30)", subject_id)
    assert memory_id not in {candidate["memory_id"] for candidate in candidates}


@pytest.mark.parametrize("subject_type", _DETECTED_TYPES)
@pytest.mark.parametrize("candidate_type", _DETECTED_TYPES)
async def test_detection_types_can_be_candidates_and_filed(policy_conn, subject_type, candidate_type):
    subject_id = await _memory(policy_conn, subject_type)
    candidate_id = await _memory(policy_conn, candidate_type)
    candidates = await policy_conn.fetch("SELECT * FROM contradiction_candidate_memories($1, 30)", subject_id)
    assert candidate_id in {candidate["memory_id"] for candidate in candidates}
    result = _json(await policy_conn.fetchval(
        "SELECT file_contradiction_case($1, $2, $1, 'The invoice schedules conflict.', 0.95, 'test')",
        subject_id,
        candidate_id,
    ))
    assert result["created"] is True


@pytest.mark.parametrize("memory_type", _DETECTED_TYPES)
async def test_type_change_enqueues_existing_memory(policy_conn, memory_type):
    memory_id = await _memory(policy_conn, "procedural")
    await policy_conn.execute("UPDATE memories SET type=$2::memory_type WHERE id=$1", memory_id, memory_type)
    assert await policy_conn.fetchval(
        "SELECT count(*) FROM contradiction_detection_queue WHERE memory_id=$1", memory_id
    ) == 1


@pytest.mark.parametrize("memory_type", ["procedural", "strategic", "goal"])
async def test_type_change_out_of_detection_scope_skips_queued_memory(policy_conn, memory_type):
    memory_id = await _memory(policy_conn, "episodic")
    await policy_conn.execute("UPDATE memories SET type=$2::memory_type WHERE id=$1", memory_id, memory_type)
    result = _json(await policy_conn.fetchval("SELECT claim_contradiction_detection_batch(NULL, TRUE)"))
    assert result["reason"] == "empty_queue"
    assert await policy_conn.fetchval(
        "SELECT status FROM contradiction_detection_queue WHERE memory_id=$1", memory_id
    ) == "pending"


async def test_disabled_detection_does_not_queue_new_memories(policy_conn):
    await policy_conn.execute("SELECT set_config('contradictions.enabled', 'false'::jsonb)")
    for memory_type in _DETECTED_TYPES:
        await _memory(policy_conn, memory_type)
    assert await policy_conn.fetchval("SELECT count(*) FROM contradiction_detection_queue") == 0
    result = _json(await policy_conn.fetchval("SELECT claim_contradiction_detection_batch()"))
    assert result["reason"] == "disabled"


async def test_migration_backfills_all_eligible_episodic_without_requeueing(policy_conn):
    migration = _MIGRATION.read_text(encoding="utf-8")
    await policy_conn.execute("ALTER TABLE memories DISABLE TRIGGER trg_queue_memory_contradiction_detection")
    missing_ids = [await _memory(policy_conn, "episodic") for _ in range(25)]
    excluded_ids = [
        await _memory(policy_conn, "episodic", status="archived"),
        await _memory(policy_conn, "episodic", expired=True),
        await _memory(policy_conn, "procedural"),
    ]
    for status in ("pending", "processing", "completed", "failed"):
        memory_id = await _memory(policy_conn, "episodic")
        await policy_conn.execute(
            """
            INSERT INTO contradiction_detection_queue (memory_id, content_hash, status, attempts, result, error)
            SELECT id, encode(digest(content, 'sha256'), 'hex'), $2, 2, '{"preserved":true}'::jsonb, 'kept'
            FROM memories WHERE id=$1
            """,
            memory_id,
            status,
        )
    await policy_conn.execute("UPDATE config_defaults SET value='86400'::jsonb WHERE key=$1", _INTERVAL_KEY)
    await policy_conn.execute("SELECT set_config($1, '43200'::jsonb)", _INTERVAL_KEY)
    await _set_last_start(policy_conn, 12345)
    before = await policy_conn.fetchval("SELECT jsonb_agg(to_jsonb(q) ORDER BY id) FROM contradiction_detection_queue q")
    state_before = await policy_conn.fetchval("SELECT value FROM state WHERE key='contradiction_detection_state'")
    await policy_conn.execute(migration)
    after = await policy_conn.fetchval("SELECT jsonb_agg(to_jsonb(q) ORDER BY id) FROM contradiction_detection_queue q")
    await policy_conn.execute(migration)
    assert await policy_conn.fetchval(
        "SELECT jsonb_agg(to_jsonb(q) ORDER BY id) FROM contradiction_detection_queue q"
    ) == after
    rows = _json(after)
    old_rows = {row["id"]: row for row in _json(before)}
    assert all(row == old_rows[row["id"]] for row in rows if row["id"] in old_rows)
    queued_ids = {row["memory_id"] for row in rows}
    assert {str(memory_id) for memory_id in missing_ids}.issubset(queued_ids)
    assert not {str(memory_id) for memory_id in excluded_ids} & queued_ids
    assert await policy_conn.fetchval("SELECT value FROM config_defaults WHERE key=$1", _INTERVAL_KEY) == "28800"
    assert await policy_conn.fetchval("SELECT get_config_int($1)", _INTERVAL_KEY) == 43200
    assert await policy_conn.fetchval("SELECT value FROM state WHERE key='contradiction_detection_state'") == state_before
