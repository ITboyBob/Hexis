from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest


pytestmark = [pytest.mark.asyncio(loop_scope="session")]
_FAILURE_CASES = Path("/Users/itboybob/.cache/hexis/m5-plan-prep-2026-10-07/report-failure-cases.json")
_CONFIG = {
    "provider": "openai-chat-completions-endpoint",
    "model": "qwen-plus",
    "endpoint": "https://selected.example/v1",
    "api_key": "test-selected-key",
    "timeout_seconds": 12,
}


async def _pending_report(db_pool):
    from services.m5_task_service import M5TaskService

    service = M5TaskService(db_pool)
    async with db_pool.acquire() as conn:
        origin = await conn.fetchval(
            "SELECT (get_or_create_chat_session(NULL,'chat',$1,'{}'::jsonb)->>'session_id')::uuid",
            f"m5-report-failure-{uuid4()}",
        )
    task = await service.submit_m5_task(
        origin,
        uuid4(),
        "核对汇总结果",
        "核对本轮汇总结果。",
        {
            "origin_session_id": str(origin),
            "input_turn_id": str(uuid4()),
            "input_order": 1,
            "stimulus_revision": 0,
            "response_id": "failure-submit-response",
            "supplier_response_id": "failure-submit-qwen-response",
            "request_id": "failure-submit-event",
            "actual_call_id": "failure-submit-call",
        },
    )
    assert await service.claim_work_round(task["work_execution_id"])
    async with db_pool.acquire() as conn:
        turn_id = await conn.fetchval(
            "INSERT INTO agent_turns(mode,session_id,user_message) VALUES('chat',$1,'核对') RETURNING id",
            task["task_session_id"],
        )
    assert await service.finish_work_round(
        task_id=task["task_id"],
        work_execution_id=task["work_execution_id"],
        task_session_id=task["task_session_id"],
        status="completed",
        result={"visible_text": "本轮整理已完成。"},
        error=None,
        turn_id=turn_id,
        assistant_text="本轮整理已完成。",
    )
    async with db_pool.acquire() as conn:
        report_id = await conn.fetchval(
            "SELECT report_id FROM m5_reports WHERE work_execution_id=$1",
            task["work_execution_id"],
        )
    return origin, task, report_id


def _tool_call(call_id: str, arguments: str, name: str = "report_task_progress"):
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }],
            },
        }]
    }


def _summary_call(call_id: str, summary: str):
    return _tool_call(call_id, json.dumps({"summary": summary}, ensure_ascii=False))


async def test_all_prepared_report_failure_scenarios_are_loaded():
    cases = json.loads(_FAILURE_CASES.read_text(encoding="utf-8"))["cases"]
    assert {case["id"] for case in cases} == {
        "parameter-correction",
        "same-rejected-parameters",
        "plain-text",
        "invalid-json",
        "temporary-delivery",
        "lost-receipt",
        "wrong-tool",
        "wrong-origin",
        "origin-unavailable",
        "request-timeout",
        "failure-budget",
        "attempt-budget",
    }


async def test_plain_text_three_times_stops_without_fabricating_call_id(db_pool):
    from services.m5_report_runner import M5ReportRunner

    _, task, report_id = await _pending_report(db_pool)
    requests = []

    async def model_request(_config, messages, _tool):
        requests.append(messages)
        return {"choices": [{"message": {"role": "assistant", "content": "已完成。"}}]}

    result = await M5ReportRunner(db_pool, model_request=model_request).run_once(
        report_id, _CONFIG
    )

    assert result["outcome"] == "exhausted"
    assert result["code"] == "RETRY_EXHAUSTED"
    assert len(requests) == 3
    async with db_pool.acquire() as conn:
        report = await conn.fetchrow(
            "SELECT failure_count,attempt_count,last_call_id,outcome FROM m5_reports WHERE report_id=$1",
            report_id,
        )
        state = await conn.fetchval("SELECT product_state FROM m5_tasks WHERE id=$1", task["task_id"])
    assert report["failure_count"] == 3
    assert report["attempt_count"] == 0
    assert report["last_call_id"] is None
    assert report["outcome"] == "exhausted"
    assert state == "idle"


