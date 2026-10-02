"""BOB-96: subconscious observation de-duplication.

These integration tests pin the end-to-end contract that the subconscious
decider must satisfy: the same contradiction pair, relationship entity, or
consolidation set must not create a new strategic memory on every run, while
the durable contradiction ledger stays authoritative and user decisions are
never silently reopened.

Tests run against the per-module isolated temp database (baseline + forward
migrations) built by ``tests/conftest.py``; every test starts an explicit
transaction and rolls it back. Assertions measure within-fixture deltas, never
the whole-database counts.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.db]

# The shared orchestration gate (task 2.2). One advisory key serializes the
# whole observation batch against user decisions and imports.
ORCH_GATE = "hexis:memory_observation_orchestration"

OBSERVATION_KINDS = ("relationship_change", "consolidation", "contradiction")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _json(value):
    return json.loads(value) if isinstance(value, str) else value


async def _load_age(conn):
    await conn.execute("LOAD 'age';")
    await conn.execute("SET search_path = public, ag_catalog;")


async def _set_config(conn, key: str, value) -> None:
    """Write a unified-config row inside the caller's transaction."""
    await conn.execute(
        """
        INSERT INTO config (key, value, description, updated_at)
        VALUES ($1, $2::jsonb, 'BOB-96 test', CURRENT_TIMESTAMP)
        ON CONFLICT (key) DO UPDATE
        SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
        """,
        key,
        json.dumps(value),
    )


async def _make_memory(
    conn,
    *,
    mtype: str = "semantic",
    content: str | None = None,
    embedding_status: str = "embedded",
    status: str = "active",
    created_at: datetime | None = None,
    metadata: dict | None = None,
    importance: float = 0.5,
    trust_level: float = 0.8,
    vector: float = 0.1,
):
    """Insert a memory with a legal embedding and materialize its AGE node."""
    if content is None:
        content = f"mem-{uuid.uuid4().hex}"
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    is_embedded = embedding_status == "embedded"
    row = await conn.fetchrow(
        """
        INSERT INTO memories (
            type, content, embedding, embedding_status, status,
            created_at, updated_at, valid_from, metadata,
            importance, trust_level, source_attribution
        ) VALUES (
            $1::memory_type, $2,
            array_fill($3::float, ARRAY[embedding_dimension()])::vector,
            $4, $5::memory_status, $6, $6, $6, $7::jsonb, $8, $9,
            '{"kind":"user_testimony","ref":"bob96"}'::jsonb
        ) RETURNING id
        """,
        mtype,
        content,
        (vector if is_embedded else 0.0),
        embedding_status,
        status,
        created_at,
        json.dumps(metadata or {}),
        importance,
        trust_level,
    )
    memory_id = row["id"]
    assert await conn.fetchval("SELECT sync_memory_node($1)", memory_id) is True
    return memory_id


async def _count_strategic(conn, *, kind: str | None = None) -> int:
    if kind is None:
        return await conn.fetchval(
            "SELECT count(*) FROM memories WHERE type = 'strategic'"
        )
    return await conn.fetchval(
        """
        SELECT count(*) FROM memories
        WHERE type = 'strategic'
          AND metadata->'supporting_evidence'->>'kind' = $1
        """,
        kind,
    )


async def _strategic_rows(conn, *, kind: str, entity: str | None = None):
    if entity is None:
        return await conn.fetch(
            """
            SELECT id, content, status, metadata, reinforcement_count
            FROM memories
            WHERE type = 'strategic'
              AND metadata->'supporting_evidence'->>'kind' = $1
            ORDER BY created_at, id
            """,
            kind,
        )
    return await conn.fetch(
        """
        SELECT id, content, status, metadata, reinforcement_count
        FROM memories
        WHERE type = 'strategic'
          AND metadata->'supporting_evidence'->>'kind' = $1
          AND metadata->'supporting_evidence'->>'entity' = $2
        ORDER BY created_at, id
        """,
        kind,
        entity,
    )


async def _age_edges(conn, rel_type: str, where: str = ""):
    """Return (edge_id, properties) for AGE edges of ``rel_type``.

    ``where`` is an optional Cypher predicate fragment placed before RETURN.
    Requires the caller to have run ``_load_age``.
    """
    rows = await conn.fetch(
        f"""
        SELECT eid::text AS eid, props::text AS props
        FROM cypher('memory_graph', $$
            MATCH (a)-[r:{rel_type}]->(b)
            {where}
            RETURN id(r), properties(r)
        $$) AS (eid agtype, props agtype)
        """
    )
    return [
        (int(row["eid"]), json.loads(row["props"] or "{}"))
        for row in rows
    ]


async def _age_edge_count(conn, rel_type: str, a_id, b_id, *, directed: bool = True) -> int:
    arrow = "->" if directed else "-"
    value = await conn.fetchval(
        f"""
        SELECT c::text
        FROM cypher('memory_graph', $$
            MATCH (a:MemoryNode {{memory_id: '{a_id}'}})-[r:{rel_type}]{arrow}
                  (b:MemoryNode {{memory_id: '{b_id}'}})
            RETURN count(r)
        $$) AS (c agtype)
        """
    )
    return int(json.loads(value))


async def _rel_edge_count(conn, rel_type: str, a_id, b_id, *, directed: bool = True) -> int:
    if directed:
        return await conn.fetchval(
            """
            SELECT count(*) FROM memory_edges
            WHERE src_type='memory' AND src_id=$1 AND rel_type=$2
              AND dst_type='memory' AND dst_id=$3
            """,
            str(a_id),
            rel_type,
            str(b_id),
        )
    return await conn.fetchval(
        """
        SELECT count(*) FROM memory_edges
        WHERE rel_type=$3 AND src_type='memory' AND dst_type='memory'
          AND ((src_id=$1 AND dst_id=$2) OR (src_id=$2 AND dst_id=$1))
        """,
        str(a_id),
        str(b_id),
        rel_type,
    )


