from __future__ import annotations

import json
from uuid import uuid4

import pytest


pytestmark = [pytest.mark.asyncio(loop_scope="session")]


async def _completed_round(db_pool):
    from services.m5_task_service import M5TaskService

    service = M5TaskService(db_pool)
    async with db_pool.acquire() as conn:
        origin = await conn.fetchval(
            "SELECT (get_or_create_chat_session(NULL,'chat',$1,'{}'::jsonb)->>'session_id')::uuid",
            f"m5-report-origin-{uuid4()}",
        )
    task = await service.submit_m5_task(
        origin,
        uuid4(),
        "核对订单金额",
        "核对订单金额并说明计算依据。",
        {
            "origin_session_id": str(origin),
            "input_turn_id": str(uuid4()),
            "input_order": 1,
            "stimulus_revision": 0,
            "response_id": "submit-response",
            "supplier_response_id": "submit-qwen-response",
            "request_id": "submit-event",
            "actual_call_id": "submit-call",
        },
    )
    claim = await service.claim_work_round(task["work_execution_id"])
    assert claim is not None
    async with db_pool.acquire() as conn:
        turn_id = await conn.fetchval(
            "INSERT INTO agent_turns (mode,session_id,user_message) VALUES ('chat',$1,'核对') RETURNING id",
            task["task_session_id"],
        )
    assert await service.finish_work_round(
        task_id=task["task_id"],
        work_execution_id=task["work_execution_id"],
        task_session_id=task["task_session_id"],
        status="completed",
        result={"visible_text": "订单金额为 18 元，计算过程已列出。"},
        error=None,
        turn_id=turn_id,
        assistant_text="订单金额为 18 元，计算过程已列出。",
    )
    async with db_pool.acquire() as conn:
        report_id = await conn.fetchval(
            "SELECT report_id FROM public.m5_reports WHERE work_execution_id=$1",
            task["work_execution_id"],
        )
    return service, origin, task, report_id


async def test_report_delivery_binds_completed_work_origin_and_is_idempotent(db_pool):
    from services.m5_report_receiver import M5ReportReceiver

    _, origin, task, report_id = await _completed_round(db_pool)
    assert report_id is not None
    receiver = M5ReportReceiver(db_pool)
    summary = "核对订单金额任务本轮回复：金额为 18 元，计算依据已保存。"

    receipt = await receiver.deliver(report_id, origin, summary, "call-report-1")
    replay = await receiver.deliver(
        report_id, origin, "新措辞不能覆盖已经接受的摘要。", "call-report-replay"
    )

    assert receipt["outcome"] == "delivered"
    assert receipt["report_id"] == str(report_id)
    assert receipt["operation_id"] == str(report_id)
    assert receipt["actual_call_id"] == "call-report-1"
    assert replay["actual_call_id"] == "call-report-replay"
    assert replay["receiver_actual_call_id"] == "call-report-1"
    assert replay["replayed"] is True
    async with db_pool.acquire() as conn:
        saved = await conn.fetchrow(
            "SELECT count(*) AS messages, (array_agg(session_id))[1] AS session_id, min(content) AS content "
            "FROM public.chat_messages WHERE source_message_id=$1",
            f"m5-report:{report_id}",
        )
        report = await conn.fetchrow(
            "SELECT outcome,summary,receiver_receipt FROM public.m5_reports WHERE report_id=$1",
            report_id,
        )
        state = await conn.fetchval(
            "SELECT product_state FROM public.m5_tasks WHERE id=$1", task["task_id"]
        )
    assert saved["messages"] == 1
    assert saved["session_id"] == origin
    assert saved["content"] == summary
    assert report["outcome"] == "delivered"
    assert report["summary"] == summary
    assert json.loads(report["receiver_receipt"])["actual_call_id"] == "call-report-1"
    assert state == "idle"