async def test_attempt_budget_allows_sixteenth_and_never_requests_seventeenth(db_pool):
    from services.m5_report_runner import M5ReportRunner

    _, task, report_id = await _pending_report(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE m5_reports SET attempt_count=15 WHERE report_id=$1", report_id)
    requests = []

    async def invalid_parameters(*args):
        requests.append(args)
        return _tool_call(f"call-{len(requests)}", '{"summary":""}')

    result = await M5ReportRunner(db_pool, model_request=invalid_parameters).run_once(
        report_id, _CONFIG
    )
    assert result["outcome"] == "exhausted"
    assert result["code"] == "TOOL_ATTEMPT_LIMIT"
    assert len(requests) == 1
    async with db_pool.acquire() as conn:
        report = await conn.fetchrow(
            "SELECT failure_count,attempt_count,outcome FROM m5_reports WHERE report_id=$1",
            report_id,
        )
        state = await conn.fetchval("SELECT product_state FROM m5_tasks WHERE id=$1", task["task_id"])
    assert report["attempt_count"] == 16
    assert report["failure_count"] == 1
    assert report["outcome"] == "exhausted"
    assert state == "idle"


async def test_repeated_permanent_parameter_rejection_stops_for_same_signature(db_pool):
    from services.m5_report_runner import M5ReportRunner

    _, _, report_id = await _pending_report(db_pool)
    requests = []

    async def same_rejection(*args):
        requests.append(args)
        return _tool_call(f"distinct-call-{len(requests)}", '{"summary":""}')

    result = await M5ReportRunner(db_pool, model_request=same_rejection).run_once(
        report_id, _CONFIG
    )
    assert result["outcome"] == "refused"
    assert result["code"] == "REPEATED_REJECTION"
    assert len(requests) == 2
    async with db_pool.acquire() as conn:
        report = await conn.fetchrow(
            "SELECT failure_count,attempt_count,rejected_signatures,last_call_id FROM m5_reports WHERE report_id=$1",
            report_id,
        )
    signatures = json.loads(report["rejected_signatures"])
    assert report["failure_count"] == 2
    assert report["attempt_count"] == 2
    assert len(signatures) == 1
    assert report["last_call_id"] == "distinct-call-2"


async def test_parameter_correction_continues_same_report_with_same_call_error_reply(db_pool):
    from services.m5_report_runner import M5ReportRunner

    _, task, report_id = await _pending_report(db_pool)
    requests = []

    async def corrected(_config, messages, _tool):
        requests.append(messages)
        if len(requests) == 1:
            return _tool_call("correction-call-1", '{"summary":""}')
        return _summary_call(
            "correction-call-2",
            "核对汇总结果任务本轮回复：本轮整理已完成。",
        )

    result = await M5ReportRunner(db_pool, model_request=corrected).run_once(
        report_id, _CONFIG
    )
    assert result["outcome"] == "delivered"
    assert len(requests) == 2
    assert requests[1][-1]["role"] == "tool"
    assert requests[1][-1]["tool_call_id"] == "correction-call-1"
    assert "INVALID_ARGUMENTS" in requests[1][-1]["content"]
    async with db_pool.acquire() as conn:
        report = await conn.fetchrow(
            "SELECT failure_count,attempt_count,work_execution_id FROM m5_reports WHERE report_id=$1",
            report_id,
        )
        work_count = await conn.fetchval(
            "SELECT count(*) FROM m5_work_rounds WHERE task_id=$1", task["task_id"]
        )
    assert report["failure_count"] == 1
    assert report["attempt_count"] == 2
    assert str(report["work_execution_id"]) == task["work_execution_id"]
    assert work_count == 1


async def test_temporary_delivery_failure_retries_same_report_then_delivers(db_pool):
    from services.m5_report_runner import M5ReportRunner
    from services.m5_report_receiver import ReportDeliveryError

    origin, _, report_id = await _pending_report(db_pool)
    requests = []

    async def model_request(_config, messages, _tool):
        requests.append(messages)
        return _summary_call(
            f"delivery-call-{len(requests)}",
            "核对汇总结果任务本轮回复：本轮整理已完成。",
        )

    runner = M5ReportRunner(db_pool, model_request=model_request)
    receiver = runner._receiver
    actual_deliver = receiver.deliver
    attempts = 0

    async def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ReportDeliveryError("DELIVERY_TEMPORARY_FAILURE", retryable=True)
        return await actual_deliver(*args, **kwargs)

    receiver.deliver = fail_once
    result = await runner.run_once(report_id, _CONFIG)
    assert result["outcome"] == "delivered"
    assert len(requests) == 2
    assert requests[1][-1]["role"] == "tool"
    assert requests[1][-1]["tool_call_id"] == "delivery-call-1"
    async with db_pool.acquire() as conn:
        report = await conn.fetchrow(
            "SELECT failure_count,attempt_count,report_id FROM m5_reports WHERE report_id=$1",
            report_id,
        )
        count = await conn.fetchval(
            "SELECT count(*) FROM chat_messages WHERE session_id=$1 AND source_message_id=$2",
            origin,
            f"m5-report:{report_id}",
        )
    assert report["failure_count"] == 1
    assert report["attempt_count"] == 2
    assert report["report_id"] == report_id
    assert count == 1


async def test_timeout_invalid_json_and_wrong_tool_have_distinct_stops(db_pool):
    from services.m5_report_runner import M5ReportRunner

    _, timeout_task, timeout_report = await _pending_report(db_pool)
    timeout_calls = 0

    async def timeout(*_args):
        nonlocal timeout_calls
        timeout_calls += 1
        raise asyncio.TimeoutError("selected report timeout")

    timeout_result = await M5ReportRunner(db_pool, model_request=timeout).run_once(
        timeout_report, _CONFIG
    )
    assert timeout_result["outcome"] == "exhausted"
    assert timeout_result["code"] == "RETRY_EXHAUSTED"
    assert timeout_calls == 3
    async with db_pool.acquire() as conn:
        timeout_row = await conn.fetchrow(
            "SELECT last_call_id,last_error FROM m5_reports WHERE report_id=$1", timeout_report
        )
        assert timeout_row["last_call_id"] is None
        assert "MODEL_REQUEST_TIMEOUT" in timeout_row["last_error"]
        assert await conn.fetchval(
            "SELECT product_state FROM m5_tasks WHERE id=$1", timeout_task["task_id"]
        ) == "idle"

    _, _, invalid_json_report = await _pending_report(db_pool)
    bad_json_calls = 0

    async def invalid_json(*_args):
        nonlocal bad_json_calls
        bad_json_calls += 1
        return _tool_call(f"invalid-json-{bad_json_calls}", "{broken")

    invalid_result = await M5ReportRunner(db_pool, model_request=invalid_json).run_once(
        invalid_json_report, _CONFIG
    )
    assert invalid_result["outcome"] == "exhausted"
    assert bad_json_calls == 3
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT attempt_count,last_call_id FROM m5_reports WHERE report_id=$1",
            invalid_json_report,
        )
    assert row["attempt_count"] == 0
    assert row["last_call_id"] == "invalid-json-3"

    _, wrong_tool_task, wrong_tool_report = await _pending_report(db_pool)
    wrong_tool_calls = 0

    async def wrong_tool(*_args):
        nonlocal wrong_tool_calls
        wrong_tool_calls += 1
        return _tool_call("wrong-tool-call", '{"summary":"do not run"}', "submit")

    wrong_result = await M5ReportRunner(db_pool, model_request=wrong_tool).run_once(
        wrong_tool_report, _CONFIG
    )
    assert wrong_result["outcome"] == "refused"
    assert wrong_result["code"] == "TOOL_NOT_ALLOWED"
    assert wrong_tool_calls == 1
    async with db_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT product_state FROM m5_tasks WHERE id=$1", wrong_tool_task["task_id"]
        ) == "idle"