async def _coherence(conn) -> float:
    return await conn.fetchval(
        "SELECT current_level FROM drives WHERE name='coherence'"
    )


# ---------------------------------------------------------------------------
# Task 1.2: real AGE capability probe
# ---------------------------------------------------------------------------
async def _apply(conn, payload: dict) -> dict:
    return _json(
        await conn.fetchval(
            "SELECT apply_subconscious_observations($1::jsonb)",
            json.dumps(payload),
        )
    )


async def _strategic_evidence(conn, kind: str):
    rows = await conn.fetch(
        """
        SELECT id, content, status, metadata, reinforcement_count
        FROM memories
        WHERE type = 'strategic'
          AND metadata->'supporting_evidence'->>'kind' = $1
        """,
        kind,
    )
    return [
        {**dict(r), "evidence": json.loads(r["metadata"])["supporting_evidence"]}
        for r in rows
    ]


async def _relationship_rows(conn, entity: str):
    norm = entity.strip().lower()
    return [
        r
        for r in await _strategic_evidence(conn, "relationship_change")
        if (r["evidence"].get("entity") or "").strip().lower() == norm
    ]


async def _contradiction_rows(conn, a, b):
    pair = {str(a), str(b)}
    return [
        r
        for r in await _strategic_evidence(conn, "contradiction")
        if {str(r["evidence"].get("memory_a")), str(r["evidence"].get("memory_b"))}
        == pair
    ]


async def _consolidation_rows(conn, ids):
    idset = {str(i) for i in ids}
    return [
        r
        for r in await _strategic_evidence(conn, "consolidation")
        if {str(x) for x in (r["evidence"].get("memory_ids") or [])} == idset
    ]


async def _case_count(conn, a, b) -> int:
    return await conn.fetchval(
        """
        SELECT count(*) FROM contradiction_cases
        WHERE LEAST(memory_a, memory_b) = LEAST($1::uuid, $2::uuid)
          AND GREATEST(memory_a, memory_b) = GREATEST($1::uuid, $2::uuid)
        """,
        a,
        b,
    )


# ---------------------------------------------------------------------------
# Task 1.1: failing use-cases
# ---------------------------------------------------------------------------
async def test_relationship_window_and_touch(db_pool):
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            e1 = f"rel-{uuid.uuid4().hex[:8]}"
            await _apply(
                conn,
                {
                    "relationship_observations": [
                        {
                            "entity": e1,
                            "change_type": "trust_increase",
                            "strength": 0.8,
                            "confidence": 0.9,
                            "summary": "first",
                        }
                    ]
                },
            )
            await _apply(
                conn,
                {
                    "relationship_observations": [
                        {
                            "entity": e1,
                            "change_type": "trust_increase",
                            "strength": 0.85,
                            "confidence": 0.9,
                            "summary": "second",
                        }
                    ]
                },
            )
            e1_rows = await _relationship_rows(conn, e1)
            assert len(e1_rows) == 1, e1_rows
            assert e1_rows[0]["reinforcement_count"] == 1

            # Case/whitespace variant within the window matches the same record.
            e2 = f"Rel-Variant-{uuid.uuid4().hex[:6]}"
            await _apply(
                conn,
                {
                    "relationship_observations": [
                        {"entity": e2, "change_type": "note", "confidence": 0.9}
                    ]
                },
            )
            before = await _relationship_rows(conn, e2.lower())
            await _apply(
                conn,
                {
                    "relationship_observations": [
                        {
                            "entity": f"  {e2.lower()}  ",
                            "change_type": "note",
                            "confidence": 0.9,
                        }
                    ]
                },
            )
            after = await _relationship_rows(conn, e2)
            assert len(after) == len(before) == 1
            assert after[0]["reinforcement_count"] == 1

            # A record older than the one-hour window does not suppress a new one.
            e3 = f"rel-old-{uuid.uuid4().hex[:8]}"
            await conn.execute(
                """
                INSERT INTO memories (type, content, embedding, embedding_status, status,
                                      created_at, updated_at, valid_from, metadata,
                                      importance, trust_level, source_attribution)
                VALUES ('strategic', 'historical relationship', 
                        array_fill(0.1::float, ARRAY[embedding_dimension()])::vector,
                        'embedded', 'active',
                        CURRENT_TIMESTAMP - INTERVAL '61 minutes',
                        CURRENT_TIMESTAMP - INTERVAL '61 minutes',
                        CURRENT_TIMESTAMP - INTERVAL '61 minutes',
                        jsonb_build_object('supporting_evidence',
                            jsonb_build_object('kind', 'relationship_change', 'entity', $1)),
                        0.5, 0.7, '{}'::jsonb)
                """,
                e3,
            )
            await _apply(
                conn,
                {
                    "relationship_observations": [
                        {"entity": e3, "change_type": "note", "confidence": 0.9}
                    ]
                },
            )
            assert len(await _relationship_rows(conn, e3)) == 2
        finally:
            await transaction.rollback()