async def test_report_delivery_rejects_unfinished_or_unavailable_origin(db_pool):
    from services.m5_report_receiver import M5ReportReceiver, ReportDeliveryError

    service, origin, task, report_id = await _completed_round(db_pool)
    receiver = M5ReportReceiver(db_pool)
    with pytest.raises(ReportDeliveryError, match="origin"):
        await receiver.deliver(report_id, uuid4(), "摘要", "wrong-origin-call")

    open_task = await service.submit_m5_task(
        origin,
        uuid4(),
        "未完成工作",
        "不能提前汇报。",
        {
            "origin_session_id": str(origin),
            "input_turn_id": str(uuid4()),
            "input_order": 2,
            "stimulus_revision": 0,
            "response_id": "second-response",
            "supplier_response_id": "second-qwen-response",
            "request_id": "second-event",
            "actual_call_id": "second-call",
        },
    )
    premature_report = uuid4()
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO public.m5_reports(report_id,task_id,origin_session_id,work_execution_id) "
            "VALUES($1,$2,$3,$4)",
            premature_report,
            open_task["task_id"],
            origin,
            open_task["work_execution_id"],
        )
    with pytest.raises(ReportDeliveryError, match="report"):
        await receiver.deliver(premature_report, origin, "摘要", "premature-call")

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE chat_sessions SET status='archived' WHERE id=$1", origin)
    with pytest.raises(ReportDeliveryError, match="origin"):
        await receiver.deliver(report_id, origin, "摘要", "archived-call")
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE m5_reports SET outcome='refused' WHERE report_id=$1", report_id
        )
    await service.finish_m5_round(
        task["task_id"],
        task["work_execution_id"],
        report_outcome="refused",
    )
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE m5_work_rounds SET status='failed',finished_at=clock_timestamp() WHERE id=$1",
            open_task["work_execution_id"],
        )
        await conn.execute(
            "UPDATE m5_reports SET outcome='refused' WHERE work_execution_id=$1",
            open_task["work_execution_id"],
        )
    await service.finish_m5_round(
        open_task["task_id"],
        open_task["work_execution_id"],
        report_outcome="refused",
    )


async def test_report_runner_requests_after_saved_work_and_delivers_real_tool_call(db_pool):
    from services.m5_report_runner import M5ReportRunner

    _, origin, task, report_id = await _completed_round(db_pool)
    calls = []

    async def model_request(config, messages, report_tool):
        calls.append((config, messages, report_tool))
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "provider-call-98",
                        "type": "function",
                        "function": {
                            "name": "report_task_progress",
                            "arguments": '{"summary":"核对订单金额任务本轮回复：金额为 18 元。"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }]
        }

    runner = M5ReportRunner(db_pool, model_request=model_request)
    result = await runner.run_once(
        report_id,
        {
            "provider": "openai-chat-completions-endpoint",
            "model": "qwen-plus",
            "endpoint": "https://selected.example/v1",
            "api_key": "test-selected-key",
            "timeout_seconds": 12,
        },
    )

    assert result["receipt"]["outcome"] == "delivered"
    assert result["receipt"]["actual_call_id"] == "provider-call-98"
    assert len(calls) == 1
    assert calls[0][0]["model"] == "qwen-plus"
    assert calls[0][2]["function"]["name"] == "report_task_progress"
    prompt_payload = json.loads(calls[0][1][1]["content"])
    assert prompt_payload["task_title"] == "核对订单金额"
    assert prompt_payload["work_result"]["visible_text"] == "订单金额为 18 元，计算过程已列出。"
    async with db_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT product_state FROM m5_tasks WHERE id=$1", task["task_id"]
        ) == "idle"
        assert await conn.fetchval(
            "SELECT last_call_id FROM m5_reports WHERE report_id=$1", report_id
        ) == "provider-call-98"
    assert str(origin) == result["receipt"]["origin_session_id"]