async def test_deterministic_config_error_stops_without_model_request(db_pool):
    from services.m5_report_runner import M5ReportRunner

    _, task, report_id = await _pending_report(db_pool)
    requests = 0

    async def forbidden_request(*_args):
        nonlocal requests
        requests += 1
        raise AssertionError("invalid configuration must fail before a model request")

    result = await M5ReportRunner(db_pool, model_request=forbidden_request).run_once(
        report_id, _CONFIG | {"timeout_seconds": None}
    )
    assert result["outcome"] == "refused"
    assert result["code"] == "CONFIGURATION_INVALID"
    assert requests == 0
    async with db_pool.acquire() as conn:
        state = await conn.fetchval(
            "SELECT product_state FROM m5_tasks WHERE id=$1", task["task_id"]
        )
        report = await conn.fetchrow(
            "SELECT failure_count,attempt_count,outcome FROM m5_reports WHERE report_id=$1",
            report_id,
        )
    assert state == "idle"
    assert report["failure_count"] == 0
    assert report["attempt_count"] == 0


async def test_exhausted_and_refused_reports_release_fifo_successor(db_pool):
    from services.m5_report_runner import M5ReportRunner
    from services.m5_task_service import M5TaskService

    for terminal_kind in ("exhausted", "refused"):
        service = M5TaskService(db_pool)
        origin, task, report_id = await _pending_report(db_pool)
        await service.append_m5_message(
            origin,
            task["task_id"],
            uuid4(),
            "收口后继续处理。",
            {
                "origin_session_id": str(origin),
                "task_id": task["task_id"],
                "input_turn_id": str(uuid4()),
                "input_order": 2,
                "stimulus_revision": 0,
                "response_id": f"{terminal_kind}-pending-response",
                "supplier_response_id": f"{terminal_kind}-pending-qwen-response",
                "request_id": f"{terminal_kind}-pending-event",
                "actual_call_id": f"{terminal_kind}-pending-call",
            },
        )
        request_count = 0

        async def fail_for_terminal(*_args):
            nonlocal request_count
            request_count += 1
            if terminal_kind == "refused":
                return _tool_call("stop-once", '{"summary":"unused"}', "submit")
            return {"choices": [{"message": {"role": "assistant", "content": "没有工具调用"}}]}

        outcome = await M5ReportRunner(
            db_pool, model_request=fail_for_terminal
        ).run_once(report_id, _CONFIG)
        assert outcome["outcome"] == terminal_kind
        assert request_count == (1 if terminal_kind == "refused" else 3)
        async with db_pool.acquire() as conn:
            task_row = await conn.fetchrow(
                "SELECT product_state,current_work_execution_id FROM m5_tasks WHERE id=$1",
                task["task_id"],
            )
            successor = await conn.fetchrow(
                "SELECT status,message_id FROM m5_work_rounds WHERE id=$1",
                task_row["current_work_execution_id"],
            )
            old_work_status = await conn.fetchval(
                "SELECT status FROM m5_work_rounds WHERE id=$1",
                task["work_execution_id"],
            )
        assert task_row["product_state"] == "working"
        assert str(task_row["current_work_execution_id"]) != task["work_execution_id"]
        assert successor["status"] == "claimed"
        assert successor["message_id"] is not None
        assert old_work_status == "completed"


