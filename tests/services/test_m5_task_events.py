from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from services.behavior_events import query_behavior_events
from services.m5_task_service import M5TaskService


pytestmark = [pytest.mark.asyncio(loop_scope="session")]


def _authorization(origin, *, task_id=None, label="event"):
    value = {
        "origin_session_id": str(origin),
        "input_turn_id": str(uuid4()),
        "input_order": 1,
        "stimulus_revision": 0,
        "response_id": f"response-{label}-{uuid4()}",
        "supplier_response_id": f"supplier-{label}",
        "request_id": f"request-{label}-{uuid4()}",
        "actual_call_id": f"call-{label}-{uuid4()}",
    }
    if task_id is not None:
        value["task_id"] = str(task_id)
    return value


async def _origin(db_pool, label):
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT (get_or_create_chat_session(NULL,'chat',$1,'{}'::jsonb)->>'session_id')::uuid",
            f"m5-task-events-{label}-{uuid4()}",
        )


async def _events(db_pool, origin):
    return await query_behavior_events(
        db_pool,
        session_id=origin,
        stream_id=f"m5-tasks:{origin}",
        event_type="m5_task_changed",
        limit=100,
    )


async def _submit(service, origin, *, command_id=None, label="submit"):
    return await service.submit_m5_task(
        origin,
        command_id or uuid4(),
        f"Task {label}",
        f"Original request {label}",
        _authorization(origin, label=label),
    )


async def test_submit_event_is_atomic_and_command_replay_is_deduplicated(db_pool):
    service = M5TaskService(db_pool)
    origin = await _origin(db_pool, "submit")
    command_id = uuid4()
    accepted = await _submit(
        service, origin, command_id=command_id, label="submit-replay"
    )
    replay = await service.submit_m5_task(
        origin,
        command_id,
        "Task submit-replay",
        "Original request submit-replay",
        _authorization(origin, label="submit-replay-again"),
    )

    page = await _events(db_pool, origin)
    assert accepted["accepted"] is True
    assert replay["replayed"] is True
    assert len(page["events"]) == 1
    event = page["events"][0]
    assert event["seq"] == 1
    assert event["task_id"] == str(accepted["task_id"])
    assert event["payload"]["origin_session_id"] == str(origin)
    assert event["payload"]["task_id"] == str(accepted["task_id"])
    assert event["payload"]["task_state"] == "working"
    assert event["payload"]["revision"] == 1


async def test_pending_while_working_increments_revision_and_publishes_projection(db_pool):
    service = M5TaskService(db_pool)
    origin = await _origin(db_pool, "pending")
    task = await _submit(service, origin, label="pending")
    command_id = uuid4()
    authorization = _authorization(origin, task_id=task["task_id"], label="pending-message")
    receipt = await service.append_m5_message(
        origin,
        task["task_id"],
        command_id,
        "追加一条待处理要求",
        authorization,
    )
    replay = await service.append_m5_message(
        origin,
        task["task_id"],
        command_id,
        "追加一条待处理要求",
        authorization,
    )
    conflict = await service.append_m5_message(
        origin, task["task_id"], command_id, "追加一条待处理要求",
        _authorization(origin, task_id=task["task_id"], label="different-call"),
    )

    page = await _events(db_pool, origin)
    async with db_pool.acquire() as conn:
        revision = await conn.fetchval(
            "SELECT revision FROM public.m5_tasks WHERE id=$1", task["task_id"]
        )
        pending_count = await conn.fetchval(
            "SELECT count(*) FROM public.m5_pending_messages WHERE task_id=$1 AND claimed_work_execution_id IS NULL",
            task["task_id"],
        )
    assert receipt["accepted"] is True
    assert replay["replayed"] is True
    assert conflict["accepted"] is False
    assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert revision == 2
    assert pending_count == 1
    assert len(page["events"]) == 2
    event = page["events"][-1]
    assert event["seq"] == 2
    assert event["payload"]["revision"] == revision
    assert event["payload"]["pending"] == [
        {
            "message_id": receipt["message_id"],
            "receive_order": 1,
            "message": "追加一条待处理要求",
        }
    ]


