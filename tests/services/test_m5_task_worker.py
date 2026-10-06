from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.voice_realtime_session import VoiceRealtimeSession


pytestmark = [pytest.mark.asyncio(loop_scope="session")]


class _MediaClient:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.mark.asyncio(loop_scope="session")
async def test_interrupted_work_is_marked_unknown_and_never_reclaimed(db_pool):
    from services.m5_task_service import M5TaskService

    async with db_pool.acquire() as conn:
        origin_session_id = await conn.fetchval(
            "SELECT (get_or_create_chat_session(NULL, 'chat', $1, '{}'::jsonb)->>'session_id')::uuid",
            f"m5-worker-crash-{uuid4()}",
        )
    accepted = await M5TaskService(db_pool).submit_m5_task(
        origin_session_id,
        uuid4(),
        "保留完整要求并完成研究",
        "请保留完整要求并完成研究。",
        {
            "origin_session_id": str(origin_session_id),
            "input_turn_id": str(uuid4()),
            "input_order": 1,
            "stimulus_revision": 0,
            "response_id": "local-response",
            "supplier_response_id": "qwen-response",
            "request_id": "qwen-event",
            "actual_call_id": "qwen-call-crash",
        },
    )
    service = M5TaskService(db_pool)
    claimed = await service.claim_work_round(accepted["work_execution_id"])
    assert claimed is not None and claimed["work_status"] == "running"

    assert await service.mark_interrupted_work_unknown() == 1
    assert await service.list_claimable_work() == []
    async with db_pool.acquire() as conn:
        work = await conn.fetchrow(
            "SELECT status, error, finished_at FROM m5_work_rounds WHERE id=$1",
            accepted["work_execution_id"],
        )
        task_state = await conn.fetchval(
            "SELECT product_state FROM m5_tasks WHERE id=$1", accepted["task_id"]
        )
        report_origin = await conn.fetchval(
            "SELECT origin_session_id FROM m5_reports WHERE work_execution_id=$1",
            accepted["work_execution_id"],
        )
    assert work["status"] == "unknown"
    assert "stopped" in work["error"]
    assert work["finished_at"] is not None
    assert task_state == "working"
    assert report_origin == origin_session_id


