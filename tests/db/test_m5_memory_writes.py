from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest


pytestmark = [pytest.mark.asyncio(loop_scope="session")]

_PREP = Path("/Users/itboybob/.cache/hexis/m5-plan-prep-2026-10-07")


async def _stub_get_embedding(conn):
    """Keep the embedding dependency deterministic while exercising real DB writes."""
    await conn.execute(
        """
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
        """
    )


def _bound_call(origin, tool_name, arguments, *, command_id=None, call_id=None):
    from services.m5_tool_bridge import BoundToolCall

    return BoundToolCall(
        actual_call_id=call_id or f"{tool_name}-{uuid4()}",
        command_id=command_id or uuid4(),
        name=tool_name,
        raw_arguments=json.dumps(arguments, ensure_ascii=False),
        origin_session_id=origin,
        input_turn_id=str(uuid4()),
        input_order=1,
        stimulus_revision=0,
        response_id=f"local-{uuid4()}",
        supplier_response_id=f"qwen-{uuid4()}",
        request_id=f"event-{uuid4()}",
        tool_surface="m5",
    )


async def _origin(db_pool):
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT (get_or_create_chat_session(NULL,'chat',$1,'{}'::jsonb)->>'session_id')::uuid",
            f"m5-memory-write-origin-{uuid4()}",
        )


def _decode_json(value):
    return json.loads(value) if isinstance(value, str) else value


async def test_c3_remember_and_c4_add_evidence_use_real_memory_db_path(db_pool):
    from core.tools.registry import create_default_registry
    from services.behavior_events import query_behavior_events
    from services.lightweight_memory_executor import LightweightMemoryExecutor

    origin = await _origin(db_pool)
    marker = uuid4().hex
    registry = create_default_registry(db_pool)
    executor = LightweightMemoryExecutor(db_pool, registry=registry)

    async with db_pool.acquire() as conn:
        await _stub_get_embedding(conn)
        tasks_before = await conn.fetchval(
            "SELECT count(*) FROM m5_tasks WHERE origin_session_id=$1", origin
        )
        turns_before = await conn.fetchval(
            "SELECT count(*) FROM agent_turns WHERE session_id=$1", origin
        )

    remember_args = {
        "content": f"澄湾工作坊每周六上午九点开始。测试标记 {marker}",
        "type": "semantic",
    }
    remember_call = _bound_call(origin, "remember", remember_args)
    accepted_remember = await executor.submit_lightweight(remember_call, remember_args)
    assert accepted_remember["state"] == "accepted"
    assert accepted_remember["operation_id"] == str(remember_call.command_id)
    assert await executor.process_one() is True

    async with db_pool.acquire() as conn:
        remember_action = await conn.fetchrow(
            "SELECT state,result,arguments,execution_started_at FROM m5_actions WHERE command_id=$1",
            remember_call.command_id,
        )
    remember_result = _decode_json(remember_action["result"])
    assert remember_action["state"] == "done"
    assert remember_action["execution_started_at"] is not None
    assert _decode_json(remember_action["arguments"]) == remember_args
    memory_id = remember_result["data"]["memory_id"]
    assert remember_result["data"]["write_effect"] == "created"

    async with db_pool.acquire() as conn:
        memory = await conn.fetchrow(
            "SELECT id,type,status,source_attribution,metadata FROM memories WHERE id=$1",
            memory_id,
        )
    assert memory is not None
    assert memory["type"] == "semantic"
    assert memory["status"] == "active"
    metadata = _decode_json(memory["metadata"])
    assert metadata["tool_write"]["session_id"] == str(origin)
    assert metadata["tool_write"]["call_id"] == remember_call.actual_call_id

    manifest = json.loads((_PREP / "manifest.json").read_text(encoding="utf-8"))
    source_path = _PREP / "sources" / "澄湾工作坊正式日程.md"
    source_record = next(
        item for item in manifest["data_files"] if item["path"] == str(source_path)
    )
    source_bytes = source_path.read_bytes()
    assert hashlib.sha256(source_bytes).hexdigest() == source_record["sha256"]
    source_text = source_bytes.decode("utf-8")
    assert "每周六上午九点开始" in source_text

    evidence_args = {
        "memory_id": str(memory_id),
        "stance": "supports",
        "source": {
            "kind": "local_document",
            "ref": str(source_path),
            "label": "澄湾工作坊正式日程",
        },
        "note": source_text.strip(),
    }
    async with db_pool.acquire() as conn:
        target = await conn.fetchrow(
            "SELECT id,type,status FROM memories WHERE id=$1", memory_id
        )
        before_audit = await conn.fetchval(
            "SELECT count(*) FROM belief_revision_audit WHERE memory_id=$1", memory_id
        )
    assert target["type"] == "semantic" and target["status"] == "active"

    evidence_call = _bound_call(origin, "add_evidence", evidence_args)
    accepted_evidence = await executor.submit_lightweight(evidence_call, evidence_args)
    assert accepted_evidence["state"] == "accepted"
    assert await executor.process_one() is True

    async with db_pool.acquire() as conn:
        evidence_action = await conn.fetchrow(
            "SELECT state,result FROM m5_actions WHERE command_id=$1",
            evidence_call.command_id,
        )
        after_audit = await conn.fetchrow(
            "SELECT evidence,prior_confidence,posterior_confidence,applied,reason "
            "FROM belief_revision_audit WHERE memory_id=$1 ORDER BY created_at DESC LIMIT 1",
            memory_id,
        )
        updated_memory = await conn.fetchrow(
            "SELECT metadata FROM memories WHERE id=$1", memory_id
        )
        task_count = await conn.fetchval(
            "SELECT count(*) FROM m5_tasks WHERE origin_session_id=$1", origin
        )
        turn_count = await conn.fetchval(
            "SELECT count(*) FROM agent_turns WHERE session_id=$1", origin
        )
    evidence_result = _decode_json(evidence_action["result"])
    assert evidence_action["state"] == "done"
    assert evidence_result["data"]["memory_id"] == str(memory_id)
    assert evidence_result["data"]["prior_confidence"] == after_audit["prior_confidence"]
    assert evidence_result["data"]["posterior_confidence"] == after_audit["posterior_confidence"]
    assert evidence_result["data"]["write_effect"] == "updated"
    assert after_audit["applied"] is True
    assert after_audit["reason"] == "applied"
    assert after_audit["posterior_confidence"] > after_audit["prior_confidence"]
    evidence_record = _decode_json(after_audit["evidence"])
    assert evidence_record["ref"] == str(source_path)
    assert before_audit == 0
    assert task_count == tasks_before == 0
    assert turn_count == turns_before == 0

    duplicate_call = _bound_call(origin, "add_evidence", evidence_args)
    assert (await executor.submit_lightweight(duplicate_call, evidence_args))["state"] == "accepted"
    assert await executor.process_one() is True
    async with db_pool.acquire() as conn:
        duplicate_action = await conn.fetchrow(
            "SELECT state,result FROM m5_actions WHERE command_id=$1", duplicate_call.command_id
        )
        duplicate_audit = await conn.fetchrow(
            "SELECT prior_confidence,posterior_confidence,applied,reason "
            "FROM belief_revision_audit WHERE memory_id=$1 ORDER BY created_at DESC LIMIT 1",
            memory_id,
        )
        final_memory_metadata = await conn.fetchval(
            "SELECT metadata FROM memories WHERE id=$1", memory_id
        )
        final_tasks = await conn.fetchval(
            "SELECT count(*) FROM m5_tasks WHERE origin_session_id=$1", origin
        )
        final_turns = await conn.fetchval(
            "SELECT count(*) FROM agent_turns WHERE session_id=$1", origin
        )
        tool_audit_count = await conn.fetchval(
            "SELECT count(*) FROM tool_executions WHERE session_id=$1 "
            "AND call_id=ANY($2::text[])",
            str(origin),
            [remember_call.actual_call_id, evidence_call.actual_call_id, duplicate_call.actual_call_id],
        )
    duplicate_result = _decode_json(duplicate_action["result"])
    assert duplicate_action["state"] == "done"
    assert duplicate_result["data"]["prior_confidence"] == duplicate_result["data"]["posterior_confidence"]
    assert duplicate_result["data"]["write_effect"] == "updated"
    assert duplicate_audit["reason"] == "duplicate_source"
    assert duplicate_audit["applied"] is False
    assert duplicate_audit["prior_confidence"] == duplicate_audit["posterior_confidence"]
    source_references = _decode_json(final_memory_metadata).get("source_references", [])
    assert sum(item.get("ref") == str(source_path) for item in source_references) == 1
    assert final_tasks == final_turns == 0
    assert tool_audit_count == 3

    events = await query_behavior_events(
        db_pool,
        session_id=origin,
        event_type="TOOL_START",
        limit=20,
    )
    result_events = await query_behavior_events(
        db_pool,
        session_id=origin,
        event_type="TOOL_RESULT",
        limit=20,
    )
    starts = [event for event in events["events"] if event["payload"].get("command_id")]
    results = [event for event in result_events["events"] if event["payload"].get("command_id")]
    assert len(starts) == 3
    assert len(results) == 3
    for event in starts + results:
        assert event["task_id"] is None
        assert event["payload"]["task_id"] is None
        assert event["payload"]["execution_lane"] == "lightweight"