async def test_finish_and_idle_session_send_publish_committed_task_states(db_pool):
    service = M5TaskService(db_pool)
    origin = await _origin(db_pool, "lifecycle")
    task = await _submit(service, origin, label="lifecycle")

    claim = await service.claim_work_round(task["work_execution_id"])
    assert claim is not None
    completed = await service.finish_work_round(
        task_id=task["task_id"],
        work_execution_id=task["work_execution_id"],
        task_session_id=task["task_session_id"],
        status="completed",
        result={"visible_text": "Done"},
        error=None,
        turn_id=None,
    )
    assert completed is True
    finished = await service.finish_m5_round(
        task["task_id"], task["work_execution_id"], report_outcome="delivered"
    )
    assert finished["finished"] is True
    assert finished["task_state"] == "idle"

    next_work = await service.create_idle_work_round(
        task["task_session_id"], "继续补充一项"
    )

    page = await _events(db_pool, origin)
    assert next_work["accepted"] is True
    assert [event["seq"] for event in page["events"]] == [1, 2, 3]
    assert [event["payload"]["task_state"] for event in page["events"]] == [
        "working",
        "idle",
        "working",
    ]
    assert [event["payload"]["revision"] for event in page["events"]] == [
        1,
        3,
        4,
    ]
    assert page["events"][-1]["payload"]["pending"] == []


async def test_each_origin_has_an_independent_task_event_stream(db_pool):
    service = M5TaskService(db_pool)
    first_origin = await _origin(db_pool, "first")
    second_origin = await _origin(db_pool, "second")
    first_task = await _submit(service, first_origin, label="first-origin")
    second_task = await _submit(service, second_origin, label="second-origin")

    first_page = await _events(db_pool, first_origin)
    second_page = await _events(db_pool, second_origin)
    assert len(first_page["events"]) == 1
    assert len(second_page["events"]) == 1
    assert first_page["events"][0]["seq"] == 1
    assert second_page["events"][0]["seq"] == 1
    assert first_page["events"][0]["task_id"] == str(first_task["task_id"])
    assert second_page["events"][0]["task_id"] == str(second_task["task_id"])


async def test_concurrent_pending_appends_keep_a_contiguous_stream_and_final_view(db_pool):
    service = M5TaskService(db_pool)
    origin = await _origin(db_pool, "concurrent")
    task = await _submit(service, origin, label="concurrent")

    async def append(index):
        return await service.append_m5_message(
            origin,
            task["task_id"],
            uuid4(),
            f"并发追加 {index}",
            _authorization(origin, task_id=task["task_id"], label=f"parallel-{index}"),
        )

    receipts = await asyncio.gather(*(append(index) for index in range(6)))
    page = await _events(db_pool, origin)
    last = page["events"][-1]
    pending_orders = [message["receive_order"] for message in last["payload"]["pending"]]

    assert all(receipt["accepted"] for receipt in receipts)
    assert [event["seq"] for event in page["events"]] == list(range(1, 8))
    assert last["payload"]["revision"] == 7
    assert pending_orders == list(range(1, 7))
    assert len(last["payload"]["pending"]) == 6


async def test_event_write_failure_rolls_back_task_and_action(db_pool, monkeypatch):
    import services.m5_task_service as task_module

    service = M5TaskService(db_pool)
    origin = await _origin(db_pool, "rollback")
    command_id = uuid4()

    async def fail_event_write(*_args, **_kwargs):
        raise RuntimeError("controlled task event write failure")

    monkeypatch.setattr(
        task_module, "record_behavior_event_on_connection", fail_event_write, raising=False
    )
    with pytest.raises(RuntimeError, match="controlled task event write failure"):
        await _submit(service, origin, command_id=command_id, label="rollback")

    async with db_pool.acquire() as conn:
        action_count = await conn.fetchval(
            "SELECT count(*) FROM public.m5_actions WHERE command_id=$1", command_id
        )
        task_count = await conn.fetchval(
            "SELECT count(*) FROM public.m5_tasks WHERE submit_command_id=$1", command_id
        )
    assert action_count == 0
    assert task_count == 0


async def test_pending_event_failure_rolls_back_message_action_and_revision(
    db_pool, monkeypatch
):
    import services.m5_task_service as task_module

    service = M5TaskService(db_pool)
    origin = await _origin(db_pool, "pending-rollback")
    task = await _submit(service, origin, label="pending-rollback")
    command_id = uuid4()

    async def fail_event_write(*_args, **_kwargs):
        raise RuntimeError("controlled pending event write failure")

    monkeypatch.setattr(
        task_module, "record_behavior_event_on_connection", fail_event_write
    )
    with pytest.raises(RuntimeError, match="controlled pending event write failure"):
        await service.append_m5_message(
            origin,
            task["task_id"],
            command_id,
            "不允许单边保存",
            _authorization(origin, task_id=task["task_id"], label="pending-rollback"),
        )

    async with db_pool.acquire() as conn:
        revision = await conn.fetchval(
            "SELECT revision FROM public.m5_tasks WHERE id=$1", task["task_id"]
        )
        message_count = await conn.fetchval(
            "SELECT count(*) FROM public.m5_pending_messages WHERE command_id=$1",
            command_id,
        )
        action_count = await conn.fetchval(
            "SELECT count(*) FROM public.m5_actions WHERE command_id=$1", command_id
        )
    assert revision == 1
    assert message_count == 0
    assert action_count == 0