async def test_wrong_origin_and_unavailable_origin_stop_without_retry(db_pool):
    from services.m5_report_runner import M5ReportRunner
    from services.m5_report_receiver import ReportDeliveryError

    scenarios = [
        ("TARGET_OWNERSHIP_MISMATCH", "wrong-origin"),
        ("ORIGIN_SESSION_UNAVAILABLE", "origin-unavailable"),
    ]
    for code, label in scenarios:
        _, task, report_id = await _pending_report(db_pool)
        requests = 0

        async def valid_report(*_args):
            nonlocal requests
            requests += 1
            return _summary_call(f"{label}-call", "核对汇总结果任务本轮回复：本轮整理已完成。")

        runner = M5ReportRunner(db_pool, model_request=valid_report)

        async def reject(*_args, **_kwargs):
            raise ReportDeliveryError(code, retryable=False)

        runner._receiver.deliver = reject
        result = await runner.run_once(report_id, _CONFIG)
        assert result["outcome"] == "refused"
        assert result["code"] == code
        assert requests == 1
        async with db_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT product_state FROM m5_tasks WHERE id=$1", task["task_id"]
            ) == "idle"
            assert await conn.fetchval(
                "SELECT count(*) FROM chat_messages WHERE session_id=$1 AND source_message_id=$2",
                task["task_session_id"],
                f"m5-report:{report_id}",
            ) == 0
