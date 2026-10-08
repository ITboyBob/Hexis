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
        async def execute_work(self, actual_prepared, *, work_execution_id, on_event=None):
            assert actual_prepared is prepared
            assert str(work_execution_id) == accepted["work_execution_id"]
            async with db_pool.acquire() as conn:
                input_row = await conn.fetchrow(
                    "SELECT role,content,source_message_id FROM chat_messages "
                    "WHERE session_id=$1::uuid AND source_message_id=$2",
                    accepted["task_session_id"],
                    f"m5-work:{work_execution_id}:input",
                )
                await conn.execute(
                    "UPDATE m5_work_rounds SET turn_id=$2::uuid WHERE id=$1::uuid",
                    work_execution_id,
                    result_turn_id,
                )
            assert input_row is not None
            assert input_row["role"] == "user"
            assert input_row["content"] == "准备一份包含研究问题和时间安排的提纲"
            received["executor_started"] = True
            received["work_execution_id"] = str(work_execution_id)
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
        assert received["work_execution_id"] == accepted["work_execution_id"]
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
            "SELECT role, content, source_message_id FROM chat_messages WHERE session_id=$1 ORDER BY ordinal",
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
    assert task_messages[0]["source_message_id"] == f"m5-work:{accepted['work_execution_id']}:input"
    assert task_messages[1]["content"] == "我按研究问题分组整理了提纲。"
    assert task_messages[1]["source_message_id"] == f"m5-work:{accepted['work_execution_id']}:assistant"
    assert origin_messages == 0
    assert legacy_rows == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_worker_persists_actual_tool_events_while_work_is_running(db_pool, monkeypatch):
    from uuid import UUID

    from services import m5_task_worker
    from services.background_executor import BackgroundExecutor
    from services.m5_task_service import M5TaskService

    service = M5TaskService(db_pool)
    async with db_pool.acquire() as conn:
        origin = await conn.fetchval(
            "SELECT (get_or_create_chat_session(NULL,'chat',$1,'{}'::jsonb)->>'session_id')::uuid",
            f"m5-tool-event-origin-{uuid4()}",
        )
    accepted = await service.submit_m5_task(
        origin,
        uuid4(),
        "检查文件并总结",
        "请检查 README 并给出总结。",
        {
            "origin_session_id": str(origin),
            "input_turn_id": str(uuid4()),
            "input_order": 1,
            "stimulus_revision": 0,
            "response_id": f"response-{uuid4()}",
            "supplier_response_id": f"supplier-{uuid4()}",
            "request_id": f"request-{uuid4()}",
            "actual_call_id": f"submit-{uuid4()}",
        },
    )
    claim = await service.claim_work_round(accepted["work_execution_id"])
    assert claim is not None
    registry = BackgroundExecutor(db_pool)
    actual_registry = await registry._create_registry()
    read_file = actual_registry.get("read_file")
    assert read_file is not None
    original_execute = read_file.execute
    entered, release = asyncio.Event(), asyncio.Event()
    call_id = f"read-file-{uuid4()}"

    async def delayed_execute(arguments, context):
        entered.set()
        await release.wait()
        return await original_execute(arguments, context)

    monkeypatch.setattr(read_file, "execute", delayed_execute)

    async def prepare(_pool, session_id, _message, history, **_kwargs):
        assert str(session_id) == accepted["task_session_id"]
        return SimpleNamespace(
            loop_options={
                "timeout_seconds": 30,
                "temperature": 0.0,
                "max_tokens": 256,
                "is_operator": False,
                "on_approval": None,
            },
            system_prompt="You are a background task worker.",
            llm_config={"provider": "openai", "model": "offline-fixture"},
            registry=actual_registry,
            selection=SimpleNamespace(skills=[], available=[]),
            session_id=str(session_id),
            user_message="请检查 README 并给出总结。",
            history=history,
            user_content="请检查 README 并给出总结。",
        )

    provider_calls = 0

    async def fake_chat_completion(**_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            return {
                "content": "Checking README.",
                "tool_calls": [{"id": call_id, "name": "read_file", "arguments": {"path": "README.md"}}],
                "raw": None,
            }
        return {"content": "README 已检查。", "tool_calls": [], "raw": None}

    monkeypatch.setattr(m5_task_worker, "prepare_background_turn", prepare)
    monkeypatch.setattr("core.agent_loop.chat_completion", fake_chat_completion)
    worker = m5_task_worker.M5TaskWorker(db_pool, executor=registry, poll_interval=0.02)
    execution = None
    try:
        execution = asyncio.create_task(worker._execute_claim(claim))
        await asyncio.wait_for(entered.wait(), 3)
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT event_type,event_id,session_id,task_id,call_id,payload->>'execution_id' AS execution_id "
                "FROM behavior_events WHERE task_id=$1::text AND event_type IN ('TOOL_START','TOOL_RESULT') ORDER BY recorded_at,producer_seq",
                str(accepted["task_id"]),
            )
            work_state = await conn.fetchval(
                "SELECT status FROM m5_work_rounds WHERE id=$1::uuid", accepted["work_execution_id"]
            )
        assert [row["event_type"] for row in rows] == ["TOOL_START"]
        assert work_state == "running"
        assert rows[0]["session_id"] == UUID(accepted["task_session_id"])
        assert rows[0]["task_id"] == accepted["task_id"]
        assert rows[0]["call_id"] == call_id
        assert rows[0]["execution_id"] == accepted["work_execution_id"]

        release.set()
        await asyncio.wait_for(execution, 3)
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT event_type,event_id,session_id,task_id,call_id,payload->>'execution_id' AS execution_id "
                "FROM behavior_events WHERE task_id=$1::text AND event_type IN ('TOOL_START','TOOL_RESULT') ORDER BY recorded_at,producer_seq",
                str(accepted["task_id"]),
            )
            work_state = await conn.fetchval(
                "SELECT status FROM m5_work_rounds WHERE id=$1::uuid", accepted["work_execution_id"]
            )
        assert [row["event_type"] for row in rows] == ["TOOL_START", "TOOL_RESULT"]
        assert len({row["event_id"] for row in rows}) == 2
        assert {row["session_id"] for row in rows} == {UUID(accepted["task_session_id"])}
        assert {row["task_id"] for row in rows} == {accepted["task_id"]}
        assert {row["call_id"] for row in rows} == {call_id}
        assert {row["execution_id"] for row in rows} == {accepted["work_execution_id"]}
        assert work_state == "completed"
    finally:
        release.set()
        if execution is not None:
            await asyncio.gather(execution, return_exceptions=True)
        await worker.close()


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
        async def execute_work(self, prepared, *, work_execution_id, on_event=None):
            assert prepared == "prepared-window-turn"
            assert str(work_execution_id) == next_work["work_execution_id"]
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE m5_work_rounds SET turn_id=$2::uuid WHERE id=$1::uuid",
                    work_execution_id,
                    turn_id,
                )
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


