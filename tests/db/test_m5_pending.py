"""M5 pending append, idempotency, single claims, and FIFO work binding."""

from __future__ import annotations

from uuid import uuid4

import pytest


pytestmark = [pytest.mark.asyncio(loop_scope="session")]


async def _origin_session(conn):
    return await conn.fetchval(
        "SELECT (get_or_create_chat_session(NULL, 'chat', $1, '{}'::jsonb)->>'session_id')::uuid",
        f"m5-pending-origin-{uuid4()}",
    )


def _authorization(origin, task_id, call_id):
    return {
        "origin_session_id": str(origin),
        "task_id": str(task_id),
        "input_turn_id": str(uuid4()),
        "input_order": 3,
        "stimulus_revision": 1,
        "response_id": f"local-{call_id}",
        "supplier_response_id": f"qwen-{call_id}",
        "request_id": f"event-{call_id}",
        "actual_call_id": call_id,
    }


async def test_three_pending_messages_replay_once_and_claim_in_fifo_order(db_pool):
    from services.m5_task_service import M5TaskService

    service = M5TaskService(db_pool)
    async with db_pool.acquire() as conn:
        origin = await _origin_session(conn)
    submitted = await service.submit_m5_task(
        origin,
        uuid4(),
        "核对订单总价和计算依据",
        "请核对总价，保留计算依据并用一句中文总结。",
        {
            "origin_session_id": str(origin),
            "input_turn_id": str(uuid4()),
            "input_order": 1,
            "stimulus_revision": 0,
            "response_id": "submit-response",
            "supplier_response_id": "submit-provider-response",
            "request_id": "submit-event",
            "actual_call_id": "submit-call",
        },
    )
    assert submitted["accepted"] is True
    first_work = submitted["work_execution_id"]
    assert await service.claim_work_round(first_work) is not None

    messages = [
        "先给出订单总价。",
        "再给出计算依据。",
        "最后将总价和依据合为一句中文。",
    ]
    command_ids = [uuid4() for _ in messages]
    accepted = []
    for index, (command_id, message) in enumerate(zip(command_ids, messages), 1):
        accepted.append(
            await service.append_m5_message(
                origin,
                submitted["task_id"],
                command_id,
                message,
                _authorization(origin, submitted["task_id"], f"pend-{index}"),
            )
        )
    replay = await service.append_m5_message(
        origin,
        submitted["task_id"],
        command_ids[0],
        messages[0],
        _authorization(origin, submitted["task_id"], "pend-replay"),
    )
    conflict = await service.append_m5_message(
        origin,
        submitted["task_id"],
        command_ids[0],
        "changed message for the same command",
        _authorization(origin, submitted["task_id"], "pend-conflict"),
    )
    async with db_pool.acquire() as conn:
        other_origin = await _origin_session(conn)
    ownership_conflict = await service.append_m5_message(
        other_origin,
        submitted["task_id"],
        uuid4(),
        "unauthorized cross-origin message",
        _authorization(other_origin, submitted["task_id"], "pend-wrong-origin"),
    )
    same_text_new_action = await service.append_m5_message(
        origin,
        submitted["task_id"],
        uuid4(),
        messages[0],
        _authorization(origin, submitted["task_id"], "pend-same-text-new-action"),
    )

    assert [item["receive_order"] for item in accepted] == [1, 2, 3]
    assert replay["message_id"] == accepted[0]["message_id"]
    assert replay["replayed"] is True
    assert conflict["accepted"] is False
    assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert ownership_conflict["accepted"] is False
    assert ownership_conflict["error"]["code"] == "TARGET_OWNERSHIP_MISMATCH"
    assert same_text_new_action["message_id"] != accepted[0]["message_id"]
    assert same_text_new_action["receive_order"] == 4
    assert await service.claim_next_m5_message(submitted["task_id"]) is None
    pending = await service.list_pending_messages(submitted["task_id"])
    assert [item["message"] for item in pending] == messages + [messages[0]]

    await service.finish_work_round(
        task_id=submitted["task_id"],
        work_execution_id=first_work,
        task_session_id=submitted["task_session_id"],
        status="completed",
        result={"text": "Initial round"},
        error=None,
        turn_id=None,
    )
    closed = await service.finish_m5_round(
        submitted["task_id"], first_work, report_outcome="delivered"
    )
    assert closed["task_state"] == "working"
    assert closed["next_message_id"] == accepted[0]["message_id"]

    async with db_pool.acquire() as conn:
        task = await conn.fetchrow(
            "SELECT product_state,current_work_execution_id,revision FROM m5_tasks WHERE id=$1",
            submitted["task_id"],
        )
        work = await conn.fetchrow(
            "SELECT task_id,message_id,status,input FROM m5_work_rounds WHERE id=$1",
            closed["next_work_execution_id"],
        )
        first_message = await conn.fetchrow(
            "SELECT task_id,receive_order,message,claimed_work_execution_id FROM m5_pending_messages WHERE id=$1",
            accepted[0]["message_id"],
        )
        count = await conn.fetchval(
            "SELECT count(*) FROM m5_pending_messages WHERE task_id=$1",
            submitted["task_id"],
        )

    assert task["product_state"] == "working"
    assert str(task["current_work_execution_id"]) == closed["next_work_execution_id"]
    assert work["status"] == "claimed"
    assert str(work["task_id"]) == submitted["task_id"]
    assert str(work["message_id"]) == accepted[0]["message_id"]
    assert work["input"] == messages[0]
    assert str(first_message["claimed_work_execution_id"]) == closed["next_work_execution_id"]
    assert first_message["receive_order"] == 1
    assert first_message["message"] == messages[0]
    assert count == 4
    assert len(pending) == 4
    assert [item["receive_order"] for item in await service.list_pending_messages(submitted["task_id"])] == [2, 3, 4]


