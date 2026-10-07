from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest


pytestmark = [pytest.mark.asyncio(loop_scope="session")]


async def _origin(conn):
    return await conn.fetchval(
        "SELECT (get_or_create_chat_session($1, 'chat', NULL, '{}'::jsonb)->>'session_id')::uuid",
        uuid4(),
    )


def _authorization(origin, call_id: str) -> dict:
    return {
        "origin_session_id": str(origin),
        "input_turn_id": str(uuid4()),
        "input_order": 1,
        "stimulus_revision": 0,
        "response_id": f"response-{call_id}",
        "supplier_response_id": f"supplier-{call_id}",
        "request_id": f"event-{call_id}",
        "actual_call_id": call_id,
    }


async def _submit_task(db_pool, origin, label="selection target"):
    from services.m5_task_service import M5TaskService

    return await M5TaskService(db_pool).submit_m5_task(
        origin,
        uuid4(),
        label,
        "Keep the user's constraints and report the outcome.",
        _authorization(origin, f"submit-{uuid4()}"),
    )


async def _begin(db_pool, origin, message="Please continue the interview outline."):
    from services.m5_selection_service import M5SelectionService

    call_id = f"pend-{uuid4()}"
    return await M5SelectionService(db_pool).begin_pend_message(
        origin_session_id=origin,
        command_id=uuid4(),
        authorization=_authorization(origin, call_id),
        message=message,
    )


@pytest.mark.parametrize("action", ["confirm", "cancel"])
async def test_voice_receipt_waits_for_durable_choice_and_matches_v1_contract(db_pool, action):
    from services.m5_selection_service import M5SelectionService
    from services.m5_tool_bridge import BoundToolCall, execute_bound_tool, pend_message_handler

    async with db_pool.acquire() as conn:
        origin = await _origin(conn)
    target = await _submit_task(db_pool, origin)
    authorization = _authorization(origin, f"voice-pend-{uuid4()}")
    call = BoundToolCall(
        actual_call_id=authorization["actual_call_id"], command_id=uuid4(),
        name="pend_message", raw_arguments='{"message":"Preserve the original order."}',
        origin_session_id=origin, input_turn_id=authorization["input_turn_id"],
        input_order=1, stimulus_revision=0, response_id=authorization["response_id"],
        supplier_response_id=authorization["supplier_response_id"],
        request_id=authorization["request_id"], tool_surface="m5",
    )
    opened = asyncio.get_running_loop().create_future()

    async def on_opened(selection):
        opened.set_result(selection)

    session = SimpleNamespace(validate_voice_tool_call=lambda bound: None)
    handler = pend_message_handler(db_pool, on_opened)
    operation = asyncio.create_task(execute_bound_tool(session, call, handlers={"pend_message": handler}))
    try:
        selection = await asyncio.wait_for(opened, timeout=2)
        assert not operation.done(), "an open selection cannot have a pending success receipt"
        service = M5SelectionService(db_pool)
        if action == "confirm":
            await service.confirm_pend_message(selection_id=selection["selection_id"], version=1, task_id=target["task_id"])
        else:
            await service.cancel_pend_message(selection_id=selection["selection_id"], version=1)
        receipt = json.loads(await asyncio.wait_for(operation, timeout=2))
        assert receipt["ok"] is True
        assert receipt["call_id"] == call.actual_call_id
        assert receipt["operation_id"] == str(call.command_id)
        assert "selection_id" not in receipt["data"] and "version" not in receipt["data"]
        if action == "confirm":
            assert receipt["state"] == "pending"
            assert set(receipt["data"]) == {"task_id", "task_title", "task_state", "message_id"}
            assert receipt["data"]["task_id"] == target["task_id"]
        else:
            assert receipt["state"] == "selection_cancelled"
            assert receipt["data"] == {}
        replay = json.loads(await execute_bound_tool(session, call, handlers={"pend_message": handler}))
        assert replay == receipt
        async with db_pool.acquire() as conn:
            count = await conn.fetchval("SELECT count(*) FROM m5_pending_messages WHERE command_id=$1",call.command_id)
        assert count == (1 if action == "confirm" else 0)
    finally:
        if not operation.done():
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation


async def test_begin_persists_bound_selection_and_open_event_without_pending_message(db_pool):
    from services.m5_selection_service import M5SelectionService
    from services.m5_task_service import M5TaskService

    async with db_pool.acquire() as conn:
        origin = await _origin(conn)
    message = "Please preserve the interview's time limit and existing question order."

    opened = await _begin(db_pool, origin, message)

    assert opened["state"] == "open"
    assert opened["version"] == 1
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT origin_session_id,command_id,actual_call_id,"authorization",message,
                   state,chosen_task_id,version
            FROM public.m5_selections WHERE selection_id=$1::uuid
            """,
            opened["selection_id"],
        )
        pending = await conn.fetchval(
            "SELECT count(*) FROM public.m5_pending_messages WHERE command_id=$1::uuid",
            row["command_id"],
        )
        action = await conn.fetchrow(
            "SELECT tool_name,state,result FROM public.m5_actions WHERE command_id=$1::uuid",
            row["command_id"],
        )
        events = await conn.fetch(
            """
            SELECT event_type,task_id,payload FROM public.behavior_events
            WHERE session_id=$1 AND event_type LIKE 'm5_selection_%'
            ORDER BY seq
            """,
            origin,
        )

    assert str(row["origin_session_id"]) == str(origin)
    assert row["actual_call_id"].startswith("pend-")
    assert row["message"] == message
    assert row["state"] == "open"
    assert row["chosen_task_id"] is None
    assert row["version"] == 1
    assert pending == 0
    assert action["tool_name"] == "pend_message"
    assert action["state"] == "pending"
    assert action["result"] is None
    assert len(events) == 1
    assert events[0]["event_type"] == "m5_selection_opened"
    assert events[0]["task_id"] is None
    first_payload = json.loads(events[0]["payload"])
    assert first_payload["selection_id"] == opened["selection_id"]
    assert first_payload["version"] == 1
    snapshot = await M5TaskService(db_pool).list_tasks_for_origin(origin)
    assert snapshot["event_cursor"]["m5-tasks"] == 1
    assert snapshot["selections"] == [
        {
            "selection_id": opened["selection_id"],
            "version": 1,
            "message": message,
        }
    ]


async def test_replayed_pend_call_reuses_one_open_selection_and_event(db_pool):
    from services.m5_selection_service import M5SelectionService

    async with db_pool.acquire() as conn:
        origin = await _origin(conn)
    command_id = uuid4()
    authorization = _authorization(origin, f"pend-replay-{uuid4()}")
    service = M5SelectionService(db_pool)
    arguments = {
        "origin_session_id": origin,
        "command_id": command_id,
        "authorization": authorization,
        "message": "Continue the second section without changing its order.",
    }

    first = await service.begin_pend_message(**arguments)
    replay = await service.begin_pend_message(**arguments)

    assert replay["accepted"] is True
    assert replay["replayed"] is True
    assert replay["selection_id"] == first["selection_id"]
    async with db_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM m5_selections WHERE command_id=$1::uuid",
            command_id,
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM behavior_events WHERE session_id=$1 AND event_type='m5_selection_opened'",
            origin,
        ) == 1


async def test_confirm_appends_through_same_origin_and_replay_does_not_duplicate(db_pool):
    from services.m5_selection_service import M5SelectionService
    from services.m5_task_service import M5TaskService

    async with db_pool.acquire() as conn:
        origin = await _origin(conn)
    task = await _submit_task(db_pool, origin)
    selection = await _begin(db_pool, origin)
    before = await M5TaskService(db_pool).list_tasks_for_origin(origin)

    confirmed = await M5SelectionService(db_pool).confirm_pend_message(
        origin_session_id=origin,
        selection_id=selection["selection_id"],
        version=selection["version"],
        task_id=task["task_id"],
    )
    replay = await M5SelectionService(db_pool).confirm_pend_message(
        origin_session_id=origin,
        selection_id=selection["selection_id"],
        version=selection["version"],
        task_id=task["task_id"],
    )
    after = await M5TaskService(db_pool).list_tasks_for_origin(origin)

    assert confirmed["accepted"] is True
    assert confirmed["task_id"] == task["task_id"]
    assert confirmed["message_id"]
    assert confirmed["receive_order"] == 1
    assert replay["replayed"] is True
    assert replay["message_id"] == confirmed["message_id"]
    assert after["event_cursor"]["m5-tasks"] == before["event_cursor"]["m5-tasks"] + 2
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_type,task_id,payload FROM behavior_events WHERE session_id=$1 AND event_type LIKE 'm5_selection_%' ORDER BY seq",
            origin,
        )
        messages = await conn.fetch(
            "SELECT id,task_id,receive_order,message FROM m5_pending_messages WHERE command_id=$1::uuid",
            selection["command_id"],
        )
        selected = await conn.fetchrow(
            "SELECT state,chosen_task_id,version FROM m5_selections WHERE selection_id=$1::uuid",
            selection["selection_id"],
        )
        action = await conn.fetchrow(
            "SELECT state,result FROM m5_actions WHERE command_id=$1::uuid",
            selection["command_id"],
        )

    assert len(rows) == 2
    assert [row["event_type"] for row in rows] == [
        "m5_selection_opened",
        "m5_selection_closed",
    ]
    assert rows[-1]["task_id"] is None
    assert json.loads(rows[-1]["payload"])["state"] == "confirmed"
    assert len(messages) == 1
    assert str(messages[0]["id"]) == confirmed["message_id"]
    assert str(messages[0]["task_id"]) == task["task_id"]
    assert selected["state"] == "confirmed"
    assert str(selected["chosen_task_id"]) == task["task_id"]
    assert selected["version"] == 2
    assert action["state"] == "pending"
    assert json.loads(action["result"])["message_id"] == confirmed["message_id"]


async def test_cancel_closes_selection_without_message_or_task_revision(db_pool):
    from services.m5_selection_service import M5SelectionService
    from services.m5_task_service import M5TaskService

    async with db_pool.acquire() as conn:
        origin = await _origin(conn)
    task = await _submit_task(db_pool, origin)
    selection = await _begin(db_pool, origin)
    before = await M5TaskService(db_pool).list_tasks_for_origin(origin)

    cancelled = await M5SelectionService(db_pool).cancel_pend_message(
        origin_session_id=origin,
        selection_id=selection["selection_id"],
        version=selection["version"],
    )
    replay = await M5SelectionService(db_pool).cancel_pend_message(
        origin_session_id=origin,
        selection_id=selection["selection_id"],
        version=selection["version"],
    )
    after = await M5TaskService(db_pool).list_tasks_for_origin(origin)

    assert cancelled["state"] == "selection_cancelled"
    assert replay["replayed"] is True
    assert after["event_cursor"]["m5-tasks"] == before["event_cursor"]["m5-tasks"] + 1
    assert after["tasks"] == before["tasks"]
    async with db_pool.acquire() as conn:
        pending = await conn.fetchval(
            "SELECT count(*) FROM m5_pending_messages WHERE command_id=$1::uuid",
            selection["command_id"],
        )
        events = await conn.fetch(
            "SELECT event_type,payload FROM behavior_events WHERE session_id=$1 AND event_type LIKE 'm5_selection_%' ORDER BY seq",
            origin,
        )
        row = await conn.fetchrow(
            "SELECT state,chosen_task_id,version FROM m5_selections WHERE selection_id=$1::uuid",
            selection["selection_id"],
        )
        action_state = await conn.fetchval(
            "SELECT state FROM m5_actions WHERE command_id=$1::uuid",
            selection["command_id"],
        )
    assert pending == 0
    assert [event["event_type"] for event in events] == [
        "m5_selection_opened",
        "m5_selection_closed",
    ]
    assert json.loads(events[-1]["payload"])["state"] == "cancelled"
    assert row["state"] == "cancelled"
    assert row["chosen_task_id"] is None
    assert row["version"] == 2
    assert action_state == "selection_cancelled"


async def test_confirm_rejects_wrong_origin_task_and_stale_version(db_pool):
    from services.m5_selection_service import M5SelectionService

    async with db_pool.acquire() as conn:
        origin = await _origin(conn)
        other_origin = await _origin(conn)
    task = await _submit_task(db_pool, other_origin, "foreign task")
    selection = await _begin(db_pool, origin)
    service = M5SelectionService(db_pool)

    wrong_origin = await service.confirm_pend_message(
        origin_session_id=other_origin,
        selection_id=selection["selection_id"],
        version=selection["version"],
        task_id=task["task_id"],
    )
    wrong_task = await service.confirm_pend_message(
        origin_session_id=origin,
        selection_id=selection["selection_id"],
        version=selection["version"],
        task_id=task["task_id"],
    )
    stale = await service.confirm_pend_message(
        origin_session_id=origin,
        selection_id=selection["selection_id"],
        version=selection["version"] + 1,
        task_id=task["task_id"],
    )

    assert wrong_origin["accepted"] is False
    assert wrong_origin["error"]["code"] == "SELECTION_ORIGIN_MISMATCH"
    assert wrong_task["accepted"] is False
    assert wrong_task["error"]["code"] == "TARGET_OWNERSHIP_MISMATCH"
    assert stale["accepted"] is False
    assert stale["error"]["code"] == "SELECTION_VERSION_CONFLICT"
    async with db_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM m5_pending_messages WHERE command_id=$1::uuid",
            selection["command_id"],
        ) == 0
        assert await conn.fetchval(
            "SELECT state FROM m5_selections WHERE selection_id=$1::uuid",
            selection["selection_id"],
        ) == "open"


async def test_confirm_rolls_back_message_action_and_close_event_if_event_write_fails(
    db_pool, monkeypatch
):
    import services.m5_selection_service as selection_module
    from services.m5_selection_service import M5SelectionService

    async with db_pool.acquire() as conn:
        origin = await _origin(conn)
    task = await _submit_task(db_pool, origin)
    selection = await _begin(db_pool, origin)
    real_writer = selection_module.record_behavior_event_on_connection

    async def fail_close(conn, event):
        if event.event_type == "m5_selection_closed":
            raise RuntimeError("selection close event write failed")
        await real_writer(conn, event)

    monkeypatch.setattr(selection_module, "record_behavior_event_on_connection", fail_close)
    with pytest.raises(RuntimeError, match="selection close event"):
        await M5SelectionService(db_pool).confirm_pend_message(
            origin_session_id=origin,
            selection_id=selection["selection_id"],
            version=selection["version"],
            task_id=task["task_id"],
        )

    async with db_pool.acquire() as conn:
        selection_state = await conn.fetchval(
            "SELECT state FROM m5_selections WHERE selection_id=$1::uuid",
            selection["selection_id"],
        )
        messages = await conn.fetchval(
            "SELECT count(*) FROM m5_pending_messages WHERE command_id=$1::uuid",
            selection["command_id"],
        )
        action = await conn.fetchrow(
            "SELECT state,result FROM m5_actions WHERE command_id=$1::uuid",
            selection["command_id"],
        )
        close_events = await conn.fetchval(
            "SELECT count(*) FROM behavior_events WHERE session_id=$1 AND event_type='m5_selection_closed'",
            origin,
        )
    assert selection_state == "open"
    assert messages == 0
    assert action["state"] == "pending"
    assert action["result"] is None
    assert close_events == 0


async def test_confirm_on_idle_task_claims_the_selected_message_in_fifo_order(db_pool):
    from services.m5_selection_service import M5SelectionService
    from services.m5_task_service import M5TaskService

    async with db_pool.acquire() as conn:
        origin = await _origin(conn)
    task = await _submit_task(db_pool, origin, "idle selection target")
    service = M5TaskService(db_pool)
    assert await service.claim_work_round(task["work_execution_id"])
    assert await service.finish_work_round(
        task_id=task["task_id"],
        work_execution_id=task["work_execution_id"],
        task_session_id=task["task_session_id"],
        status="completed",
        result={"visible_text": "Initial work is finished."},
        error=None,
        turn_id=None,
        assistant_text="Initial work is finished.",
    )
    idle = await service.finish_m5_round(
        task["task_id"], task["work_execution_id"], report_outcome="delivered"
    )
    assert idle["task_state"] == "idle"
    selection = await _begin(db_pool, origin, "Continue from the next numbered question.")

    accepted = await M5SelectionService(db_pool).confirm_pend_message(
        origin_session_id=origin,
        selection_id=selection["selection_id"],
        version=selection["version"],
        task_id=task["task_id"],
    )

    assert accepted["accepted"] is True
    assert accepted["task_state"] == "working"
    assert accepted["receive_order"] == 1
    assert accepted["work_execution_id"]
    async with db_pool.acquire() as conn:
        pending = await conn.fetchrow(
            "SELECT receive_order,message,claimed_work_execution_id FROM m5_pending_messages WHERE command_id=$1::uuid",
            selection["command_id"],
        )
        work = await conn.fetchrow(
            "SELECT message_id,input,status FROM m5_work_rounds WHERE id=$1::uuid",
            accepted["work_execution_id"],
        )
    assert pending["receive_order"] == 1
    assert pending["message"] == "Continue from the next numbered question."
    assert str(pending["claimed_work_execution_id"]) == accepted["work_execution_id"]
    assert str(work["message_id"]) == accepted["message_id"]
    assert work["input"] == pending["message"]
    assert work["status"] == "claimed"


async def test_confirm_after_source_closes_expires_selection_without_appending(db_pool):
    from services.m5_selection_service import M5SelectionService

    async with db_pool.acquire() as conn:
        origin = await _origin(conn)
    task = await _submit_task(db_pool, origin)
    selection = await _begin(db_pool, origin)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.chat_sessions SET status='archived' WHERE id=$1", origin
        )

    expired = await M5SelectionService(db_pool).confirm_pend_message(
        origin_session_id=origin,
        selection_id=selection["selection_id"],
        version=selection["version"],
        task_id=task["task_id"],
    )

    assert expired["accepted"] is False
    assert expired["error"]["code"] == "SELECTION_EXPIRED"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state,chosen_task_id,version FROM m5_selections WHERE selection_id=$1::uuid",
            selection["selection_id"],
        )
        pending = await conn.fetchval(
            "SELECT count(*) FROM m5_pending_messages WHERE command_id=$1::uuid",
            selection["command_id"],
        )
        action = await conn.fetchrow(
            "SELECT state,result FROM m5_actions WHERE command_id=$1::uuid",
            selection["command_id"],
        )
        events = await conn.fetch(
            "SELECT event_type,payload FROM behavior_events WHERE session_id=$1 AND event_type LIKE 'm5_selection_%' ORDER BY seq",
            origin,
        )
    assert row["state"] == "expired"
    assert row["chosen_task_id"] is None
    assert row["version"] == 2
    assert pending == 0
    assert action["state"] == "failed"
    assert json.loads(events[-1]["payload"])["state"] == "expired"