async def test_consolidation_set_and_touch(db_pool):
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            a = await _make_memory(conn)
            b = await _make_memory(conn)
            c = await _make_memory(conn)

            first = await _apply(
                conn,
                {
                    "consolidation_observations": [
                        {
                            "memory_ids": [str(a), str(b)],
                            "rationale": "related",
                            "confidence": 0.8,
                        }
                    ]
                },
            )
            second = await _apply(
                conn,
                {
                    "consolidation_observations": [
                        {
                            "memory_ids": [str(b), str(a)],
                            "rationale": "related",
                            "confidence": 0.8,
                        }
                    ]
                },
            )
            assert second["consolidation"] == 0, second
            rows = await _consolidation_rows(conn, [a, b])
            assert len(rows) == 1, rows
            assert rows[0]["reinforcement_count"] == 1

            # Duplicate element and case variance still normalize to one set.
            await _apply(
                conn,
                {
                    "consolidation_observations": [
                        {
                            "memory_ids": [str(a).upper(), str(b), str(b)],
                            "rationale": "related",
                            "confidence": 0.8,
                        }
                    ]
                },
            )
            assert len(await _consolidation_rows(conn, [a, b])) == 1

            # A different set is a different suggestion.
            await _apply(
                conn,
                {
                    "consolidation_observations": [
                        {
                            "memory_ids": [str(a), str(c)],
                            "rationale": "related",
                            "confidence": 0.8,
                        }
                    ]
                },
            )
            assert len(await _consolidation_rows(conn, [a, c])) == 1

            # A single valid id never forms a consolidation signature.
            await _apply(
                conn,
                {
                    "consolidation_observations": [
                        {"memory_ids": [str(a)], "rationale": "solo", "confidence": 0.8}
                    ]
                },
            )
            assert first["consolidation"] == 1
        finally:
            await transaction.rollback()


async def test_contradiction_repeat_side_effects(db_pool):
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            await conn.execute(
                "UPDATE drives SET current_level = 0.1 WHERE name='coherence'"
            )
            a = await _make_memory(conn, content="contra a")
            b = await _make_memory(conn, content="contra b")
            w = await _make_memory(
                conn,
                mtype="worldview",
                content="worldview stability probe",
                metadata={"stability": 0.7, "category": "belief", "confidence": 0.7},
            )
            payload = {
                "contradiction_observations": [
                    {
                        "memory_a": str(a),
                        "memory_b": str(b),
                        "worldview_id": str(w),
                        "tension": "a and b disagree",
                        "confidence": 0.8,
                    }
                ]
            }

            before_drive = await _coherence(conn)
            applied1 = await _apply(conn, payload)
            after_first = await _coherence(conn)
            stability_1 = await conn.fetchval(
                "SELECT (metadata->>'stability')::float FROM memories WHERE id=$1",
                w,
            )

            assert applied1["contradictions"] == 1
            assert len(await _contradiction_rows(conn, a, b)) == 1
            assert await _case_count(conn, a, b) == 1
            assert after_first - before_drive == pytest.approx(0.15, abs=1e-9)
            assert stability_1 == pytest.approx(0.56, abs=1e-6)

            applied2 = await _apply(conn, payload)
            after_second = await _coherence(conn)
            stability_2 = await conn.fetchval(
                "SELECT (metadata->>'stability')::float FROM memories WHERE id=$1",
                w,
            )
            assert applied2["contradictions"] == 0, applied2
            assert len(await _contradiction_rows(conn, a, b)) == 1
            assert await _case_count(conn, a, b) == 1
            assert after_second == pytest.approx(after_first, abs=1e-9)
            assert stability_2 == pytest.approx(stability_1, abs=1e-6)

            # Trigger re-entry on its own must not create a case or raise drive.
            before_trigger = await _coherence(conn)
            await conn.fetchval(
                """
                SELECT create_strategic_memory(
                    'duplicate trigger re-entry',
                    'Unresolved tension between beliefs',
                    0.8,
                    jsonb_build_object('kind','contradiction','memory_a',$1::uuid,
                                       'memory_b',$2::uuid,'tension','x','confidence',0.8)
                )
                """,
                a,
                b,
            )
            assert await _case_count(conn, a, b) == 1
            assert await _coherence(conn) == pytest.approx(before_trigger, abs=1e-9)
        finally:
            await transaction.rollback()


async def test_episodic_case_and_queue_boundary(db_pool):
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            ep1 = await _make_memory(conn, mtype="episodic", content="episodic one")
            ep2 = await _make_memory(conn, mtype="episodic", content="episodic two")
            sem = await _make_memory(conn, content="semantic one")

            await _apply(
                conn,
                {
                    "contradiction_observations": [
                        {
                            "memory_a": str(ep1),
                            "memory_b": str(ep2),
                            "tension": "episodic conflict",
                            "confidence": 0.8,
                        }
                    ]
                },
            )
            assert await _case_count(conn, ep1, ep2) == 1

            await _apply(
                conn,
                {
                    "contradiction_observations": [
                        {
                            "memory_a": str(ep1),
                            "memory_b": str(sem),
                            "tension": "episodic-semantic conflict",
                            "confidence": 0.8,
                        }
                    ]
                },
            )
            assert await _case_count(conn, ep1, sem) == 1

            queued = await conn.fetchval(
                "SELECT count(*) FROM contradiction_detection_queue WHERE memory_id = ANY($1::uuid[])",
                [ep1, ep2],
            )
            assert queued == 0
        finally:
            await transaction.rollback()


async def test_contradiction_threshold_and_invalid_inputs(db_pool):
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            await conn.execute(
                "SELECT set_config('contradictions.minimum_confidence','0.78'::jsonb)"
            )
            a = await _make_memory(conn)
            b = await _make_memory(conn)
            before_drive = await _coherence(conn)

            below = _json(
                await conn.fetchval(
                    "SELECT file_contradiction_case($1,$2,NULL,'x',0.77,'test','{}'::jsonb)",
                    a,
                    b,
                )
            )
            above = _json(
                await conn.fetchval(
                    "SELECT file_contradiction_case($1,$2,NULL,'x',0.78,'test','{}'::jsonb)",
                    a,
                    b,
                )
            )
            assert below == {"created": False, "reason": "below_threshold", "confidence": 0.77}
            assert above["created"] is True
            assert await _case_count(conn, a, b) == 1

            # A missing confidence reaches the ledger as the caller's default,
            # not a fabricated pass. Direct call with 0.7 is rejected.
            c = await _make_memory(conn)
            d = await _make_memory(conn)
            low = _json(
                await conn.fetchval(
                    "SELECT file_contradiction_case($1,$2,NULL,'y',0.7,'test','{}'::jsonb)",
                    c,
                    d,
                )
            )
            assert low["created"] is False
            assert low["reason"] == "below_threshold"
            assert await _case_count(conn, c, d) == 0

            # Invalid / same / missing memory ids are inert.
            invalid = _json(
                await conn.fetchval(
                    "SELECT file_contradiction_case($1,$1,NULL,'self',0.9,'test','{}'::jsonb)",
                    a,
                )
            )
            assert invalid["created"] is False
            assert invalid["reason"] == "invalid_pair"
            ghost = _json(
                await conn.fetchval(
                    "SELECT file_contradiction_case($1,$2,NULL,'ghost',0.9,'test','{}'::jsonb)",
                    uuid.uuid4(),
                    b,
                )
            )
            assert ghost["created"] is False
            assert ghost["reason"] == "memory_not_found"
            assert await _coherence(conn) == pytest.approx(before_drive + 0.15, abs=1e-9)
        finally:
            await transaction.rollback()