async def test_idle_append_claims_exactly_one_message_in_the_same_transaction(db_pool):
    from services.m5_task_service import M5TaskService

    service = M5TaskService(db_pool)
    async with db_pool.acquire() as conn:
        origin = await _origin_session(conn)
    submitted = await service.submit_m5_task(
        origin,
        uuid4(),
        "Finish the current work",
        "Finish the current work before accepting the next message.",
        {
            "origin_session_id": str(origin),
            "input_turn_id": str(uuid4()),
            "input_order": 1,
            "stimulus_revision": 0,
            "response_id": "submit-idle-response",
            "supplier_response_id": "submit-idle-qwen",
            "request_id": "submit-idle-event",
            "actual_call_id": "submit-idle-call",
        },
    )
    first_work = submitted["work_execution_id"]
    await service.claim_work_round(first_work)
    await service.finish_work_round(
        task_id=submitted["task_id"],
        work_execution_id=first_work,
        task_session_id=submitted["task_session_id"],
        status="completed",
        result={"text": "Current work done"},
        error=None,
        turn_id=None,
    )
    closed = await service.finish_m5_round(
        submitted["task_id"], first_work, report_outcome="delivered"
    )
    assert closed["task_state"] == "idle"

    result = await service.append_m5_message(
        origin,
        submitted["task_id"],
        uuid4(),
        "Start this next message now.",
        _authorization(origin, submitted["task_id"], "call-idle-append"),
    )

    assert result["accepted"] is True
    assert result["task_state"] == "working"
    assert result["receive_order"] == 1
    assert result["work_execution_id"] is not None
    async with db_pool.acquire() as conn:
        task = await conn.fetchrow(
            "SELECT product_state,current_work_execution_id FROM m5_tasks WHERE id=$1",
            submitted["task_id"],
        )
        links = await conn.fetchrow(
            """
            SELECT p.receive_order,p.message,p.claimed_work_execution_id,w.message_id
            FROM public.m5_pending_messages p
            JOIN public.m5_work_rounds w ON w.id=p.claimed_work_execution_id
            WHERE p.id=$1
            """,
            result["message_id"],
        )
    assert task["product_state"] == "working"
    assert str(task["current_work_execution_id"]) == result["work_execution_id"]
    assert links["receive_order"] == 1
    assert links["message"] == "Start this next message now."
    assert str(links["message_id"]) == result["message_id"]