async def test_completed_background_turn_reuses_input_and_filters_tool_memory(
    db_pool, monkeypatch
):
    from core.tools.base import ToolResult
    from core.tools.config import ToolsConfig
    from services import m5_task_worker
    from services.background_executor import BackgroundExecutor
    from services.m5_task_service import M5TaskService

    service = M5TaskService(db_pool)
    async with db_pool.acquire() as conn:
        origin = await conn.fetchval(
            "SELECT (get_or_create_chat_session(NULL,'chat',$1,'{}'::jsonb)->>'session_id')::uuid",
            f"m5-memory-boundary-{uuid4()}",
        )
        contact_before = await conn.fetchval(
            "SELECT last_user_contact FROM heartbeat_state WHERE id=1"
        )
    accepted = await service.submit_m5_task(
        origin,
        uuid4(),
        "Inspect the project files",
        "Please inspect the README and return a concise summary.",
        {
            "origin_session_id": str(origin),
            "input_turn_id": str(uuid4()),
            "input_order": 1,
            "stimulus_revision": 0,
            "response_id": "memory-boundary-response",
            "supplier_response_id": "memory-boundary-provider-response",
            "request_id": "memory-boundary-request",
            "actual_call_id": "memory-boundary-call",
        },
    )
    claim = await service.claim_work_round(accepted["work_execution_id"])
    assert claim is not None
    executions = {"tool": 0, "provider": 0}

    class _Registry:
        def __init__(self):
            self.pool = db_pool

        async def get_specs(self, _context):
            return [{
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a source file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }]

        async def get_config(self):
            return ToolsConfig()

        def get_spec(self, _name):
            return None

        async def execute(self, name, arguments, _context):
            assert name == "read_file"
            assert arguments == {"path": "README.md"}
            executions["tool"] += 1
            return ToolResult.success_result({
                "text": "TOOL_RESULT_SECRET_SENTINEL",
                "details": {"title": "Hexis"},
            })

    registry = _Registry()

    async def prepare(_pool, session_id, user_message, history, **_kwargs):
        assert str(session_id) == accepted["task_session_id"]
        assert user_message == "Inspect the project files"
        return SimpleNamespace(
            loop_options={
                "timeout_seconds": 30,
                "temperature": 0.0,
                "max_tokens": 256,
                "is_operator": False,
                "on_approval": None,
            },
            system_prompt="You are a background task worker.",
            llm_config={"provider": "openai", "model": "offline-fixture"},
            registry=registry,
            selection=SimpleNamespace(skills=[], available=[]),
            session_id=str(session_id),
            user_message=user_message,
            history=history,
            user_content=user_message,
        )

    async def fake_chat_completion(**kwargs):
        executions["provider"] += 1
        if executions["provider"] == 1:
            return {
                "content": "Checking the README.",
                "tool_calls": [{
                    "id": "m5-readme-call",
                    "name": "read_file",
                    "arguments": {"path": "README.md"},
                }],
                "raw": None,
            }
        messages = kwargs["messages"]
        assert any(
            message.get("role") == "tool"
            and "TOOL_RESULT_SECRET_SENTINEL" in str(message.get("content"))
            for message in messages
        )
        return {"content": "The README summary is ready.", "tool_calls": [], "raw": None}

    monkeypatch.setattr(m5_task_worker, "prepare_background_turn", prepare)
    monkeypatch.setattr("core.agent_loop.chat_completion", fake_chat_completion)
    worker = m5_task_worker.M5TaskWorker(
        db_pool,
        executor=BackgroundExecutor(db_pool),
        poll_interval=0.02,
    )
    await worker._execute_claim(claim)

    work_id = accepted["work_execution_id"]
    source_identity = f"m5-work:{work_id}"
    async with db_pool.acquire() as conn:
        messages = await conn.fetch(
            "SELECT role,content,content_blocks,metadata,source_message_id "
            "FROM chat_messages WHERE session_id=$1::uuid ORDER BY ordinal",
            accepted["task_session_id"],
        )
        units = await conn.fetch(
            "SELECT user_text,assistant_text,content,metadata,source_attribution,source_identity "
            "FROM subconscious_units WHERE session_id=$1::uuid AND source_identity=$2",
            accepted["task_session_id"],
            source_identity,
        )
        work = await conn.fetchrow(
            "SELECT status,turn_id FROM m5_work_rounds WHERE id=$1::uuid",
            work_id,
        )
        contact_after = await conn.fetchval(
            "SELECT last_user_contact FROM heartbeat_state WHERE id=1"
        )

    assert executions == {"tool": 1, "provider": 2}
    assert work["status"] == "completed"
    assert work["turn_id"] is not None
    assert [message["source_message_id"] for message in messages if message["role"] == "user"] == [
        f"{source_identity}:input"
    ]
    tool_call = next(message for message in messages if message["source_message_id"] == f"tool:{work['turn_id']}:m5-readme-call:call")
    tool_result = next(message for message in messages if message["source_message_id"] == f"tool:{work['turn_id']}:m5-readme-call:result")
    assistant = next(message for message in messages if message["source_message_id"] == f"{source_identity}:assistant")
    assert tool_call["role"] == "assistant" and tool_call["content"] == ""
    assert tool_result["role"] == "toolResult"
    assert assistant["role"] == "assistant" and assistant["content"] == "The README summary is ready."
    assert len(units) == 1
    assert units[0]["user_text"] == "Inspect the project files"
    assert units[0]["assistant_text"] == "The README summary is ready."
    assert units[0]["source_identity"] == source_identity
    assert "TOOL_RESULT_SECRET_SENTINEL" not in units[0]["content"]
    assert "TOOL_RESULT_SECRET_SENTINEL" not in json.dumps(units[0]["metadata"])
    assert "TOOL_RESULT_SECRET_SENTINEL" not in json.dumps(units[0]["source_attribution"])
    assert contact_after == contact_before


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_m5_work_memory_repair_reuses_turn_without_reexecution(db_pool):
    from services.m5_task_service import M5TaskService

    service = M5TaskService(db_pool)
    async with db_pool.acquire() as conn:
        origin = await conn.fetchval(
            "SELECT (get_or_create_chat_session(NULL,'chat',$1,'{}'::jsonb)->>'session_id')::uuid",
            f"m5-memory-repair-{uuid4()}",
        )
    accepted = await service.submit_m5_task(
        origin,
        uuid4(),
        "Repair the saved conversation memory",
        "Answer without repeating any tools.",
        {
            "origin_session_id": str(origin),
            "input_turn_id": str(uuid4()),
            "input_order": 1,
            "stimulus_revision": 0,
            "response_id": "memory-repair-response",
            "supplier_response_id": "memory-repair-provider-response",
            "request_id": "memory-repair-request",
            "actual_call_id": "memory-repair-call",
        },
    )
    claim = await service.claim_work_round(accepted["work_execution_id"])
    assert claim is not None
    await service.append_work_input(
        task_id=accepted["task_id"],
        work_execution_id=accepted["work_execution_id"],
        task_session_id=accepted["task_session_id"],
        text="Repair the saved conversation memory",
        attachments=[],
    )
    turn_id = uuid4()
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_turns (id,mode,session_id,status,result) "
            "VALUES ($1,'chat',$2,'completed',$3::jsonb)",
            turn_id,
            accepted["task_session_id"],
            json.dumps({
                "text": "The original completed answer.",
                "stopped_reason": "timeout",
                "timed_out": True,
            }),
        )
    assert await service.finish_work_round(
        task_id=accepted["task_id"],
        work_execution_id=accepted["work_execution_id"],
        task_session_id=accepted["task_session_id"],
        status="unknown",
        result={"text": "The original completed answer."},
        error="Injected post-turn memory failure.",
        turn_id=turn_id,
    )

    with pytest.raises(ValueError, match="no completed final response"):
        await service.repair_work_memory(accepted["work_execution_id"])
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_turns SET result=$2::jsonb WHERE id=$1::uuid",
            turn_id,
            json.dumps({
                "text": "The original completed answer.",
                "stopped_reason": "completed",
                "timed_out": False,
            }),
        )

    repaired = await service.repair_work_memory(accepted["work_execution_id"])
    replayed = await service.repair_work_memory(accepted["work_execution_id"])
    async with db_pool.acquire() as conn:
        with pytest.raises(Exception, match="source_identity conflict"):
            await conn.fetchval(
                "SELECT public.record_chat_session_turn($1::uuid,$2,$3,'m5-task',$4::jsonb)",
                accepted["task_session_id"],
                "Repair the saved conversation memory",
                "A different answer must not reuse this source identity.",
                json.dumps({
                    "host_background": True,
                    "source_identity": f"m5-work:{accepted['work_execution_id']}",
                }),
            )

    assert repaired["repaired"] is True
    assert repaired["memory_status"] == "stored"
    assert replayed["memory_status"] == "duplicate"
    async with db_pool.acquire() as conn:
        units = await conn.fetch(
            "SELECT user_text,assistant_text,source_identity FROM subconscious_units "
            "WHERE source_identity=$1",
            f"m5-work:{accepted['work_execution_id']}",
        )
        assistant_count = await conn.fetchval(
            "SELECT count(*) FROM chat_messages WHERE session_id=$1::uuid "
            "AND source_message_id=$2",
            accepted["task_session_id"],
            f"m5-work:{accepted['work_execution_id']}:assistant",
        )
        status = await conn.fetchval(
            "SELECT status FROM m5_work_rounds WHERE id=$1::uuid",
            accepted["work_execution_id"],
        )
    assert len(units) == 1
    assert units[0]["user_text"] == "Repair the saved conversation memory"
    assert units[0]["assistant_text"] == "The original completed answer."
    assert assistant_count == 1
    assert status == "unknown"