async def test_contradiction_observation_no_side_effects_for_rejected(db_pool):
    """Phase 3 gate: a rejected observation must not write a strategic row."""
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            await conn.execute(
                "SELECT set_config('contradictions.minimum_confidence','0.78'::jsonb)"
            )
            a = await _make_memory(conn)
            b = await _make_memory(conn)
            before_drive = await _coherence(conn)
            await _apply(
                conn,
                {
                    "contradiction_observations": [
                        {"memory_a": str(a), "memory_b": str(b), "tension": "x", "confidence": 0.77}
                    ]
                },
            )
            assert await _case_count(conn, a, b) == 0
            assert await _contradiction_rows(conn, a, b) == []
            assert await _coherence(conn) == pytest.approx(before_drive, abs=1e-9)
        finally:
            await transaction.rollback()


async def test_terminal_case_not_reopened(db_pool):
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            await _load_age(conn)
            a = await _make_memory(conn, content="terminal a")
            b = await _make_memory(conn, content="terminal b")
            payload = {
                "contradiction_observations": [
                    {
                        "memory_a": str(a),
                        "memory_b": str(b),
                        "tension": "terminal conflict",
                        "confidence": 0.8,
                    }
                ]
            }
            await _apply(conn, payload)
            case_id = await conn.fetchval(
                """
                SELECT id FROM contradiction_cases
                WHERE LEAST(memory_a,memory_b)=LEAST($1::uuid,$2::uuid)
                  AND GREATEST(memory_a,memory_b)=GREATEST($1::uuid,$2::uuid)
                """,
                a,
                b,
            )
            decided = _json(
                await conn.fetchval(
                    "SELECT decide_contradiction($1::uuid,'tension',NULL,'web','operator')",
                    case_id,
                )
            )
            assert decided["status"] == "tension"

            drive_after_decision = await _coherence(conn)
            # Direct ledger/idempotent entry is phase 2: a decided pair is never
            # reopened and never adds drive or a second case.
            refiled = _json(
                await conn.fetchval(
                    "SELECT file_contradiction_case($1,$2,NULL,'terminal conflict',0.8,'subconscious','{}'::jsonb)",
                    a,
                    b,
                )
            )
            assert refiled["created"] is False
            assert refiled.get("reason") == "already_decided"
            assert await _case_count(conn, a, b) == 1
            assert await _coherence(conn) == pytest.approx(drive_after_decision, abs=1e-9)
            assert len(await _contradiction_rows(conn, a, b)) == 1

            # Reverse-order re-report is the same unordered pair.
            refiled_reverse = _json(
                await conn.fetchval(
                    "SELECT file_contradiction_case($1,$2,NULL,'terminal conflict reversed',0.8,'subconscious','{}'::jsonb)",
                    b,
                    a,
                )
            )
            assert refiled_reverse["created"] is False
            assert await _case_count(conn, a, b) == 1

            found = await conn.fetch("SELECT * FROM find_contradictions()")
            pair = {str(a), str(b)}
            assert not any({str(r["memory_a"]), str(r["memory_b"])} == pair for r in found)

            heartbeat = _json(
                await conn.fetchval(
                    """
                    SELECT execute_heartbeat_action(
                        gen_random_uuid(), 'resolve_contradiction',
                        jsonb_build_object('memory_a',$1::uuid,'memory_b',$2::uuid)
                    )
                    """,
                    a,
                    b,
                )
            )
            assert heartbeat["success"] is True
            assert heartbeat["result"]["decision_required"] is False
            assert heartbeat["result"]["changed_memories"] is False
        finally:
            await transaction.rollback()


async def test_edge_merge_and_properties(db_pool):
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            await _load_age(conn)
            a = await _make_memory(conn)
            b = await _make_memory(conn)

            await conn.execute(
                "SELECT create_memory_relationship($1,$2,'ASSOCIATED',"
                "'{\"source\":\"first\",\"strength\":0.4}'::jsonb)",
                a,
                b,
            )
            await conn.execute(
                "SELECT create_memory_relationship($1,$2,'ASSOCIATED',"
                "'{\"rationale\":\"second\",\"strength\":0.9}'::jsonb)",
                a,
                b,
            )
            assert await _age_edge_count(conn, "ASSOCIATED", a, b) == 1
            assert await _rel_edge_count(conn, "ASSOCIATED", a, b) == 1
            _, props = (
                await _age_edges(
                    conn,
                    "ASSOCIATED",
                    f"WHERE a.memory_id = '{a}' AND b.memory_id = '{b}'",
                )
            )[0]
            assert props["source"] == "first"
            assert props["rationale"] == "second"
            assert props["strength"] == 0.9

            # Empty properties must not erase what is already there.
            await conn.execute(
                "SELECT create_memory_relationship($1,$2,'ASSOCIATED','{}'::jsonb)",
                a,
                b,
            )
            assert await _age_edge_count(conn, "ASSOCIATED", a, b) == 1
            assert await _rel_edge_count(conn, "ASSOCIATED", a, b) == 1
            _, props2 = (
                await _age_edges(
                    conn,
                    "ASSOCIATED",
                    f"WHERE a.memory_id = '{a}' AND b.memory_id = '{b}'",
                )
            )[0]
            assert props2["source"] == "first"
            assert props2["rationale"] == "second"

            # CONTRADICTS is unordered: the reverse direction is the same edge.
            c = await _make_memory(conn)
            d = await _make_memory(conn)
            await conn.execute(
                "SELECT create_memory_relationship($1,$2,'CONTRADICTS','{\"source\":\"x\"}'::jsonb)",
                c,
                d,
            )
            await conn.execute(
                "SELECT create_memory_relationship($1,$2,'CONTRADICTS','{\"source\":\"y\"}'::jsonb)",
                d,
                c,
            )
            assert await _age_edge_count(conn, "CONTRADICTS", c, d, directed=False) == 1
            assert await _rel_edge_count(conn, "CONTRADICTS", c, d, directed=False) == 1
        finally:
            await transaction.rollback()


