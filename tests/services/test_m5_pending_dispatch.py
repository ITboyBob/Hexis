from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.voice_realtime_session import VoiceRealtimeSession


pytestmark = [pytest.mark.asyncio(loop_scope="session")]


class _ClosedClient:
    async def close(self):
        return None


async def test_pending_rounds_reuse_task_session_and_wait_for_report_terminal(
    db_pool, monkeypatch
):
    from services import m5_task_worker
    from services.m5_task_service import M5TaskService

    service = M5TaskService(db_pool)
    async with db_pool.acquire() as conn:
        origin = await conn.fetchval(
            "SELECT (get_or_create_chat_session(NULL, 'chat', $1, '{}'::jsonb)->>'session_id')::uuid",
            f"m5-pending-dispatch-{uuid4()}",
        )
    submitted = await service.submit_m5_task(
        origin,
        uuid4(),
        "核算订单总价",
        "请核算订单总价，并保留计算依据。",
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
    messages = ["先给出订单总价。", "再给出计算依据。", "最后合成一句中文总结。"]
    for index, message in enumerate(messages, 1):
        await service.append_m5_message(
            origin,
            submitted["task_id"],
            uuid4(),
            message,
            {
                "origin_session_id": str(origin),
                "task_id": submitted["task_id"],
                "input_turn_id": str(uuid4()),
                "input_order": index + 1,
                "stimulus_revision": 0,
                "response_id": f"local-pend-{index}",
                "supplier_response_id": f"qwen-pend-{index}",
                "request_id": f"event-pend-{index}",
                "actual_call_id": f"call-pend-{index}",
            },
        )

    voice = VoiceRealtimeSession(
        session_id=origin,
        media_session_id="closed-after-pend",
        client=_ClosedClient(),
    )
    await voice.close("manual")
    assert await service.list_pending_messages(submitted["task_id"])

    prepared_messages: list[str] = []
    task_sessions: list[str] = []
    work_executions: list[str] = []
    executed = asyncio.Queue()
    release_first = asyncio.Event()

    async def prepare(_pool, task_session_id, message, _history, **kwargs):
        task_sessions.append(str(task_session_id))
        prepared_messages.append(message)
        return SimpleNamespace(user_message=message)

    class _Executor:
        async def execute_work(self, prepared, *, work_execution_id, on_event=None):
            work_executions.append(str(work_execution_id))
            message = prepared.user_message
            await executed.put(message)
            if message == "核算订单总价":
                await release_first.wait()
            turn_id = uuid4()
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO agent_turns (id,mode,session_id,user_message) VALUES ($1,'chat',$2,$3)",
                    turn_id,
                    submitted["task_session_id"],
                    message,
                )
            return SimpleNamespace(
                turn_id=str(turn_id),
                text=f"处理完成：{message}",
                visible_text=f"处理完成：{message}",
                tool_calls_made=[],
                tool_results=[],
                iterations=1,
                timed_out=False,
                stopped_reason="completed",
            )

    monkeypatch.setattr(m5_task_worker, "prepare_background_turn", prepare)
    worker = m5_task_worker.M5TaskWorker(
        db_pool, executor=_Executor(), poll_interval=0.02
    )
    worker.start()
    try:
        worker.wake()
        assert await asyncio.wait_for(executed.get(), timeout=3) == "核算订单总价"
        async with db_pool.acquire() as conn:
            work_1 = await conn.fetchval(
                "SELECT current_work_execution_id FROM m5_tasks WHERE id=$1",
                submitted["task_id"],
            )
        release_first.set()
        await asyncio.wait_for(worker.wait_idle(), timeout=3)

        report_1 = await worker.finish_m5_round(
            submitted["task_id"], work_1, report_outcome="delivered"
        )
        assert report_1["task_state"] == "working"
        assert await asyncio.wait_for(executed.get(), timeout=3) == messages[0]
        await asyncio.wait_for(worker.wait_idle(), timeout=3)

        work_2 = report_1["next_work_execution_id"]
        report_2 = await worker.finish_m5_round(
            submitted["task_id"], work_2, report_outcome="exhausted"
        )
        assert report_2["task_state"] == "working"
        assert await asyncio.wait_for(executed.get(), timeout=3) == messages[1]
        await asyncio.wait_for(worker.wait_idle(), timeout=3)

        work_3 = report_2["next_work_execution_id"]
        report_3 = await worker.finish_m5_round(
            submitted["task_id"], work_3, report_outcome="refused"
        )
        assert report_3["task_state"] == "working"
        assert await asyncio.wait_for(executed.get(), timeout=3) == messages[2]
        await asyncio.wait_for(worker.wait_idle(), timeout=3)

        round_3 = await worker.finish_m5_round(
            submitted["task_id"], report_3["next_work_execution_id"],
            report_outcome="delivered",
        )
        assert round_3["task_state"] == "idle"
        assert round_3["next_work_execution_id"] is None

        stale = await worker.finish_m5_round(
            submitted["task_id"], work_1, report_outcome="delivered"
        )
        assert stale["finished"] is False
    finally:
        release_first.set()
        await worker.close()

    assert prepared_messages == ["核算订单总价", *messages]
    assert set(task_sessions) == {submitted["task_session_id"]}
    assert len(work_executions) == 4
    assert len(set(work_executions)) == 4
    assert await service.list_pending_messages(submitted["task_id"]) == []
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT receive_order,message,claimed_work_execution_id FROM m5_pending_messages WHERE task_id=$1 ORDER BY receive_order",
            submitted["task_id"],
        )
        state = await conn.fetchval(
            "SELECT product_state FROM m5_tasks WHERE id=$1", submitted["task_id"]
        )
    assert [row["receive_order"] for row in rows] == [1, 2, 3]
    assert [row["message"] for row in rows] == messages
    assert all(row["claimed_work_execution_id"] is not None for row in rows)
    assert state == "idle"