async def _completed_report(db_pool, label):
    service = M5TaskService(db_pool)
    origin = await _origin(db_pool, label)
    task = await _submit(service, origin, label=label)
    assert await service.claim_work_round(task["work_execution_id"])
    assert await service.finish_work_round(
        task_id=task["task_id"],
        work_execution_id=task["work_execution_id"],
        task_session_id=task["task_session_id"],
        status="completed",
        result={"visible_text": "已完成受控工作"},
        error=None,
        turn_id=None,
        assistant_text="任务正文已写入专属会话。",
    )
    async with db_pool.acquire() as conn:
        report_id = await conn.fetchval(
            "SELECT report_id FROM public.m5_reports WHERE work_execution_id=$1",
            task["work_execution_id"],
        )
    return service, origin, task, report_id


async def test_report_delivery_emits_one_task_terminal_event_and_replay_emits_none(db_pool):
    from services.m5_report_receiver import M5ReportReceiver

    _service, origin, task, report_id = await _completed_report(db_pool, "report-delivery")
    receiver = M5ReportReceiver(db_pool)
    receipt = await receiver.deliver(
        report_id, origin, "本轮报告已接受。", f"report-call-{uuid4()}"
    )
    replay = await receiver.deliver(
        report_id, origin, "不能替换已接受内容。", f"report-replay-{uuid4()}"
    )

    page = await _events(db_pool, origin)
    assert receipt["outcome"] == replay["outcome"] == "delivered"
    assert replay["replayed"] is True
    assert len(page["events"]) == 2
    assert page["events"][-1]["payload"]["task_id"] == str(task["task_id"])
    assert page["events"][-1]["payload"]["task_state"] == "idle"


async def test_report_event_failure_rolls_back_delivery_and_task_finish(db_pool, monkeypatch):
    from services import m5_task_service as task_module
    from services.m5_report_receiver import M5ReportReceiver, ReportDeliveryError

    _service, origin, task, report_id = await _completed_report(db_pool, "report-rollback")

    async def fail_event_write(*_args, **_kwargs):
        raise RuntimeError("controlled report task event failure")

    monkeypatch.setattr(
        task_module, "record_behavior_event_on_connection", fail_event_write
    )
    with pytest.raises(ReportDeliveryError):
        await M5ReportReceiver(db_pool).deliver(
            report_id, origin, "本次交付需整体回滚。", f"report-call-{uuid4()}"
        )

    async with db_pool.acquire() as conn:
        report_state = await conn.fetchval(
            "SELECT outcome FROM public.m5_reports WHERE report_id=$1", report_id
        )
        task_state = await conn.fetchval(
            "SELECT product_state FROM public.m5_tasks WHERE id=$1", task["task_id"]
        )
        report_messages = await conn.fetchval(
            "SELECT count(*) FROM public.chat_messages WHERE source_message_id=$1",
            f"m5-report:{report_id}",
        )
    page = await _events(db_pool, origin)
    assert report_state == "pending"
    assert task_state == "working"
    assert report_messages == 0
    assert len(page["events"]) == 1


async def test_terminal_report_refusal_emits_task_state_event_in_report_transaction(db_pool):
    from services.m5_report_runner import M5ReportRunner

    _service, origin, task, report_id = await _completed_report(db_pool, "report-refused")
    result = await M5ReportRunner(db_pool).stop_report(
        report_id,
        outcome="refused",
        code="TOOL_NOT_ALLOWED",
        message="受控终态测试",
        actual_call_id=f"report-call-{uuid4()}",
    )
    page = await _events(db_pool, origin)

    assert result["outcome"] == "refused"
    assert len(page["events"]) == 2
    assert page["events"][-1]["payload"]["task_id"] == str(task["task_id"])
    assert page["events"][-1]["payload"]["task_state"] == "idle"


async def test_snapshot_cursor_and_view_share_one_authoritative_origin(db_pool):
    service = M5TaskService(db_pool)
    origin = await _origin(db_pool, "snapshot")
    task = await _submit(service, origin, label="snapshot")

    snapshot = await service.list_tasks_for_origin(origin)

    assert snapshot["origin_session_id"] == str(origin)
    assert snapshot["event_cursor"]["m5-tasks"] == 1
    assert len(snapshot["tasks"]) == 1
    assert snapshot["tasks"][0] == {
        "task_id": str(task["task_id"]),
        "task_title": "Task snapshot",
        "task_state": "working",
        "task_session_id": str(task["task_session_id"]),
        "revision": 1,
        "pending": [],
    }


async def test_snapshot_rejects_a_missing_or_inactive_origin(db_pool):
    service = M5TaskService(db_pool)
    assert await service.list_tasks_for_origin(uuid4()) is None