@pytest.mark.asyncio(loop_scope="session")
async def test_accepted_submit_runs_outside_voice_lifecycle_and_owns_task_session(
    db_pool, monkeypatch
):
    from services import m5_task_worker
    from services.m5_task_service import M5TaskService

    async with db_pool.acquire() as conn:
        origin_session_id = await conn.fetchval(
            "SELECT (get_or_create_chat_session(NULL, 'chat', $1, '{}'::jsonb)->>'session_id')::uuid",
            f"m5-worker-origin-{uuid4()}",
        )
    command_id = uuid4()
    accepted = await M5TaskService(db_pool).submit_m5_task(
        origin_session_id,
        command_id,
        "准备一份包含研究问题和时间安排的提纲",
        "请准备提纲，保留研究问题和时间安排要求。",
        {
            "origin_session_id": str(origin_session_id),
            "input_turn_id": str(uuid4()),
            "input_order": 1,
            "stimulus_revision": 0,
            "response_id": "local-response",
            "supplier_response_id": "qwen-response",
            "request_id": "qwen-event",
            "actual_call_id": "qwen-call-submit",
        },
    )

    started = asyncio.Event()
    release = asyncio.Event()
    received: dict[str, object] = {}
    prepared = object()
    result_turn_id = uuid4()
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_turns (id, mode, session_id, user_message) VALUES ($1, 'chat', $2, 'task')",
            result_turn_id,
            accepted["task_session_id"],
        )

    async def prepare(pool, session_id, user_message, history, **kwargs):
        received["prepare"] = (session_id, user_message, history)
        return prepared

    class _Executor:
        async def execute_work(self, actual_prepared, *, on_event=None):
            assert actual_prepared is prepared
            received["executor_started"] = True
            started.set()
            await release.wait()
            received["result_turn_id"] = str(result_turn_id)
            return SimpleNamespace(
                turn_id=str(result_turn_id),
                text="我按研究问题分组整理了提纲。",
                visible_text="我按研究问题分组整理了提纲。",
                messages=[],
                tool_calls_made=[],
                tool_results=[],
                iterations=1,
                energy_spent=0,
                timed_out=False,
                stopped_reason="completed",
            )

    monkeypatch.setattr(m5_task_worker, "prepare_background_turn", prepare)
    worker = m5_task_worker.M5TaskWorker(
        db_pool,
        executor=_Executor(),
        poll_interval=0.02,
    )
    voice_client = _MediaClient()
    voice_session = VoiceRealtimeSession(
        session_id=origin_session_id,
        media_session_id="media-origin",
        client=voice_client,
    )

    assert accepted["accepted"] is True
    assert received.get("executor_started") is None
    worker.start()
    try:
        worker.wake()
        await asyncio.wait_for(started.wait(), timeout=3)
        assert received["executor_started"] is True
        assert str(received["prepare"][0]) == accepted["task_session_id"]
        assert received["prepare"][1] == "准备一份包含研究问题和时间安排的提纲"

        await voice_session.close("manual")
        assert voice_client.closed is True
        assert worker.running is True
        release.set()
        await asyncio.wait_for(worker.wait_idle(), timeout=3)
    finally:
        release.set()
        await worker.close()

    async with db_pool.acquire() as conn:
        task = await conn.fetchrow(
            "SELECT product_state, task_session_id, current_work_execution_id FROM m5_tasks WHERE id=$1",
            accepted["task_id"],
        )
        work = await conn.fetchrow(
            "SELECT status, turn_id, result FROM m5_work_rounds WHERE id=$1",
            accepted["work_execution_id"],
        )
        task_messages = await conn.fetch(
            "SELECT role, content FROM chat_messages WHERE session_id=$1 ORDER BY ordinal",
            accepted["task_session_id"],
        )
        origin_messages = await conn.fetchval(
            "SELECT count(*) FROM chat_messages WHERE session_id=$1",
            origin_session_id,
        )
        legacy_rows = await conn.fetchval(
            "SELECT count(*) FROM background_tasks WHERE command_id=$1", command_id
        )

    assert task["product_state"] == "working"
    assert str(task["task_session_id"]) == accepted["task_session_id"]
    assert str(task["current_work_execution_id"]) == accepted["work_execution_id"]
    assert work["status"] == "completed"
    assert str(work["turn_id"]) == received["result_turn_id"]
    work_result = json.loads(work["result"])
    assert work_result["visible_text"] == "我按研究问题分组整理了提纲。"
    assert work_result["stopped_reason"] == "completed"
    assert [message["role"] for message in task_messages] == ["user", "assistant"]
    assert task_messages[0]["content"] == "准备一份包含研究问题和时间安排的提纲"
    assert task_messages[1]["content"] == "我按研究问题分组整理了提纲。"
    assert origin_messages == 0
    assert legacy_rows == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_task_window_context_reaches_preparation_then_is_cleared(db_pool, monkeypatch):
    from services import m5_task_worker
    from services.m5_task_service import M5TaskService

    service = M5TaskService(db_pool)
    async with db_pool.acquire() as conn:
        origin = await conn.fetchval(
            "SELECT (get_or_create_chat_session(NULL, 'chat', $1, '{}'::jsonb)->>'session_id')::uuid",
            f"m5-window-origin-{uuid4()}",
        )
    accepted = await service.submit_m5_task(
        origin,
        uuid4(),
        "Create a study guide",
        "Create a study guide with citations.",
        {
            "origin_session_id": str(origin),
            "input_turn_id": str(uuid4()),
            "input_order": 1,
            "stimulus_revision": 0,
            "response_id": "response-window",
            "supplier_response_id": "qwen-window",
            "request_id": "event-window",
            "actual_call_id": "call-window-submit",
        },
    )
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE m5_work_rounds SET status='completed' WHERE id=$1",
            accepted["work_execution_id"],
        )
        await conn.execute(
            "UPDATE m5_tasks SET product_state='idle',current_work_execution_id=NULL WHERE id=$1",
            accepted["task_id"],
        )
    next_work = await service.create_idle_work_round(
        accepted["task_session_id"],
        "Review this chart",
        prompt_addenda=["Existing attachment text"],
        visual_attachments=[
            {"name": "chart", "mime_type": "image/png", "data_url": "data:image/png;base64,abc"}
        ],
        attachments=[{"name": "chart.png", "artifact_id": str(uuid4())}],
    )
    turn_id = uuid4()
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_turns (id,mode,session_id,user_message) VALUES ($1,'chat',$2,'task')",
            turn_id,
            accepted["task_session_id"],
        )

    prepared_inputs = {}

    async def prepare(_pool, session_id, message, history, **kwargs):
        prepared_inputs.update(
            session_id=session_id,
            message=message,
            history=history,
            kwargs=kwargs,
        )
        return "prepared-window-turn"

    started = asyncio.Event()

    class _Executor:
        async def execute_work(self, prepared, *, on_event=None):
            assert prepared == "prepared-window-turn"
            started.set()
            return SimpleNamespace(
                turn_id=str(turn_id),
                text="Chart reviewed.",
                visible_text="Chart reviewed.",
                tool_calls_made=[],
                tool_results=[],
                iterations=1,
                timed_out=False,
                stopped_reason="completed",
            )

    monkeypatch.setattr(m5_task_worker, "prepare_background_turn", prepare)
    worker = m5_task_worker.M5TaskWorker(db_pool, executor=_Executor(), poll_interval=0.02)
    worker.start()
    try:
        worker.wake()
        await asyncio.wait_for(started.wait(), timeout=3)
        await asyncio.wait_for(worker.wait_idle(), timeout=3)
    finally:
        await worker.close()

    assert str(prepared_inputs["session_id"]) == accepted["task_session_id"]
    assert prepared_inputs["message"] == "Review this chart"
    assert prepared_inputs["kwargs"]["prompt_addenda"] == ["Existing attachment text"]
    assert prepared_inputs["kwargs"]["visual_attachments"][0]["data_url"] == "data:image/png;base64,abc"
    assert prepared_inputs["kwargs"]["visual_attachments"][0]["name"] == "chart"
    async with db_pool.acquire() as conn:
        metadata = await conn.fetchval(
            "SELECT metadata FROM chat_sessions WHERE id=$1",
            accepted["task_session_id"],
        )
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert "m5_work_contexts" not in metadata