async def test_concept_edge_and_cluster_regression(db_pool):
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            await _load_age(conn)
            m = await _make_memory(conn)
            concept = f"Concept-{uuid.uuid4().hex[:8]}"
            assert await conn.fetchval(
                "SELECT link_memory_to_concept($1,$2,0.6)", m, concept
            )
            assert await conn.fetchval(
                "SELECT link_memory_to_concept($1,$2,0.9)", m, concept
            )
            inst = await conn.fetchval(
                f"""
                SELECT c::text FROM cypher('memory_graph', $$
                    MATCH (m:MemoryNode {{memory_id: '{m}'}})-[r:INSTANCE_OF]->(c:ConceptNode {{name: '{concept}'}})
                    RETURN count(r)
                $$) AS (c agtype)
                """
            )
            assert int(json.loads(inst)) == 1
            concept_edges = await conn.fetch(
                """
                SELECT count(*) FROM memory_edges
                WHERE src_type='memory' AND src_id=$1
                  AND rel_type='INSTANCE_OF' AND dst_type='concept' AND dst_id=$2
                """,
                str(m),
                concept,
            )
            assert concept_edges[0][0] == 1

            cluster_id = uuid.uuid4()
            assert await conn.fetchval(
                "SELECT link_memory_to_cluster_graph($1,$2,0.5)", m, cluster_id
            )
            assert await conn.fetchval(
                "SELECT link_memory_to_cluster_graph($1,$2,0.8)", m, cluster_id
            )
            member = await conn.fetchval(
                f"""
                SELECT c::text FROM cypher('memory_graph', $$
                    MATCH (m:MemoryNode {{memory_id: '{m}'}})-[r:MEMBER_OF]->(c:ClusterNode {{cluster_id: '{cluster_id}'}})
                    RETURN count(r)
                $$) AS (c agtype)
                """
            )
            assert int(json.loads(member)) == 1
        finally:
            await transaction.rollback()


async def test_file_created_and_drive(db_pool):
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            a = await _make_memory(conn)
            b = await _make_memory(conn)
            before = await _coherence(conn)
            first = _json(
                await conn.fetchval(
                    "SELECT file_contradiction_case($1,$2,NULL,'tension',0.9,'test','{}'::jsonb)",
                    a,
                    b,
                )
            )
            after_first = await _coherence(conn)
            second = _json(
                await conn.fetchval(
                    "SELECT file_contradiction_case($1,$2,NULL,'tension',0.9,'test','{}'::jsonb)",
                    a,
                    b,
                )
            )
            after_second = await _coherence(conn)

            assert first["created"] is True
            assert after_first - before == pytest.approx(0.15, abs=1e-9)
            assert second["created"] is False
            assert second.get("reason") == "existing_case"
            assert after_second == pytest.approx(after_first, abs=1e-9)
            assert await _case_count(conn, a, b) == 1
        finally:
            await transaction.rollback()


async def test_repeat_three_target_kinds(db_pool):
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            a = await _make_memory(conn)
            b = await _make_memory(conn)
            entity = f"three-{uuid.uuid4().hex[:8]}"
            narrative = {
                "type": "chapter_transition",
                "suggested_name": f"Chapter-{uuid.uuid4().hex[:6]}",
                "confidence": 0.8,
            }
            emotional = {"pattern": f"pattern-{uuid.uuid4().hex[:6]}", "confidence": 0.8}
            payload = {
                "relationship_observations": [
                    {"entity": entity, "change_type": "note", "confidence": 0.9}
                ],
                "contradiction_observations": [
                    {
                        "memory_a": str(a),
                        "memory_b": str(b),
                        "tension": "three-kind conflict",
                        "confidence": 0.8,
                    }
                ],
                "consolidation_observations": [
                    {"memory_ids": [str(a), str(b)], "rationale": "three", "confidence": 0.8}
                ],
                "narrative_observations": [narrative],
                "emotional_observations": [emotional],
            }
            first = await _apply(conn, payload)
            assert first["relationships"] == 1
            assert first["contradictions"] == 1
            assert first["consolidation"] == 1
            assert first["narrative"] == 1
            assert first["emotional"] == 1

            second = await _apply(conn, payload)
            assert second["relationships"] == 0, second
            assert second["contradictions"] == 0, second
            assert second["consolidation"] == 0, second
            # Unchanged contracts: narrative/emotional still record every time.
            assert second["narrative"] == 1
            assert second["emotional"] == 1

            assert len(await _relationship_rows(conn, entity)) == 1
            assert len(await _contradiction_rows(conn, a, b)) == 1
            assert len(await _consolidation_rows(conn, [a, b])) == 1
        finally:
            await transaction.rollback()


