from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import pytest

from services.m5_tool_bridge import (
    BoundToolCall,
    execute_bound_tool,
    lightweight_memory_handlers,
)


pytestmark = [pytest.mark.asyncio(loop_scope="session")]


class _AuthorizedSession:
    def validate_voice_tool_call(self, call):
        assert call.actual_call_id
        assert call.input_turn_id


def _call(origin, *, arguments: dict, command_id=None, call_id="m5-memory-call"):
    return BoundToolCall(
        actual_call_id=call_id,
        command_id=command_id or uuid4(),
        name="remember",
        raw_arguments=json.dumps(arguments, ensure_ascii=False),
        origin_session_id=origin,
        input_turn_id=str(uuid4()),
        input_order=1,
        stimulus_revision=0,
        response_id="local-response-1",
        supplier_response_id="qwen-response-1",
        request_id="qwen-event-1",
        tool_surface="m5",
    )


async def _origin_session(db_pool):
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT (get_or_create_chat_session(NULL,'chat',$1,'{}'::jsonb)->>'session_id')::uuid",
            f"m5-lightweight-origin-{uuid4()}",
        )


async def test_lightweight_acceptance_is_durable_idempotent_and_creates_no_task(db_pool):
    from services.behavior_events import query_behavior_events
    from services.lightweight_memory_executor import LightweightMemoryExecutor

    origin = await _origin_session(db_pool)
    command_id = uuid4()
    call = _call(
        origin,
        command_id=command_id,
        arguments={"content": "澄湾工作坊每周六上午九点开始。", "type": "semantic"},
    )
    executor = LightweightMemoryExecutor(db_pool)
    handlers = lightweight_memory_handlers(executor)

    raw_first = await execute_bound_tool(_AuthorizedSession(), call, handlers=handlers)
    raw_replay = await execute_bound_tool(_AuthorizedSession(), call, handlers=handlers)
    first = json.loads(raw_first)
    replay = json.loads(raw_replay)

    assert first["ok"] is True
    assert first["state"] == "accepted"
    assert first["operation_id"] == str(command_id)
    assert "memory_id" not in first["data"]
    assert replay["ok"] is True
    assert replay["state"] == "accepted"
    assert replay["operation_id"] == str(command_id)
    events = await query_behavior_events(
        db_pool,
        session_id=origin,
        event_type="TOOL_RESULT",
        call_id=call.actual_call_id,
        limit=10,
    )
    assert events["events"] == []

    async with db_pool.acquire() as conn:
        action = await conn.fetchrow(
            'SELECT tool_name,argument_digest,arguments,"authorization",state,result,execution_started_at '
            "FROM public.m5_actions WHERE command_id=$1",
            command_id,
        )
        action_count = await conn.fetchval(
            "SELECT count(*) FROM public.m5_actions WHERE command_id=$1", command_id
        )
        task_count = await conn.fetchval(
            "SELECT count(*) FROM public.m5_tasks WHERE origin_session_id=$1", origin
        )
        turn_count = await conn.fetchval(
            "SELECT count(*) FROM public.agent_turns WHERE session_id=$1", origin
        )
    assert action["tool_name"] == "remember"
    assert len(action["argument_digest"]) == 64
    assert json.loads(action["arguments"]) == {
        "content": "澄湾工作坊每周六上午九点开始。",
        "type": "semantic",
    }
    authorization = json.loads(action["authorization"])
    assert authorization["origin_session_id"] == str(origin)
    assert authorization["actual_call_id"] == call.actual_call_id
    assert "arguments" not in authorization
    assert action["state"] == "accepted"
    assert action["execution_started_at"] is None
    assert action_count == 1
    assert task_count == 0
    assert turn_count == 0
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM public.m5_actions WHERE command_id=$1", command_id)


async def test_lightweight_command_conflict_does_not_replace_saved_arguments(db_pool):
    from services.lightweight_memory_executor import LightweightMemoryExecutor

    origin = await _origin_session(db_pool)
    command_id = uuid4()
    executor = LightweightMemoryExecutor(db_pool)
    handlers = lightweight_memory_handlers(executor)
    first_call = _call(
        origin,
        command_id=command_id,
        arguments={"content": "原始授权内容", "type": "semantic"},
    )
    conflicting_call = _call(
        origin,
        command_id=command_id,
        call_id="m5-memory-call-conflict",
        arguments={"content": "替换成另一段内容", "type": "semantic"},
    )

    first = json.loads(
        await execute_bound_tool(_AuthorizedSession(), first_call, handlers=handlers)
    )
    conflict = json.loads(
        await execute_bound_tool(_AuthorizedSession(), conflicting_call, handlers=handlers)
    )

    assert first["state"] == "accepted"
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT arguments,state FROM public.m5_actions WHERE command_id=$1",
            command_id,
        )
    assert json.loads(row["arguments"]) == {
        "content": "原始授权内容",
        "type": "semantic",
    }
    assert row["state"] == "accepted"
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM public.m5_actions WHERE command_id=$1", command_id)