async def test_add_evidence_target_must_be_active_and_semantic_before_db_write(db_pool):
    from core.tools.registry import create_default_registry
    from services.behavior_events import query_behavior_events
    from services.lightweight_memory_executor import LightweightMemoryExecutor

    origin = await _origin(db_pool)
    registry = create_default_registry(db_pool)
    executor = LightweightMemoryExecutor(db_pool, registry=registry)
    async with db_pool.acquire() as conn:
        episodic_id = await conn.fetchval(
            "INSERT INTO memories(type,content,status) VALUES('episodic',$1,'active') RETURNING id",
            f"not-a-belief-{uuid4()}",
        )
    for target_id, expected_code in (
        (uuid4(), "TARGET_NOT_FOUND"),
        (episodic_id, "TARGET_TYPE_MISMATCH"),
    ):
        args = {
            "memory_id": str(target_id),
            "stance": "supports",
            "source": {
                "kind": "local_document",
                "ref": f"test-source:{uuid4()}",
                "label": "test source",
            },
        }
        call = _bound_call(origin, "add_evidence", args)
        assert (await executor.submit_lightweight(call, args))["state"] == "accepted"
        assert await executor.process_one() is True
        async with db_pool.acquire() as conn:
            action = await conn.fetchrow(
                "SELECT state,result FROM m5_actions WHERE command_id=$1", call.command_id
            )
            revision_count = await conn.fetchval(
                "SELECT count(*) FROM belief_revision_audit WHERE memory_id=$1",
                target_id,
            )
        result = _decode_json(action["result"])
        assert action["state"] == "failed"
        assert result["error"]["code"] == expected_code
        assert revision_count == 0
        events = await query_behavior_events(
            db_pool,
            session_id=origin,
            event_type="TOOL_RESULT",
            call_id=call.actual_call_id,
            limit=10,
        )
        assert len(events["events"]) == 1
        event = events["events"][0]
        assert event["task_id"] is None
        assert event["payload"]["execution_lane"] == "lightweight"
        assert event["payload"]["command_id"] == str(call.command_id)
        assert event["payload"]["success"] is False
        assert event["payload"]["error"] == result["error"]["message"]