async def test_age_capability(db_pool):
    """MERGE, per-key SET, id/properties read, and DELETE-by-id must work.

    Task 2/4 build on exactly these primitives; a capability failure must fail
    here rather than silently degrade to a property-less MERGE.
    """
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            await _load_age(conn)
            a = await _make_memory(conn, content="age-cap-a")
            b = await _make_memory(conn, content="age-cap-b")

            # MERGE the edge, then SET two keys on a second pass.
            await conn.execute(
                f"""
                SELECT * FROM cypher('memory_graph', $$
                    MATCH (a:MemoryNode {{memory_id: '{a}'}}),
                          (b:MemoryNode {{memory_id: '{b}'}})
                    MERGE (a)-[r:ASSOCIATED]->(b)
                    SET r.source = 'probe', r.strength = 0.4
                    RETURN r
                $$) AS (r agtype)
                """
            )
            await conn.execute(
                f"""
                SELECT * FROM cypher('memory_graph', $$
                    MATCH (a:MemoryNode {{memory_id: '{a}'}}),
                          (b:MemoryNode {{memory_id: '{b}'}})
                    MERGE (a)-[r:ASSOCIATED]->(b)
                    SET r.rationale = 'second pass', r.strength = 0.9
                    RETURN r
                $$) AS (r agtype)
                """
            )

            edges = await _age_edges(
                conn,
                "ASSOCIATED",
                f"WHERE a.memory_id = '{a}' AND b.memory_id = '{b}'",
            )
            assert len(edges) == 1, edges
            edge_id, props = edges[0]
            assert props["source"] == "probe"
            assert props["rationale"] == "second pass"
            assert props["strength"] == 0.9

            # DELETE by the edge's own id.
            await conn.execute(
                f"""
                SELECT * FROM cypher('memory_graph', $$
                    MATCH (a)-[r:ASSOCIATED]->(b)
                    WHERE id(r) = {edge_id}
                    DELETE r
                $$) AS (r agtype)
                """
            )
            assert await _age_edge_count(conn, "ASSOCIATED", a, b) == 0

            # JSON null / empty value semantics: SET with an empty string keeps
            # the key present; this is the contract task 4 relies on.
            await conn.execute(
                f"""
                SELECT * FROM cypher('memory_graph', $$
                    MATCH (a:MemoryNode {{memory_id: '{a}'}}),
                          (b:MemoryNode {{memory_id: '{b}'}})
                    MERGE (a)-[r:ASSOCIATED]->(b)
                    SET r.empty = ''
                    RETURN r
                $$) AS (r agtype)
                """
            )
            _, props2 = (
                await _age_edges(
                    conn,
                    "ASSOCIATED",
                    f"WHERE a.memory_id = '{a}' AND b.memory_id = '{b}'",
                )
            )[0]
            assert "empty" in props2
        finally:
            await transaction.rollback()


# ---------------------------------------------------------------------------
# Concurrency: two independent connections / transactions
# ---------------------------------------------------------------------------
async def _seed_memories(conn, count: int, *, mtype: str = "semantic"):
    ids = []
    for i in range(count):
        ids.append(await _make_memory(conn, mtype=mtype, content=f"{mtype}-{uuid.uuid4().hex}"))
    return ids


async def _cleanup_memories(conn, ids):
    await conn.execute(
        "DELETE FROM learning_review_items WHERE source_memory_id = ANY($1::uuid[])",
        ids,
    )
    await conn.execute(
        """
        DELETE FROM contradiction_cases
        WHERE memory_a = ANY($1::uuid[]) OR memory_b = ANY($1::uuid[])
        """,
        ids,
    )
    await conn.execute("DELETE FROM memories WHERE id = ANY($1::uuid[])", ids)


async def test_concurrent_observations(db_pool):
    """Simultaneous same-signature re-report must not create two cases/edges."""
    async with db_pool.acquire() as setup:
        a = await _make_memory(setup, content=f"conc-a-{uuid.uuid4().hex}")
        b = await _make_memory(setup, content=f"conc-b-{uuid.uuid4().hex}")
        entity = f"conc-rel-{uuid.uuid4().hex[:8]}"
        await _set_config(setup, "contradictions.minimum_confidence", 0.78)

    async def worker(conn):
        await conn.execute("LOAD 'age';")
        await conn.execute("SET search_path = public, ag_catalog;")
        payload = {
            "relationship_observations": [
                {"entity": entity, "change_type": "note", "confidence": 0.9}
            ],
            "contradiction_observations": [
                {
                    "memory_a": str(a),
                    "memory_b": str(b),
                    "tension": "conc",
                    "confidence": 0.8,
                }
            ],
            "consolidation_observations": [
                {"memory_ids": [str(a), str(b)], "rationale": "conc", "confidence": 0.8}
            ],
        }
        async with conn.transaction():
            return await _apply(conn, payload)

    try:
        c1 = await db_pool.acquire()
        c2 = await db_pool.acquire()
        try:
            r1, r2 = await asyncio.wait_for(
                asyncio.gather(worker(c1), worker(c2)), timeout=30
            )
        finally:
            await db_pool.release(c1)
            await db_pool.release(c2)

        async with db_pool.acquire() as conn:
            await _load_age(conn)
            assert await _case_count(conn, a, b) == 1
            assert len(await _contradiction_rows(conn, a, b)) == 1
            assert len(await _relationship_rows(conn, entity)) == 1
            assert len(await _consolidation_rows(conn, [a, b])) == 1
            # Exactly one side created the pair; the other reported existing.
            assert sorted([r1["contradictions"], r2["contradictions"]]) == [0, 1]
            assert await _age_edge_count(conn, "CONTRADICTS", a, b, directed=False) == 1
            assert await _rel_edge_count(conn, "CONTRADICTS", a, b, directed=False) == 1
    finally:
        async with db_pool.acquire() as conn:
            await _cleanup_memories(conn, [a, b])
            await conn.execute(
                "DELETE FROM memories WHERE type='strategic' AND content IN ('note','conc')"
            )
            await conn.execute("DELETE FROM drives WHERE name='coherence'")