async def test_lightweight_only_accepts_m5_surface(db_pool):
    from services.lightweight_memory_executor import LightweightMemoryExecutor

    origin = await _origin_session(db_pool)
    call = _call(
        origin,
        arguments={"content": "不能进入非 M5 工具面", "type": "semantic"},
    )
    non_m5 = replace(call, tool_surface="m4")
    executor = LightweightMemoryExecutor(db_pool)
    output = await execute_bound_tool(
        _AuthorizedSession(),
        non_m5,
        handlers=lightweight_memory_handlers(executor),
    )
    receipt = json.loads(output)
    assert receipt["ok"] is False
    assert receipt["error"]["code"] == "TOOL_NOT_ALLOWED"
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM m5_actions WHERE command_id=$1", call.command_id) == 0


async def test_claimed_action_recovery_marks_unknown_and_never_reclaims(db_pool):
    from services.lightweight_memory_executor import LightweightMemoryExecutor
    from services.behavior_events import query_behavior_events

    origin = await _origin_session(db_pool)
    command_id = uuid4()
    arguments = {"content": f"不重复执行的记忆 {uuid4()}", "type": "semantic"}
    call = _call(origin, command_id=command_id, arguments=arguments)
    executor = LightweightMemoryExecutor(db_pool)
    accepted = await executor.submit_lightweight(call, arguments)
    assert accepted["state"] == "accepted"

    async with db_pool.acquire() as conn:
        claim = await conn.fetchval("SELECT public.claim_m5_lightweight_action()")
    claim = json.loads(claim) if isinstance(claim, str) else claim
    assert claim["command_id"] == str(command_id)
    assert claim["arguments"] == arguments
    assert claim["execution_started_at"]

    assert await executor.mark_interrupted_unknown() == 1
    assert await executor.process_one() is False
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state,result,execution_started_at FROM m5_actions WHERE command_id=$1",
            command_id,
        )
        memory_count = await conn.fetchval(
            "SELECT count(*) FROM memories WHERE content=$1", arguments["content"]
        )
        assert await conn.fetchval(
            "SELECT count(*) FROM m5_tasks WHERE origin_session_id=$1", origin
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM agent_turns WHERE session_id=$1", origin
        ) == 0
    result = json.loads(row["result"])
    assert row["state"] == "unknown"
    assert row["execution_started_at"] is not None
    assert result["error"]["code"] == "EXECUTION_UNKNOWN"
    assert memory_count == 0
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
    assert event["payload"]["command_id"] == str(command_id)
    assert event["payload"]["success"] is False
    assert event["payload"]["state"] == "unknown"
    assert event["payload"]["error"] == result["error"]["message"]


@pytest.mark.parametrize(
    ("tool_result", "expected_state"),
    [
        ("done", "done"),
        ("failed", "failed"),
        ("unknown", "unknown"),
    ],
)
async def test_terminal_event_preserves_the_authoritative_receipt_state(db_pool, monkeypatch, tool_result, expected_state):
    from core.tools import create_default_registry
    from core.tools.base import ToolErrorType, ToolResult
    from services.behavior_events import query_behavior_events
    from services.lightweight_memory_executor import LightweightMemoryExecutor

    origin = await _origin_session(db_pool)
    arguments = {"content": f"受控终态状态 {uuid4()}", "type": "semantic"}
    call = _call(origin, arguments=arguments, call_id=f"state-{expected_state}-{uuid4()}")
    registry = create_default_registry(db_pool)
    handler = registry.get("remember")
    assert handler is not None
    if tool_result == "done":
        result = ToolResult.success_result({"memory_id": str(uuid4()), "reused": False}, "记忆已保存")
    elif tool_result == "failed":
        result = ToolResult.error_result("受控失败原因", error_type=ToolErrorType.INVALID_PARAMS)
    else:
        result = ToolResult.error_result("受控未确认原因", error_type=ToolErrorType.EXECUTION_FAILED)

    async def execute(_arguments, _context):
        return result

    monkeypatch.setattr(handler, "execute", execute)
    executor = LightweightMemoryExecutor(db_pool, registry=registry)
    accepted = await executor.submit_lightweight(call, arguments)
    assert accepted["state"] == "accepted"
    assert await executor.process_one() is True

    events = await query_behavior_events(
        db_pool,
        session_id=origin,
        event_type="TOOL_RESULT",
        call_id=call.actual_call_id,
        limit=10,
    )
    assert len(events["events"]) == 1
    payload = events["events"][0]["payload"]
    assert payload["state"] == expected_state
    assert payload["success"] is (expected_state == "done")
    assert payload["error"] == result.error
    async with db_pool.acquire() as conn:
        receipt = await conn.fetchval(
            "SELECT result FROM public.m5_actions WHERE command_id=$1", call.command_id
        )
    assert payload["state"] == json.loads(receipt)["state"]


async def test_lightweight_handler_builder_only_exposes_approved_memory_tools():
    from services.m5_tool_bridge import lightweight_memory_handlers

    async def submit(_call, _arguments):
        return {"ok": True, "state": "accepted", "operation_id": str(uuid4()), "data": {}}

    handlers = lightweight_memory_handlers(type("Executor", (), {"submit_lightweight": submit})())
    assert set(handlers) == {"remember", "add_evidence"}
    assert lightweight_memory_handlers(None) == {}
