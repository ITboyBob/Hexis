"""Atomic M5 submit, durable task identity, and non-terminal acceptance."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest


pytestmark = [pytest.mark.asyncio(loop_scope="session")]


async def _new_origin(conn):
    return await conn.fetchval(
        "SELECT (get_or_create_chat_session($1, 'chat', NULL, '{}'::jsonb)->>'session_id')::uuid",
        uuid4(),
    )


def _authorization(origin_session_id, call_id: str) -> dict:
    return {
        "origin_session_id": str(origin_session_id),
        "input_turn_id": str(uuid4()),
        "input_order": 1,
        "stimulus_revision": 0,
        "response_id": f"local-{call_id}",
        "supplier_response_id": f"qwen-{call_id}",
        "request_id": f"event-{call_id}",
        "actual_call_id": call_id,
    }


async def test_submit_creates_one_task_session_and_first_work_round_atomically(db_pool):
    async with db_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT to_regclass('public.m5_tasks') IS NOT NULL"
        ), "M5 task persistence is not implemented"

    from services.m5_task_service import M5TaskService

    command_id = uuid4()
    description = "准备下一轮访谈提纲并按问题类型分组"
    original_request = "请准备访谈提纲，保留研究问题、格式和时间约束。"
    async with db_pool.acquire() as conn:
        origin_session_id = await _new_origin(conn)

    accepted = await M5TaskService(db_pool).submit_m5_task(
        origin_session_id,
        command_id,
        description,
        original_request,
        _authorization(origin_session_id, "call-first"),
    )

    assert accepted["accepted"] is True
    assert accepted["replayed"] is False
    assert accepted["operation_id"] == str(command_id)
    assert accepted["task_title"] == description
    assert accepted["task_state"] == "working"
    async with db_pool.acquire() as conn:
        rows = await conn.fetchrow(
            """
            SELECT t.id AS task_id, t.task_session_id, t.product_state,
                   t.task_description, t.original_user_request,
                   t.current_work_execution_id, w.status AS work_status,
                   w.input AS work_input, a.state AS action_state,
                   a.actual_call_id, a.authorization
            FROM public.m5_tasks t
            JOIN public.m5_work_rounds w ON w.id=t.current_work_execution_id
            JOIN public.m5_actions a ON a.command_id=t.submit_command_id
            WHERE t.id=$1
            """,
            accepted["task_id"],
        )
        origin_count = await conn.fetchval(
            "SELECT count(*) FROM chat_messages WHERE session_id=$1", origin_session_id
        )
        task_session = await conn.fetchrow(
            "SELECT id, surface, status FROM chat_sessions WHERE id=$1",
            accepted["task_session_id"],
        )

    assert str(rows["task_session_id"]) == accepted["task_session_id"]
    assert rows["product_state"] == "working"
    assert rows["task_description"] == description
    assert rows["original_user_request"] == original_request
    assert rows["work_status"] == "claimed"
    assert rows["work_input"] == description
    assert rows["action_state"] == "accepted"
    assert rows["actual_call_id"] == "call-first"
    assert json.loads(rows["authorization"])["input_turn_id"]
    assert task_session["surface"] == "m5-task"
    assert task_session["status"] == "active"
    assert origin_count == 0


async def test_submit_replay_reuses_business_result_but_new_call_and_conflicts_fail(
    db_pool,
):
    from services.m5_task_service import M5TaskService

    service = M5TaskService(db_pool)
    command_id = uuid4()
    description = "整理已经授权的资料"
    original_request = "请整理资料并保留引用。"
    async with db_pool.acquire() as conn:
        origin_session_id = await _new_origin(conn)

    first = await service.submit_m5_task(
        origin_session_id,
        command_id,
        description,
        original_request,
        _authorization(origin_session_id, "call-original"),
    )
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE chat_sessions SET status='archived' WHERE id=$1",
            origin_session_id,
        )
    replay = await service.submit_m5_task(
        origin_session_id,
        command_id,
        description,
        original_request,
        _authorization(origin_session_id, "call-replay"),
    )
    conflict = await service.submit_m5_task(
        origin_session_id,
        command_id,
        "different business arguments",
        original_request,
        _authorization(origin_session_id, "call-conflict"),
    )
    async with db_pool.acquire() as conn:
        other_origin = await _new_origin(conn)
    cross_origin_conflict = await service.submit_m5_task(
        other_origin,
        command_id,
        description,
        original_request,
        _authorization(other_origin, "call-cross-origin"),
    )

    assert replay["accepted"] is True and replay["replayed"] is True
    assert replay["task_id"] == first["task_id"]
    assert replay["task_session_id"] == first["task_session_id"]
    assert replay["work_execution_id"] == first["work_execution_id"]
    assert conflict["accepted"] is False
    assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert cross_origin_conflict["accepted"] is False
    assert cross_origin_conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    async with db_pool.acquire() as conn:
        action = await conn.fetchrow(
            "SELECT actual_call_id, argument_digest FROM m5_actions WHERE command_id=$1",
            command_id,
        )
        task_count = await conn.fetchval(
            "SELECT count(*) FROM m5_tasks WHERE submit_command_id=$1", command_id
        )
        work_count = await conn.fetchval(
            "SELECT count(*) FROM m5_work_rounds WHERE task_id=$1", first["task_id"]
        )
    assert action["actual_call_id"] == "call-original"
    assert len(action["argument_digest"]) == 64
    assert task_count == 1
    assert work_count == 1


async def test_same_text_with_new_command_and_other_origin_creates_distinct_task(db_pool):
    from services.m5_task_service import M5TaskService

    service = M5TaskService(db_pool)
    async with db_pool.acquire() as conn:
        origin_one = await _new_origin(conn)
        origin_two = await _new_origin(conn)
    description = "查找相关资料并整理来源"
    original_request = "请查找资料，列出引用来源。"
    first = await service.submit_m5_task(
        origin_one,
        uuid4(),
        description,
        original_request,
        _authorization(origin_one, "call-origin-1"),
    )
    second = await service.submit_m5_task(
        origin_two,
        uuid4(),
        description,
        original_request,
        _authorization(origin_two, "call-origin-2"),
    )

    assert first["task_id"] != second["task_id"]
    assert first["task_session_id"] != second["task_session_id"]
    assert first["task_session_id"] != str(origin_one)
    assert second["task_session_id"] != str(origin_two)


async def test_concurrent_same_command_returns_one_accepted_action(db_pool):
    from services.m5_task_service import M5TaskService

    service = M5TaskService(db_pool)
    command_id = uuid4()
    async with db_pool.acquire() as conn:
        origin_session_id = await _new_origin(conn)
    description = "按既定要求完成一份总结"
    original_request = "请保留结论与来源。"

    async def submit(call_id):
        return await service.submit_m5_task(
            origin_session_id,
            command_id,
            description,
            original_request,
            _authorization(origin_session_id, call_id),
        )

    first, second = await asyncio.gather(submit("call-race-1"), submit("call-race-2"))

    assert first["accepted"] and second["accepted"]
    assert first["task_id"] == second["task_id"]
    assert first["task_session_id"] == second["task_session_id"]
    async with db_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM m5_actions WHERE command_id=$1", command_id
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM m5_tasks WHERE submit_command_id=$1", command_id
        ) == 1