async def test_file_created_and_drive_committed(db_pool):
    """Same-pair file twice across commits: created then existing, drive once."""
    async with db_pool.acquire() as conn:
        a = await _make_memory(conn, content=f"file-a-{uuid.uuid4().hex}")
        b = await _make_memory(conn, content=f"file-b-{uuid.uuid4().hex}")
    try:
        async with db_pool.acquire() as conn:
            before = await _coherence(conn)
            first = _json(
                await conn.fetchval(
                    "SELECT file_contradiction_case($1,$2,NULL,'t','0.9'::float,'test','{}'::jsonb)",
                    a,
                    b,
                )
            )
            after_first = await _coherence(conn)
            second = _json(
                await conn.fetchval(
                    "SELECT file_contradiction_case($1,$2,NULL,'t','0.9'::float,'test','{}'::jsonb)",
                    a,
                    b,
                )
            )
            after_second = await _coherence(conn)
        assert first["created"] is True
        assert second["created"] is False
        assert after_first - before == pytest.approx(0.15, abs=1e-9)
        assert after_second == pytest.approx(after_first, abs=1e-9)
    finally:
        async with db_pool.acquire() as conn:
            await _cleanup_memories(conn, [a, b])
            await conn.execute("DELETE FROM drives WHERE name='coherence'")


async def test_concurrent_case_file_decide(db_pool):
    """file(commit) then decide concurrently on the same new pair is safe."""
    async with db_pool.acquire() as conn:
        a = await _make_memory(conn, content=f"fd-a-{uuid.uuid4().hex}")
        b = await _make_memory(conn, content=f"fd-b-{uuid.uuid4().hex}")
    try:
        async with db_pool.acquire() as conn:
            filed = _json(
                await conn.fetchval(
                    "SELECT file_contradiction_case($1,$2,NULL,'t','0.9'::float,'test','{}'::jsonb)",
                    a,
                    b,
                )
            )
        case_id = filed["case_id"]

        async def decide(conn):
            async with conn.transaction():
                return _json(
                    await conn.fetchval(
                        "SELECT decide_contradiction($1::uuid,'tension',NULL,'web','op')",
                        case_id,
                    )
                )

        async def refile(conn):
            async with conn.transaction():
                return _json(
                    await conn.fetchval(
                        "SELECT file_contradiction_case($1,$2,NULL,'t','0.9'::float,'test','{}'::jsonb)",
                        a,
                        b,
                    )
                )

        c1 = await db_pool.acquire()
        c2 = await db_pool.acquire()
        try:
            results = await asyncio.wait_for(
                asyncio.gather(decide(c1), refile(c2)), timeout=30
            )
        finally:
            await db_pool.release(c1)
            await db_pool.release(c2)
        # Never two cases and never a reopened terminal pair.
        async with db_pool.acquire() as conn:
            assert await _case_count(conn, a, b) == 1
            status = await conn.fetchval(
                "SELECT status FROM contradiction_cases WHERE id=$1::uuid", case_id
            )
            assert status == "tension"
    finally:
        async with db_pool.acquire() as conn:
            await _cleanup_memories(conn, [a, b])
            await conn.execute("DELETE FROM drives WHERE name='coherence'")


async def test_heartbeat_batch_gate(db_pool):
    """Batch containing resolve must serialize with a concurrent decide."""
    async with db_pool.acquire() as conn:
        a = await _make_memory(conn, content=f"hb-a-{uuid.uuid4().hex}")
        b = await _make_memory(conn, content=f"hb-b-{uuid.uuid4().hex}")
        filed = _json(
            await conn.fetchval(
                "SELECT file_contradiction_case($1,$2,NULL,'t','0.9'::float,'test','{}'::jsonb)",
                a,
                b,
            )
        )
    case_id = filed["case_id"]
    try:
        async def batch(conn):
            async with conn.transaction():
                return _json(
                    await conn.fetchval(
                        """
                        SELECT execute_heartbeat_actions_batch(
                            gen_random_uuid(),
                            jsonb_build_array(jsonb_build_object(
                                'action','resolve_contradiction',
                                'params', jsonb_build_object('case_id',$1::uuid)
                            ))
                        )
                        """,
                        case_id,
                    )
                )

        async def decide(conn):
            async with conn.transaction():
                return _json(
                    await conn.fetchval(
                        "SELECT decide_contradiction($1::uuid,'tension',NULL,'web','op')",
                        case_id,
                    )
                )

        c1 = await db_pool.acquire()
        c2 = await db_pool.acquire()
        try:
            await asyncio.wait_for(asyncio.gather(batch(c1), decide(c2)), timeout=30)
        finally:
            await db_pool.release(c1)
            await db_pool.release(c2)
        async with db_pool.acquire() as conn:
            assert await _case_count(conn, a, b) == 1
            assert (
                await conn.fetchval(
                    "SELECT status FROM contradiction_cases WHERE id=$1::uuid", case_id
                )
                == "tension"
            )
    finally:
        async with db_pool.acquire() as conn:
            await _cleanup_memories(conn, [a, b])
            await conn.execute("DELETE FROM drives WHERE name='coherence'")


