"""Behavior-event bridge tests for the background executor."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from core.agent_loop import (
    AgentEvent,
    AgentEventData,
    AgentLoop,
    AgentLoopConfig,
    AgentLoopResult,
)
from core.cognitive_memory_api import HydratedContext
from core.tools.base import ToolResult
from core.tools.config import ToolsConfig
from core.tools import ToolContext
from services.behavior_events import query_behavior_events
from services.behavior_events import BehaviorEvent
from services.background_executor import BackgroundExecutor, BackgroundTaskRequest
import services.background_executor as background_executor_module

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


def _hydrated_context() -> HydratedContext:
    return HydratedContext(
        memories=[],
        partial_activations=[],
        identity=[],
        worldview=[],
        emotional_state=None,
        goals=None,
        urgent_drives=[],
    )


def _loop_result(
    *,
    text: str = "done",
    tool_results: list[dict[str, Any]] | None = None,
    turn_id: str | None = None,
) -> AgentLoopResult:
    return AgentLoopResult(
        text=text,
        visible_text=text,
        messages=[],
        tool_calls_made=[],
        iterations=1,
        energy_spent=0,
        turn_id=turn_id or str(uuid4()),
        tool_results=tool_results or [],
    )


def _patch_explicit_assembly(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_old_runner(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("background executor must not use run_agent")

    async def fail_appraisal(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("background executor must not run appraisal")

    monkeypatch.setattr("services.agent.run_agent", fail_old_runner)
    monkeypatch.setattr("services.agent.run_subconscious_appraisal", fail_appraisal)
    monkeypatch.setattr(
        "services.background_preparation.load_llm_config",
        AsyncMock(return_value={"provider": "fake-provider", "model": "fake-model"}),
    )
    monkeypatch.setattr(
        "services.background_preparation.get_agent_profile_context",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        "services.background_preparation.CognitiveMemory.hydrate",
        AsyncMock(return_value=_hydrated_context()),
    )
    monkeypatch.setattr(
        "services.background_preparation.render_chat_memory_context_db",
        AsyncMock(return_value="memory context"),
    )
    monkeypatch.setattr(
        "services.background_preparation.render_chat_continuity_context_db",
        AsyncMock(return_value="continuity context"),
    )
    monkeypatch.setattr(
        "services.background_preparation.build_system_prompt",
        AsyncMock(return_value="background system prompt"),
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_agent_loop_tool_events_share_call_id(db_pool):
    events: list[AgentEventData] = []

    async def capture(event: AgentEventData) -> None:
        events.append(event)

    registry = MagicMock()
    registry.pool = db_pool
    registry.get_specs = AsyncMock(return_value=[])
    registry.get_spec = MagicMock(return_value=None)
    registry.execute = AsyncMock(return_value=ToolResult.success_result("ok"))
    registry.preflight_outbound_controls = AsyncMock(return_value=None)
    registry.get_config = AsyncMock(return_value=ToolsConfig())

    config = AgentLoopConfig(
        tool_context=ToolContext.CHAT,
        system_prompt="test prompt",
        llm_config={"provider": "fake", "model": "fake", "api_key": "test"},
        registry=registry,
        pool=db_pool,
        timeout_seconds=10,
        on_event=capture,
    )

    with patch("core.agent_loop.chat_completion") as mock_llm:
        mock_llm.side_effect = [
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_fixed",
                        "name": "recall",
                        "arguments": {"query": "test"},
                    }
                ],
                "raw": None,
            },
            {"content": "Done.", "tool_calls": [], "raw": None},
        ]
        await AgentLoop(config).run("Test")

    starts = [event for event in events if event.event is AgentEvent.TOOL_START]
    results = [event for event in events if event.event is AgentEvent.TOOL_RESULT]
    assert len(starts) == 1
    assert len(results) == 1
    assert starts[0].data["call_id"] == "call_fixed"
    assert results[0].data["call_id"] == "call_fixed"


@pytest.mark.asyncio(loop_scope="session")
async def test_background_executor_records_task_and_tool_events(
    db_pool,
    monkeypatch,
):
    _patch_explicit_assembly(monkeypatch)
    recorded: list[BehaviorEvent] = []

    async def fake_record(pool: Any, event: BehaviorEvent) -> dict[str, Any]:
        recorded.append(event)
        return {
            "event_id": str(event.event_id),
            "seq": len(recorded),
            "deduplicated": False,
        }

    monkeypatch.setattr(
        "services.background_executor.record_behavior_event",
        fake_record,
    )
    async with db_pool.acquire() as conn:
        session_id = await conn.fetchval(
            "INSERT INTO chat_sessions DEFAULT VALUES RETURNING id"
        )
        turn = await conn.fetchval(
            """
            SELECT start_agent_turn(
                'chat', 'record events', $1::uuid, '{"messages":[]}'::jsonb
            )
            """,
            session_id,
        )
    turn_id = UUID(json.loads(turn)["turn_id"])
    loop_result = _loop_result(
        tool_results=[{"name": "recall", "success": True}],
        turn_id=str(turn_id),
    )

    async def fake_agent_run(
        self: AgentLoop,
        user_message: str,
        history: list[dict[str, Any]] | None = None,
        user_content: Any | None = None,
    ) -> AgentLoopResult:
        await self.config.on_event(
            AgentEventData(
                event=AgentEvent.TOOL_START,
                data={
                    "call_id": "call-1",
                    "tool_name": "recall",
                    "arguments": {"query": "context"},
                    "iteration": 1,
                },
            )
        )
        await self.config.on_event(
            AgentEventData(
                event=AgentEvent.TOOL_RESULT,
                data={
                    "call_id": "call-1",
                    "tool_name": "recall",
                    "success": True,
                },
            )
        )
        return loop_result

    monkeypatch.setattr(AgentLoop, "run", fake_agent_run)

    command_id = uuid4()
    executor = BackgroundExecutor(db_pool)
    request = BackgroundTaskRequest(
        session_id=session_id,
        command_id=command_id,
        instruction="record events",
        success_criteria="text_and_tool",
    )
    outcome = await executor.run(request)

    assert [event.event_type for event in recorded] == [
        "task_queued",
        "task_started",
        "TOOL_START",
        "TOOL_RESULT",
        "task_completed",
    ]
    assert [event.producer_seq for event in recorded] == [1, 2, 3, 4, 5]
    assert len({event.event_id for event in recorded}) == 5

    for event in recorded:
        assert isinstance(event, BehaviorEvent)
        assert event.schema_version == 1
        assert event.stream_id == "background-task"
        assert event.session_id == session_id
        assert event.source == "background-executor"
        assert event.producer_id == "background-executor"
        assert event.channel == "background"
        assert event.task_id == outcome["task_id"]
        assert event.payload["execution_id"] == outcome["execution_id"]

    assert [event.call_id for event in recorded[2:4]] == ["call-1", "call-1"]
    assert recorded[-1].turn_id == UUID(loop_result.turn_id)

    async with db_pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM background_tasks WHERE id=$1",
            UUID(outcome["task_id"]),
        )
    assert status == "completed"


@pytest.mark.asyncio(loop_scope="session")
async def test_terminal_event_failure_preserves_real_task_terminal_state(
    db_pool,
    monkeypatch,
):
    _patch_explicit_assembly(monkeypatch)
    recorded: list[BehaviorEvent] = []

    async def failing_terminal_record(
        pool: Any,
        event: BehaviorEvent,
    ) -> dict[str, Any]:
        if event.event_type == "task_completed":
            raise RuntimeError("terminal behavior log unavailable")
        recorded.append(event)
        return {
            "event_id": str(event.event_id),
            "seq": len(recorded),
            "deduplicated": False,
        }

    monkeypatch.setattr(
        "services.background_executor.record_behavior_event",
        failing_terminal_record,
    )

    async def fake_agent_run(
        self: AgentLoop,
        user_message: str,
        history: list[dict[str, Any]] | None = None,
        user_content: Any | None = None,
    ) -> AgentLoopResult:
        return _loop_result()

    monkeypatch.setattr(AgentLoop, "run", fake_agent_run)

    async with db_pool.acquire() as conn:
        session_id = await conn.fetchval(
            "INSERT INTO chat_sessions DEFAULT VALUES RETURNING id"
        )

    command_id = uuid4()
    executor = BackgroundExecutor(db_pool)
    request = BackgroundTaskRequest(
        session_id=session_id,
        command_id=command_id,
        instruction="preserve real terminal state",
        success_criteria="text",
    )

    with pytest.raises(
        RuntimeError,
        match="task_completed event write failed",
    ):
        await executor.run(request)

    assert [event.event_type for event in recorded] == [
        "task_queued",
        "task_started",
    ]

    async with db_pool.acquire() as conn:
        task = await conn.fetchrow(
            """
            SELECT status, error, current_execution_id
            FROM background_tasks WHERE command_id=$1
            """,
            command_id,
        )
        execution = await conn.fetchrow(
            """
            SELECT status, error
            FROM background_task_executions WHERE id=$1
            """,
            task["current_execution_id"],
        )

    assert task["status"] == "completed"
    assert task["error"] is None
    assert execution["status"] == "completed"
    assert execution["error"] is None
    assert all(
        event.event_type not in {"task_completed", "task_failed"}
        for event in recorded
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_terminal_event_retry_replays_missing_terminal_event(
    db_pool,
    monkeypatch,
):
    _patch_explicit_assembly(monkeypatch)
    original_record = background_executor_module.record_behavior_event
    recorded: list[BehaviorEvent] = []
    agent_runs = 0

    async def failing_terminal_record(
        pool: Any,
        event: BehaviorEvent,
    ) -> dict[str, Any]:
        if event.event_type == "task_completed":
            raise RuntimeError("terminal behavior log unavailable")
        recorded.append(event)
        return {
            "event_id": str(event.event_id),
            "seq": len(recorded),
            "deduplicated": False,
        }

    monkeypatch.setattr(
        background_executor_module,
        "record_behavior_event",
        failing_terminal_record,
    )

    async def fake_agent_run(
        self: AgentLoop,
        user_message: str,
        history: list[dict[str, Any]] | None = None,
        user_content: Any | None = None,
    ) -> AgentLoopResult:
        nonlocal agent_runs
        agent_runs += 1
        return _loop_result()

    monkeypatch.setattr(AgentLoop, "run", fake_agent_run)

    async with db_pool.acquire() as conn:
        session_id = await conn.fetchval(
            "INSERT INTO chat_sessions DEFAULT VALUES RETURNING id"
        )

    command_id = uuid4()
    executor = BackgroundExecutor(db_pool)
    request = BackgroundTaskRequest(
        session_id=session_id,
        command_id=command_id,
        instruction="replay missing terminal event",
        success_criteria="text",
    )

    with pytest.raises(
        RuntimeError,
        match="task_completed event write failed",
    ):
        await executor.run(request)

    monkeypatch.setattr(
        background_executor_module,
        "record_behavior_event",
        original_record,
    )
    outcome = await executor.run(request)

    assert agent_runs == 1
    assert outcome["task_updated"] is True
    assert outcome["succeeded"] is True
    assert outcome.get("existing_terminal") is True

    async with db_pool.acquire() as conn:
        task = await conn.fetchrow(
            """
            SELECT id, current_execution_id
            FROM background_tasks WHERE command_id=$1
            """,
            command_id,
        )

    page = await query_behavior_events(
        db_pool,
        session_id=session_id,
        task_id=str(task["id"]),
        limit=100,
    )
    terminal_events = [
        event
        for event in page["events"]
        if event["event_type"] == "task_completed"
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0]["payload"]["execution_id"] == str(
        task["current_execution_id"]
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_event_retry_reuses_event_object_and_id(db_pool, monkeypatch):
    recorded: list[BehaviorEvent] = []

    async def fake_record(pool: Any, event: BehaviorEvent) -> dict[str, Any]:
        recorded.append(event)
        return {
            "event_id": str(event.event_id),
            "seq": len(recorded),
            "deduplicated": len(recorded) > 1,
        }

    monkeypatch.setattr(
        "services.background_executor.record_behavior_event",
        fake_record,
    )

    event = BehaviorEvent(
        event_id=uuid4(),
        schema_version=1,
        event_type="task_started",
        stream_id="background-task",
        session_id=uuid4(),
        source="background-executor",
        producer_id="background-executor",
        producer_seq=7,
        channel="background",
        payload={"execution_id": str(uuid4())},
        task_id=str(uuid4()),
    )
    executor = BackgroundExecutor(db_pool)

    await executor._record_event(event)
    await executor._record_event(event)

    assert recorded == [event, event]
    assert recorded[0].event_id == recorded[1].event_id
    assert recorded[0].producer_seq == recorded[1].producer_seq == 7


@pytest.mark.asyncio(loop_scope="session")
async def test_tool_start_event_failure_fails_task_and_propagates(
    db_pool,
    monkeypatch,
):
    _patch_explicit_assembly(monkeypatch)
    recorded: list[BehaviorEvent] = []

    async def failing_record(pool: Any, event: BehaviorEvent) -> dict[str, Any]:
        if event.event_type == "TOOL_START":
            raise RuntimeError("behavior log unavailable")
        recorded.append(event)
        return {
            "event_id": str(event.event_id),
            "seq": len(recorded),
            "deduplicated": False,
        }

    monkeypatch.setattr(
        "services.background_executor.record_behavior_event",
        failing_record,
    )

    result = _loop_result(
        tool_results=[{"name": "recall", "success": True}],
    )

    async def fake_agent_run(
        self: AgentLoop,
        user_message: str,
        history: list[dict[str, Any]] | None = None,
        user_content: Any | None = None,
    ) -> AgentLoopResult:
        await self.config.on_event(
            AgentEventData(
                event=AgentEvent.TOOL_START,
                data={
                    "call_id": "call-failed",
                    "tool_name": "recall",
                    "arguments": {},
                    "iteration": 1,
                },
            )
        )
        await self.config.on_event(
            AgentEventData(
                event=AgentEvent.TOOL_RESULT,
                data={
                    "call_id": "call-failed",
                    "tool_name": "recall",
                    "success": True,
                },
            )
        )
        return result

    monkeypatch.setattr(AgentLoop, "run", fake_agent_run)

    async with db_pool.acquire() as conn:
        session_id = await conn.fetchval(
            "INSERT INTO chat_sessions DEFAULT VALUES RETURNING id"
        )

    command_id = uuid4()
    executor = BackgroundExecutor(db_pool)
    request = BackgroundTaskRequest(
        session_id=session_id,
        command_id=command_id,
        instruction="must fail on event error",
        success_criteria="text_and_tool",
    )

    with pytest.raises(RuntimeError, match="behavior log unavailable"):
        await executor.run(request)

    assert [event.event_type for event in recorded] == [
        "task_queued",
        "task_started",
        "TOOL_RESULT",
        "task_failed",
    ]

    async with db_pool.acquire() as conn:
        task = await conn.fetchrow(
            """
            SELECT status, error, current_execution_id
            FROM background_tasks WHERE command_id=$1
            """,
            command_id,
        )
        execution = await conn.fetchrow(
            """
            SELECT status, error, turn_id
            FROM background_task_executions WHERE id=$1
            """,
            task["current_execution_id"],
        )

    assert task["status"] == "failed"
    assert "behavior log unavailable" in task["error"]
    assert execution["status"] == "failed"
    assert "behavior log unavailable" in execution["error"]
    assert execution["turn_id"] == UUID(result.turn_id)


@pytest.mark.asyncio(loop_scope="session")
async def test_missing_tool_call_id_fails_task_without_unbound_event(
    db_pool,
    monkeypatch,
):
    _patch_explicit_assembly(monkeypatch)

    async def fake_agent_run(
        self: AgentLoop,
        user_message: str,
        history: list[dict[str, Any]] | None = None,
        user_content: Any | None = None,
    ) -> AgentLoopResult:
        await self.config.on_event(
            AgentEventData(
                event=AgentEvent.TOOL_START,
                data={
                    "tool_name": "recall",
                    "arguments": {},
                    "iteration": 1,
                },
            )
        )
        return _loop_result(
            text="done",
            tool_results=[{"name": "recall", "success": True}],
        )

    monkeypatch.setattr(AgentLoop, "run", fake_agent_run)

    async with db_pool.acquire() as conn:
        session_id = await conn.fetchval(
            "INSERT INTO chat_sessions DEFAULT VALUES RETURNING id"
        )

    command_id = uuid4()
    executor = BackgroundExecutor(db_pool)
    request = BackgroundTaskRequest(
        session_id=session_id,
        command_id=command_id,
        instruction="tool event must carry call_id",
        success_criteria="text_and_tool",
    )

    with pytest.raises(ValueError, match="tool event call_id is required"):
        await executor.run(request)

    async with db_pool.acquire() as conn:
        task = await conn.fetchrow(
            """
            SELECT id, status, error
            FROM background_tasks WHERE command_id=$1
            """,
            command_id,
        )

    page = await query_behavior_events(
        db_pool,
        session_id=session_id,
        task_id=str(task["id"]),
        limit=100,
    )
    tool_events = [
        event
        for event in page["events"]
        if event["event_type"] in {"TOOL_START", "TOOL_RESULT"}
    ]

    assert task["status"] == "failed"
    assert "tool event call_id is required" in task["error"]
    assert tool_events == []