async def test_report_runner_rejects_plain_text_without_delivery(db_pool):
    from services.m5_report_runner import M5ReportRunner

    _, origin, _, report_id = await _completed_round(db_pool)

    async def plain_text(*_args):
        return {"choices": [{"message": {"role": "assistant", "content": "已完成。"}}]}

    runner = M5ReportRunner(db_pool, model_request=plain_text)
    result = await runner.run_once(
        report_id,
        {
            "provider": "openai-chat-completions-endpoint",
            "model": "qwen-plus",
            "endpoint": "https://selected.example/v1",
            "api_key": "test-selected-key",
            "timeout_seconds": 12,
        },
    )
    assert result["outcome"] == "exhausted"
    assert result["code"] == "RETRY_EXHAUSTED"
    async with db_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT outcome FROM m5_reports WHERE report_id=$1", report_id
        ) == "exhausted"
        assert await conn.fetchval(
            "SELECT count(*) FROM chat_messages WHERE session_id=$1 AND source_message_id LIKE 'm5-report:%'",
            origin,
        ) == 0


async def test_task_worker_runs_report_after_work_and_closes_only_after_receipt(db_pool, monkeypatch):
    from types import SimpleNamespace
    from services import m5_task_worker
    from services.m5_report_runner import M5ReportRunner
    from services.m5_task_service import M5TaskService

    service = M5TaskService(db_pool)
    async with db_pool.acquire() as conn:
        origin = await conn.fetchval(
            "SELECT (get_or_create_chat_session(NULL,'chat',$1,'{}'::jsonb)->>'session_id')::uuid",
            f"m5-report-worker-origin-{uuid4()}",
        )
    task = await service.submit_m5_task(
        origin,
        uuid4(),
        "汇总本轮发现",
        "整理并报告本轮发现。",
        {
            "origin_session_id": str(origin),
            "input_turn_id": str(uuid4()),
            "input_order": 1,
            "stimulus_revision": 0,
            "response_id": "worker-submit-response",
            "supplier_response_id": "worker-submit-qwen-response",
            "request_id": "worker-submit-event",
            "actual_call_id": "worker-submit-call",
        },
    )
    turn_id = uuid4()

    async def prepare(*_args, **_kwargs):
        return SimpleNamespace(user_message="整理并报告本轮发现。")

    monkeypatch.setattr(m5_task_worker, "prepare_background_turn", prepare)

    class _Executor:
        async def execute_work(self, *_args, **_kwargs):
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO agent_turns (id,mode,session_id,user_message) VALUES ($1,'chat',$2,'工作')",
                    turn_id,
                    task["task_session_id"],
                )
            return SimpleNamespace(
                turn_id=str(turn_id),
                text="发现已整理。",
                visible_text="发现已整理。",
                tool_calls_made=[],
                tool_results=[],
                iterations=1,
                timed_out=False,
                stopped_reason="completed",
            )

    async def report_model(_config, _messages, _tool):
        async with db_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT status FROM m5_work_rounds WHERE id=$1", task["work_execution_id"]
            ) == "completed"
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "worker-report-call",
                        "function": {
                            "name": "report_task_progress",
                            "arguments": '{"summary":"汇总本轮发现任务本轮回复：发现已整理。"}',
                        },
                    }],
                },
            }]
        }

    async def report_config():
        return {
            "provider": "openai-chat-completions-endpoint",
            "model": "qwen-plus",
            "endpoint": "https://selected.example/v1",
            "api_key": "selected-test-key",
            "timeout_seconds": 10,
        }

    worker = m5_task_worker.M5TaskWorker(
        db_pool,
        executor=_Executor(),
        report_runner=M5ReportRunner(db_pool, model_request=report_model),
        report_config_provider=report_config,
    )
    assert await worker.process_one() is True
    async with db_pool.acquire() as conn:
        state = await conn.fetchval(
            "SELECT product_state FROM m5_tasks WHERE id=$1", task["task_id"]
        )
        report = await conn.fetchrow(
            "SELECT outcome,last_call_id FROM m5_reports WHERE work_execution_id=$1",
            task["work_execution_id"],
        )
        summary_count = await conn.fetchval(
            "SELECT count(*) FROM chat_messages WHERE session_id=$1 AND source_message_id LIKE 'm5-report:%'",
            origin,
        )
    assert state == "idle"
    assert report["outcome"] == "delivered"
    assert report["last_call_id"] == "worker-report-call"
    assert summary_count == 1