async def test_embedding_alignment_concurrent_decide(db_pool):
    """An embedding-triggered graph primitive must not invert lock order."""
    async with db_pool.acquire() as conn:
        await _load_age(conn)
        a = await _make_memory(conn, content=f"em-a-{uuid.uuid4().hex}")
        b = await _make_memory(conn, content=f"em-b-{uuid.uuid4().hex}")
        filed = _json(
            await conn.fetchval(
                "SELECT file_contradiction_case($1,$2,NULL,'t','0.9'::float,'test','{}'::jsonb)",
                a,
                b,
            )
        )
    case_id = filed["case_id"]
    try:
        async def touch_embedding(conn):
            async with conn.transaction():
                # Triggers auto_check_worldview_alignment -> create_memory_relationship.
                await conn.execute(
                    """
                    UPDATE memories
                    SET embedding = array_fill(0.2::float, ARRAY[embedding_dimension()])::vector,
                        embedding_status='embedded'
                    WHERE id = $1
                    """,
                    a,
                )
                return True

        async def decide(conn):
            async with conn.transaction():
                return _json(
                    await conn.fetchval(
                        "SELECT decide_contradiction($1::uuid,'tension',NULL,'web','op')",
                        case_id,
                    )
                )

        c1 = await db_pool.acquire()
        c2 = await db_pool.acquire()
        try:
            await asyncio.wait_for(
                asyncio.gather(touch_embedding(c1), decide(c2)), timeout=30
            )
        finally:
            await db_pool.release(c1)
            await db_pool.release(c2)
    finally:
        async with db_pool.acquire() as conn:
            await _cleanup_memories(conn, [a, b])
            await conn.execute("DELETE FROM drives WHERE name='coherence'")


async def test_weekly_correction_concurrent_decide(db_pool):
    """A weekly correction and a same-source decision must not deadlock."""
    async with db_pool.acquire() as conn:
        source = await _make_memory(conn, content=f"wk-src-{uuid.uuid4().hex}")
        other = await _make_memory(conn, content=f"wk-other-{uuid.uuid4().hex}")
        review = _json(
            await conn.fetchval(
                """
                SELECT create_learning_review(
                    CURRENT_TIMESTAMP - INTERVAL '7 days', CURRENT_TIMESTAMP,
                    'week', ARRAY[$1]::uuid[]
                )
                """,
                source,
            )
        )
        item_id = await conn.fetchval(
            "SELECT id FROM learning_review_items WHERE review_id=$1::uuid",
            review["review_id"],
        )
        filed = _json(
            await conn.fetchval(
                "SELECT file_contradiction_case($1,$2,NULL,'t','0.9'::float,'test','{}'::jsonb)",
                source,
                other,
            )
        )
    case_id = filed["case_id"]
    try:
        async def correct(conn):
            async with conn.transaction():
                return _json(
                    await conn.fetchval(
                        """
                        SELECT decide_learning_review_item(
                            $1::uuid, 'correct', 'corrected content', 'web', 'op'
                        )
                        """,
                        item_id,
                    )
                )

        async def decide(conn):
            async with conn.transaction():
                return _json(
                    await conn.fetchval(
                        "SELECT decide_contradiction($1::uuid,'tension',NULL,'web','op')",
                        case_id,
                    )
                )

        c1 = await db_pool.acquire()
        c2 = await db_pool.acquire()
        try:
            await asyncio.wait_for(
                asyncio.gather(correct(c1), decide(c2)), timeout=30
            )
        finally:
            await db_pool.release(c1)
            await db_pool.release(c2)
    finally:
        async with db_pool.acquire() as conn:
            await _cleanup_memories(conn, [source, other])
            await conn.execute("DELETE FROM learning_review_items WHERE review_id=$1", review["review_id"])
            await conn.execute("DELETE FROM learning_reviews WHERE id=$1", review["review_id"])
            await conn.execute("DELETE FROM drives WHERE name='coherence'")


async def test_hmx_import_concurrent_decide(db_pool):
    """The ordinary HMX import entry (Python) serializes with a decision."""
    from core.digest import content_hash_v1
    from core.memory_exchange import build_envelope, import_hmx, resolve_export_sections

    async with db_pool.acquire() as conn:
        a = await _make_memory(conn, content=f"hmx-a-{uuid.uuid4().hex}")
        b = await _make_memory(conn, content=f"hmx-b-{uuid.uuid4().hex}")
        filed = _json(
            await conn.fetchval(
                "SELECT file_contradiction_case($1,$2,NULL,'t','0.9'::float,'test','{}'::jsonb)",
                a,
                b,
            )
        )
    case_id = filed["case_id"]

    plan = resolve_export_sections("telepathy")
    content = f"imported-{uuid.uuid4().hex}"
    envelope = build_envelope(
        intent="telepathy",
        plan=plan,
        instance_id="hexis_source",
        schema_version="0007_hmx_additive_import",
        embedding_model="embeddinggemma:300m",
        embedding_dimension=768,
        lineage_id=str(uuid.uuid4()),
        relationship_edge_types=["SUPPORTS"],
    )
    envelope["sections"]["memories"] = [
        {
            "ref": f"{envelope['export_id']}:1",
            "type": "semantic",
            "status": "active",
            "content": content,
            "content_hash_v1": content_hash_v1(content),
            "importance": 0.7,
            "trust_level": 0.8,
            "metadata": {},
            "provenance": {
                "acquisition_mode": "experienced",
                "origin_instance": "hexis_source",
                "origin_id": "1",
                "import_chain": [],
                "modification_chain": [],
            },
        }
    ]

    try:
        async def do_import(conn):
            return await import_hmx(conn, envelope, strategy="additive")

        async def decide(conn):
            async with conn.transaction():
                return _json(
                    await conn.fetchval(
                        "SELECT decide_contradiction($1::uuid,'tension',NULL,'web','op')",
                        case_id,
                    )
                )

        c1 = await db_pool.acquire()
        c2 = await db_pool.acquire()
        try:
            await asyncio.wait_for(
                asyncio.gather(do_import(c1), decide(c2)), timeout=30
            )
        finally:
            await db_pool.release(c1)
            await db_pool.release(c2)
        async with db_pool.acquire() as conn:
            assert await _case_count(conn, a, b) == 1
            imported = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE content=$1", content
            )
            assert imported == 1
    finally:
        async with db_pool.acquire() as conn:
            await _cleanup_memories(conn, [a, b])
            await conn.execute("DELETE FROM memories WHERE content=$1", content)
            await conn.execute("DELETE FROM drives WHERE name='coherence'")

