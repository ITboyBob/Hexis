from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest


pytestmark = [pytest.mark.asyncio(loop_scope="session")]
_CONFIG = {
    "provider": "openai-chat-completions-endpoint",
    "model": "qwen-plus",
    "endpoint": "https://selected.example/v1",
    "api_key": "test-selected-key",
    "timeout_seconds": 12,
}


async def _running_work(db_pool):
    from services.m5_task_service import M5TaskService

    service = M5TaskService(db_pool)
    async with db_pool.acquire() as conn:
        origin = await conn.fetchval(
            "SELECT (get_or_create_chat_session(NULL,'chat',$1,'{}'::jsonb)->>'session_id')::uuid",
            f"m5-report-recovery-{uuid4()}",
        )
    task = await service.submit_m5_task(
        origin,
        uuid4(),
        "恢复本轮摘要",
        "请安全恢复报告，不重复执行工作。",
        {
            "origin_session_id": str(origin),
            "input_turn_id": str(uuid4()),
            "input_order": 1,
            "stimulus_revision": 0,
            "response_id": "recovery-submit-response",
            "supplier_response_id": "recovery-submit-qwen-response",
            "request_id": "recovery-submit-event",
            "actual_call_id": "recovery-submit-call",
        },
    )
    assert await service.claim_work_round(task["work_execution_id"])
    return service, origin, task


def _success(summary: str, call_id: str):
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "tool_calls": [{
                    "id": call_id,
                    "function": {
                        "name": "report_task_progress",
                        "arguments": json.dumps({"summary": summary}, ensure_ascii=False),
                    },
                }],
            },
        }]
    }


async def test_lost_receiver_ack_reads_saved_delivery_without_second_request(db_pool):
    from services.m5_report_runner import M5ReportRunner
    from services.m5_report_receiver import ReportDeliveryError

    service, origin, task = await _running_work(db_pool)
    turn_id = await db_pool.fetchval(
        "INSERT INTO agent_turns(mode,session_id,user_message) VALUES('chat',$1,'工作结果') RETURNING id",
        task["task_session_id"],
    )
    assert await service.finish_work_round(
        task_id=task["task_id"],
        work_execution_id=task["work_execution_id"],
        task_session_id=task["task_session_id"],
        status="completed",
        result={"visible_text": "原工作结果。"},
        error=None,
        turn_id=turn_id,
        assistant_text="原工作结果。",
    )
    report_id = await db_pool.fetchval(
        "SELECT report_id FROM m5_reports WHERE work_execution_id=$1",
        task["work_execution_id"],
    )
    requests = 0

    async def model_request(*_args):
        nonlocal requests
        requests += 1
        return _success("恢复本轮摘要任务本轮回复：原结果。", f"report-call-{requests}")

    runner = M5ReportRunner(db_pool, model_request=model_request)
    real_deliver = runner._receiver.deliver
    deliveries = 0

    async def lose_ack_after_commit(*args, **kwargs):
        nonlocal deliveries
        deliveries += 1
        receipt = await real_deliver(*args, **kwargs)
        if deliveries == 1:
            raise ReportDeliveryError("DELIVERY_TEMPORARY_FAILURE", retryable=True)
        return receipt

    runner._receiver.deliver = lose_ack_after_commit
    recovered = await runner.run_once(report_id, _CONFIG)
    assert recovered["outcome"] == "delivered"
    assert recovered["receipt"]["actual_call_id"] == "report-call-1"
    assert requests == 1
    assert deliveries == 1
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM chat_messages WHERE session_id=$1 AND source_message_id=$2",
            origin,
            f"m5-report:{report_id}",
        )
        state = await conn.fetchval(
            "SELECT product_state FROM m5_tasks WHERE id=$1", task["task_id"]
        )
    assert count == 1
    assert state == "idle"


async def test_startup_pending_report_runs_without_rerunning_unknown_work(db_pool, monkeypatch):
    from services import m5_task_worker
    from services.m5_report_runner import M5ReportRunner

    service, origin, task = await _running_work(db_pool)
    await service.append_m5_message(
        origin,
        task["task_id"],
        uuid4(),
        "工作恢复后处理这条后续消息。",
        {
            "origin_session_id": str(origin),
            "task_id": task["task_id"],
            "input_turn_id": str(uuid4()),
            "input_order": 2,
            "stimulus_revision": 0,
            "response_id": "recovery-pend-response",
            "supplier_response_id": "recovery-pend-qwen-response",
            "request_id": "recovery-pend-event",
            "actual_call_id": "recovery-pend-call",
        },
    )
    assert await service.mark_interrupted_work_unknown() == 1
    executor_calls = 0
    report_calls = 0

    async def model_request(*_args):
        nonlocal report_calls
        report_calls += 1
        return _success("恢复本轮摘要任务本轮回复：工作中断，结果不确定。", "restart-report-call")

    class _Executor:
        async def execute_work(self, *_args, **_kwargs):
            nonlocal executor_calls
            executor_calls += 1
            raise AssertionError("unknown work must never be rerun")

    async def report_config():
        return _CONFIG

    worker = m5_task_worker.M5TaskWorker(
        db_pool,
        executor=_Executor(),
        report_runner=M5ReportRunner(db_pool, model_request=model_request),
        report_config_provider=report_config,
    )
    processed = await worker.process_one()

    assert processed is True
    assert executor_calls == 0
    assert report_calls == 1
    async with db_pool.acquire() as conn:
        work_status = await conn.fetchval(
            "SELECT status FROM m5_work_rounds WHERE id=$1", task["work_execution_id"]
        )
        report = await conn.fetchrow(
            "SELECT outcome,last_call_id FROM m5_reports WHERE work_execution_id=$1",
            task["work_execution_id"],
        )
        state = await conn.fetchval(
            "SELECT product_state FROM m5_tasks WHERE id=$1", task["task_id"]
        )
        current_work = await conn.fetchrow(
            "SELECT id,status,message_id FROM m5_work_rounds WHERE id=(SELECT current_work_execution_id FROM m5_tasks WHERE id=$1)",
            task["task_id"],
        )
        delivered = await conn.fetchval(
            "SELECT count(*) FROM chat_messages WHERE session_id=$1 AND source_message_id LIKE 'm5-report:%'",
            origin,
        )
    assert work_status == "unknown"
    assert report["outcome"] == "delivered"
    assert report["last_call_id"] == "restart-report-call"
    assert state == "working"
    assert current_work["status"] == "claimed"
    assert current_work["message_id"] is not None
    assert delivered == 1
