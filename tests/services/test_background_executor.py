"""Background executor wiring tests.

The AgentLoop run stubs here verify assembly and task-state wiring only. They
do not replace the real LLM/tool acceptance run required by Phase 4.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from core.agent_loop import AgentLoop, AgentLoopResult
from core.cognitive_memory_api import HydratedContext
from core.tools import ToolContext
from services.background_executor import BackgroundExecutor, BackgroundTaskRequest


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
    text: str = "",
    visible_text: str = "",
    tool_results: list[dict[str, Any]] | None = None,
    stopped_reason: str = "completed",
    timed_out: bool = False,
    turn_id: str | None = None,
) -> AgentLoopResult:
    return AgentLoopResult(
        text=text,
        messages=[],
        tool_calls_made=[],
        iterations=1,
        energy_spent=0,
        turn_id=turn_id or str(uuid4()),
        visible_text=visible_text,
        tool_results=tool_results or [],
        timed_out=timed_out,
        stopped_reason=stopped_reason,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_background_executor_uses_full_registry_and_no_old_runner(
    db_pool,
    monkeypatch,
):
    async def fail_old_runner(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("background executor must not use run_agent")

    async def fail_appraisal(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("background executor must not run appraisal")

    monkeypatch.setattr("services.agent.run_agent", fail_old_runner)
    monkeypatch.setattr("services.agent.run_subconscious_appraisal", fail_appraisal)

    executor = BackgroundExecutor(db_pool)
    config = await executor._load_config()
    registry = await executor._create_registry()
    names = registry.list_names()

    assert "recall" in names
    assert "remember" in names
    assert config["provider"]
    assert config["model"]


def test_task_result_is_not_loop_completed():
    result = _loop_result()
    assert BackgroundExecutor._succeeded(result, "text") is False

    result.text = "done"
    result.visible_text = "done"
    assert BackgroundExecutor._succeeded(result, "text") is True


def test_success_criteria_use_real_text_and_tool_results():
    text = _loop_result(text="done", visible_text="done")
    assert BackgroundExecutor._succeeded(text, "tool") is False

    tool = _loop_result(
        tool_results=[
            {"name": "recall", "success": True},
            {"name": "remember", "success": False},
        ]
    )
    assert BackgroundExecutor._succeeded(tool, "tool") is True
    assert BackgroundExecutor._succeeded(tool, "text_and_tool") is False

    both = _loop_result(
        text="done",
        visible_text="done",
        tool_results=[{"name": "recall", "success": True}],
    )
    assert BackgroundExecutor._succeeded(both, "text_and_tool") is True

    timeout = _loop_result(text="done", visible_text="done", timed_out=True)
    assert BackgroundExecutor._succeeded(timeout, "text") is False

    stopped = _loop_result(text="done", visible_text="done", stopped_reason="error")
    assert BackgroundExecutor._succeeded(stopped, "text") is False

    with pytest.raises(ValueError, match="unsupported success criteria"):
        BackgroundExecutor._succeeded(both, "unknown")


@pytest.mark.asyncio(loop_scope="session")
async def test_run_explicitly_assembles_real_agent_loop(db_pool, monkeypatch):
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
        AsyncMock(return_value={"persona": {"name": "Hexis"}}),
    )
    hydrate = AsyncMock(return_value=_hydrated_context())
    monkeypatch.setattr(
        "services.background_preparation.CognitiveMemory.hydrate",
        hydrate,
    )
    monkeypatch.setattr(
        "services.background_preparation.render_chat_memory_context_db",
        AsyncMock(return_value="memory context"),
    )
    continuity_calls: list[dict[str, Any]] = []

    async def fake_continuity(
        conn: Any,
        session_id: str | None,
        *,
        exclude_sensitive: bool = False,
        strict: bool = False,
    ) -> str:
        continuity_calls.append(
            {
                "session_id": session_id,
                "exclude_sensitive": exclude_sensitive,
                "strict": strict,
            }
        )
        return "continuity context"

    monkeypatch.setattr(
        "services.background_preparation.render_chat_continuity_context_db",
        fake_continuity,
    )
    prompt_calls: list[dict[str, Any]] = []

    async def fake_prompt(*args: Any, **kwargs: Any) -> str:
        prompt_calls.append({"args": args, "kwargs": kwargs})
        return "background system prompt"

    monkeypatch.setattr(
        "services.background_preparation.build_system_prompt",
        fake_prompt,
    )
    selected = {}
    original_select_skills = __import__(
        "services.background_preparation", fromlist=["select_skills"]
    ).select_skills

    async def capture_selection(*args: Any, **kwargs: Any):
        selected["value"] = await original_select_skills(*args, **kwargs)
        return selected["value"]

    monkeypatch.setattr(
        "services.background_preparation.select_skills", capture_selection
    )
    db_history = [{"role": "assistant", "content": "authoritative db history"}]
    hydrate_history = AsyncMock(return_value=db_history)
    monkeypatch.setattr(
        "services.background_executor.CognitiveMemory.hydrate_chat_session",
        hydrate_history,
    )

    captured: dict[str, Any] = {}

    async def fake_agent_run(
        self: AgentLoop,
        user_message: str,
        history: list[dict[str, Any]] | None = None,
        user_content: Any | None = None,
    ) -> AgentLoopResult:
        captured["config"] = self.config
        captured["user_message"] = user_message
        captured["history"] = history
        captured["user_content"] = user_content
        return _loop_result(
            text="done",
            visible_text="done",
            tool_results=[{"name": "recall", "success": True}],
        )

    monkeypatch.setattr(AgentLoop, "run", fake_agent_run)

    async with db_pool.acquire() as conn:
        session_id = await conn.fetchval(
            "INSERT INTO chat_sessions DEFAULT VALUES RETURNING id"
        )

    executor = BackgroundExecutor(db_pool)
    request = BackgroundTaskRequest(
        session_id=session_id,
        command_id=uuid4(),
        instruction="summarize the current context",
        success_criteria="text_and_tool",
    )
    outcome = await executor.run(request)

    assert outcome["succeeded"] is True
    assert outcome["criteria_met"] is True
    assert outcome["task_updated"] is True
    hydrate_history.assert_awaited_once_with(session_id)
    assert hydrate.await_args.kwargs["session_id"] == str(session_id)
    assert hydrate.await_args.kwargs["include_partial"] is True
    assert hydrate.await_args.kwargs["include_identity"] is True
    assert hydrate.await_args.kwargs["include_worldview"] is True
    assert hydrate.await_args.kwargs["include_emotional_state"] is True
    assert hydrate.await_args.kwargs["include_goals"] is True
    assert hydrate.await_args.kwargs["include_drives"] is True
    assert continuity_calls == [
        {
            "session_id": str(session_id),
            "exclude_sensitive": False,
            "strict": True,
        }
    ]

    prompt_kwargs = prompt_calls[0]["kwargs"]
    assert prompt_kwargs["surface"] == "background"
    assert prompt_kwargs["active_skills"] == selected["value"].skills
    assert prompt_kwargs["available_skills"] == selected["value"].available
    assert prompt_kwargs["allowed_tool_names"] is None

    config = captured["config"]
    assert config is not None
    assert config.tool_context is ToolContext.CHAT
    assert config.system_prompt == "background system prompt"
    assert config.llm_config == {"provider": "fake-provider", "model": "fake-model"}
    assert config.allowed_tool_names is None
    assert config.surface == "background"
    assert config.session_id == str(session_id)
    assert captured["history"] == db_history
    assert "recall" in config.registry.list_names()
    assert "remember" in config.registry.list_names()
    assert captured["user_message"] == (
        "continuity context\n\nmemory context\n\n"
        "[USER MESSAGE]\nsummarize the current context"
    )

    async with db_pool.acquire() as conn:
        task = await conn.fetchrow(
            """
            SELECT status, result, error, current_execution_id, completed_at
            FROM background_tasks WHERE id=$1
            """,
            UUID(outcome["task_id"]),
        )
        execution = await conn.fetchrow(
            """
            SELECT status, result, error, turn_id
            FROM background_task_executions WHERE id=$1
            """,
            UUID(outcome["execution_id"]),
        )

    assert task is not None
    assert task["status"] == "completed"
    assert task["error"] is None
    assert task["completed_at"] is not None
    assert task["current_execution_id"] == UUID(outcome["execution_id"])
    assert json.loads(task["result"])["text"] == "done"
    assert execution["status"] == "completed"
    assert execution["error"] is None


@pytest.mark.asyncio(loop_scope="session")
async def test_strict_continuity_failure_fails_task_and_propagates(
    db_pool,
    monkeypatch,
):
    async def fail_old_runner(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("background executor must not use run_agent")

    async def fail_appraisal(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("background executor must not run appraisal")

    async def fail_agent_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("AgentLoop must not run after continuity failure")

    monkeypatch.setattr("services.agent.run_agent", fail_old_runner)
    monkeypatch.setattr("services.agent.run_subconscious_appraisal", fail_appraisal)
    monkeypatch.setattr(AgentLoop, "run", fail_agent_run)
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

    async def failing_continuity(*args: Any, **kwargs: Any) -> str:
        assert kwargs["strict"] is True
        raise RuntimeError("continuity unavailable")

    monkeypatch.setattr(
        "services.background_preparation.render_chat_continuity_context_db",
        failing_continuity,
    )

    async with db_pool.acquire() as conn:
        session_id = await conn.fetchval(
            "INSERT INTO chat_sessions DEFAULT VALUES RETURNING id"
        )

    executor = BackgroundExecutor(db_pool)
    request = BackgroundTaskRequest(
        session_id=session_id,
        command_id=uuid4(),
        instruction="must not reach the loop",
        success_criteria="text",
    )

    with pytest.raises(RuntimeError, match="continuity unavailable"):
        await executor.run(request)

    async with db_pool.acquire() as conn:
        task = await conn.fetchrow(
            """
            SELECT id, status, error, current_execution_id, completed_at
            FROM background_tasks WHERE session_id=$1
            """,
            session_id,
        )
        assert task is not None
        assert task["status"] == "failed"
        assert "continuity unavailable" in task["error"]
        assert task["completed_at"] is not None

        execution = await conn.fetchrow(
            """
            SELECT status, error
            FROM background_task_executions WHERE id=$1
            """,
            task["current_execution_id"],
        )
        assert execution["status"] == "failed"
        assert "continuity unavailable" in execution["error"]


@pytest.mark.asyncio(loop_scope="session")
async def test_superseded_execution_cannot_report_success(db_pool, monkeypatch):
    _patch_explicit_assembly(monkeypatch)

    async with db_pool.acquire() as conn:
        session_id = await conn.fetchval(
            "INSERT INTO chat_sessions DEFAULT VALUES RETURNING id"
        )

    command_id = uuid4()
    request = BackgroundTaskRequest(
        session_id=session_id,
        command_id=command_id,
        instruction="run while another worker claims",
        success_criteria="text_and_tool",
    )
    replacement_execution_id: dict[str, UUID] = {}

    async def fake_agent_run(
        self: AgentLoop,
        user_message: str,
        history: list[dict[str, Any]] | None = None,
        user_content: Any | None = None,
    ) -> AgentLoopResult:
        async with db_pool.acquire() as conn:
            task_id = await conn.fetchval(
                """
                SELECT id FROM background_tasks
                WHERE session_id=$1 AND command_id=$2
                """,
                session_id,
                command_id,
            )
            replacement_execution_id["value"] = await conn.fetchval(
                "SELECT claim_background_task($1::uuid)", task_id
            )
        return _loop_result(
            text="done",
            visible_text="done",
            tool_results=[{"name": "recall", "success": True}],
        )

    monkeypatch.setattr(AgentLoop, "run", fake_agent_run)

    executor = BackgroundExecutor(db_pool)
    outcome = await executor.run(request)

    assert outcome["criteria_met"] is True
    assert outcome["task_updated"] is False
    assert outcome["succeeded"] is False

    async with db_pool.acquire() as conn:
        task = await conn.fetchrow(
            """
            SELECT status, result, error, current_execution_id, completed_at
            FROM background_tasks WHERE command_id=$1
            """,
            command_id,
        )
        old_execution = await conn.fetchrow(
            """
            SELECT status, result, error, finished_at
            FROM background_task_executions WHERE id=$1
            """,
            UUID(outcome["execution_id"]),
        )

    assert task["status"] == "running"
    assert task["result"] is None
    assert task["error"] is None
    assert task["completed_at"] is None
    assert task["current_execution_id"] == replacement_execution_id["value"]
    assert old_execution["status"] == "running"
    assert old_execution["result"] is None
    assert old_execution["error"] is None
    assert old_execution["finished_at"] is None


@pytest.mark.asyncio(loop_scope="session")
async def test_invalid_success_criteria_fails_before_task_claim(
    db_pool,
    monkeypatch,
):
    async def fail_agent_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("AgentLoop must not run for invalid success criteria")

    monkeypatch.setattr(AgentLoop, "run", fail_agent_run)

    async with db_pool.acquire() as conn:
        session_id = await conn.fetchval(
            "INSERT INTO chat_sessions DEFAULT VALUES RETURNING id"
        )

    command_id = uuid4()
    executor = BackgroundExecutor(db_pool)
    request = BackgroundTaskRequest(
        session_id=session_id,
        command_id=command_id,
        instruction="must not be created",
        success_criteria="unknown",
    )

    with pytest.raises(ValueError, match="unsupported success criteria"):
        await executor.run(request)

    async with db_pool.acquire() as conn:
        task_count = await conn.fetchval(
            "SELECT count(*) FROM background_tasks WHERE command_id=$1",
            command_id,
        )
        execution_count = await conn.fetchval(
            """
            SELECT count(*)
            FROM background_task_executions e
            JOIN background_tasks t ON t.id=e.task_id
            WHERE t.command_id=$1
            """,
            command_id,
        )

    assert task_count == 0
    assert execution_count == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_success_criteria_exception_preserves_turn_id(db_pool, monkeypatch):
    _patch_explicit_assembly(monkeypatch)
    turn_id = uuid4()

    async def fake_agent_run(
        self: AgentLoop,
        user_message: str,
        history: list[dict[str, Any]] | None = None,
        user_content: Any | None = None,
    ) -> AgentLoopResult:
        return _loop_result(
            text="done",
            visible_text="done",
            turn_id=str(turn_id),
        )

    def fail_success_check(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("success criteria evaluator failed")

    monkeypatch.setattr(AgentLoop, "run", fake_agent_run)
    monkeypatch.setattr(BackgroundExecutor, "_succeeded", fail_success_check)

    async with db_pool.acquire() as conn:
        session_id = await conn.fetchval(
            "INSERT INTO chat_sessions DEFAULT VALUES RETURNING id"
        )

    executor = BackgroundExecutor(db_pool)
    request = BackgroundTaskRequest(
        session_id=session_id,
        command_id=uuid4(),
        instruction="preserve the turn identity",
        success_criteria="text",
    )

    with pytest.raises(RuntimeError, match="success criteria evaluator failed"):
        await executor.run(request)

    async with db_pool.acquire() as conn:
        execution = await conn.fetchrow(
            """
            SELECT e.status, e.error, e.turn_id
            FROM background_task_executions e
            JOIN background_tasks t ON t.id=e.task_id
            WHERE t.session_id=$1
            """,
            session_id,
        )

    assert execution["status"] == "failed"
    assert "success criteria evaluator failed" in execution["error"]
    assert execution["turn_id"] == turn_id
