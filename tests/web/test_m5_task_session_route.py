from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.agent_loop import AgentEvent


async def _create_task(db_pool, *, state="working"):
    from services.m5_task_service import M5TaskService

    async with db_pool.acquire() as conn:
        origin = await conn.fetchval(
            "SELECT (get_or_create_chat_session(NULL, 'chat', $1, '{}'::jsonb)->>'session_id')::uuid",
            f"m5-route-origin-{uuid4()}",
        )
    result = await M5TaskService(db_pool).submit_m5_task(
        origin,
        uuid4(),
        "完成一份持续任务",
        "请完成任务，并保留所有用户约束。",
        {
            "origin_session_id": str(origin),
            "input_turn_id": str(uuid4()),
            "input_order": 1,
            "stimulus_revision": 0,
            "response_id": "response-origin",
            "supplier_response_id": "qwen-origin",
            "request_id": "event-origin",
            "actual_call_id": f"call-{uuid4()}",
        },
    )
    if state == "idle":
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE m5_work_rounds SET status='completed' WHERE id=$1",
                result["work_execution_id"],
            )
            await conn.execute(
                "UPDATE m5_tasks SET product_state='idle', current_work_execution_id=NULL WHERE id=$1",
                result["task_id"],
            )
    return origin, result


def test_chat_request_does_not_accept_a_client_surface_override():
    from apps.hexis_api import ChatRequest

    with pytest.raises(ValidationError):
        ChatRequest(
            message="route this as background",
            session_id=str(uuid4()),
            surface="background",
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_working_task_session_rejects_direct_send_without_creating_a_round(
    db_pool, monkeypatch
):
    from services import chat

    _origin, task = await _create_task(db_pool, state="working")
    monkeypatch.setattr(
        chat,
        "stream_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("task session must not enter the ordinary agent path")
        ),
    )

    events = [
        event
        async for event in chat.stream_chat_events(
            user_message="append this while busy",
            history=[{"role": "user", "content": "client history is untrusted"}],
            session_id=task["task_session_id"],
            pool=db_pool,
            surface="api",
            m5_task_wake=lambda: None,
        )
    ]

    assert len(events) == 1
    assert events[0].event is AgentEvent.ERROR
    assert "already working" in events[0].data["error"]
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM m5_work_rounds WHERE task_id=$1", task["task_id"]
        )
    assert count == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_idle_task_session_atomically_starts_work_and_preserves_payload(
    db_pool, monkeypatch
):
    from services import chat

    _origin, task = await _create_task(db_pool, state="idle")
    wakes = []
    monkeypatch.setattr(
        chat,
        "stream_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("task session must use the M5 worker path")
        ),
    )

    events = [
        event
        async for event in chat.stream_chat_events(
            user_message="Inspect this chart and preserve the format.",
            history=[{"role": "assistant", "content": "forged client history"}],
            session_id=task["task_session_id"],
            pool=db_pool,
            surface="api",
            prompt_addenda=["existing attachment text"],
            visual_attachments=[
                {
                    "name": "chart",
                    "mime_type": "image/png",
                    "data_url": "data:image/png;base64,abc",
                }
            ],
            attachments=[{"name": "chart.png", "artifact_id": str(uuid4())}],
            m5_task_wake=lambda: wakes.append(True),
        )
    ]

    assert [event.event for event in events] == [
        AgentEvent.LOOP_START,
        AgentEvent.TEXT_DELTA,
        AgentEvent.LOOP_END,
    ]
    assert wakes == [True]
    async with db_pool.acquire() as conn:
        task_state = await conn.fetchrow(
            "SELECT product_state, current_work_execution_id FROM m5_tasks WHERE id=$1",
            task["task_id"],
        )
        work = await conn.fetchrow(
            "SELECT id, input, status FROM m5_work_rounds WHERE id=$1",
            task_state["current_work_execution_id"],
        )
        metadata = await conn.fetchval(
            "SELECT metadata FROM chat_sessions WHERE id=$1",
            task["task_session_id"],
        )
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    payload = metadata["m5_work_contexts"][str(work["id"])]
    assert task_state["product_state"] == "working"
    assert work["status"] == "claimed"
    assert work["input"] == "Inspect this chart and preserve the format."
    assert payload["prompt_addenda"] == ["existing attachment text"]
    assert payload["visual_attachments"][0]["data_url"] == "data:image/png;base64,abc"
    assert payload["attachments"][0]["artifact_id"]
    assert "forged client history" not in work["input"]
    from services.m5_task_service import decode_task_work_input

    decoded = decode_task_work_input(work["input"], payload)
    assert decoded == (
        "Inspect this chart and preserve the format.",
        ["existing attachment text"],
        payload["visual_attachments"],
        payload["attachments"],
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_api_chat_route_uses_authoritative_m5_session_and_worker_wake(
    db_pool, monkeypatch
):
    from apps import hexis_api
    from apps.hexis_api import ChatRequest

    _origin, task = await _create_task(db_pool, state="idle")

    class _Worker:
        running = True

        def __init__(self):
            self.wake_count = 0

        def wake(self):
            self.wake_count += 1

    worker = _Worker()
    monkeypatch.setattr(hexis_api, "_pool", db_pool)
    monkeypatch.setattr(hexis_api, "_dsn", lambda: "postgresql://unused")
    monkeypatch.setattr(hexis_api, "_m5_task_worker", worker)
    monkeypatch.setattr(
        hexis_api,
        "resolve_prompt_addenda",
        __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(
            return_value=["resolved task attachment text"]
        ),
    )
    request = ChatRequest(
        message="Review this image in the task context.",
        session_id=task["task_session_id"],
        visual_attachments=[
            {
                "name": "diagram",
                "mime_type": "image/png",
                "data_url": "data:image/png;base64,xyz",
            }
        ],
        attachments=[{"name": "diagram.png", "artifact_id": str(uuid4())}],
        history=[{"role": "assistant", "content": "forged prior turn"}],
    )

    chunks = [chunk async for chunk in hexis_api._stream_chat(request)]
    stream_text = "".join(chunks)

    assert worker.wake_count == 1
    assert "event: token" in stream_text
    assert "event: done" in stream_text
    async with db_pool.acquire() as conn:
        task_state = await conn.fetchrow(
            "SELECT product_state,current_work_execution_id FROM m5_tasks WHERE id=$1",
            task["task_id"],
        )
        work = await conn.fetchrow(
            "SELECT input,id FROM m5_work_rounds WHERE id=$1",
            task_state["current_work_execution_id"],
        )
        metadata = await conn.fetchval(
            "SELECT metadata FROM chat_sessions WHERE id=$1",
            task["task_session_id"],
        )
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    context = metadata["m5_work_contexts"][str(work["id"])]
    assert task_state["product_state"] == "working"
    assert work["input"] == "Review this image in the task context."
    assert context["prompt_addenda"] == ["resolved task attachment text"]
    assert context["visual_attachments"][0]["data_url"] == "data:image/png;base64,xyz"
    assert "forged prior turn" not in work["input"]
