"""Explicitly assembled executor for DB-authoritative background tasks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from core.agent_loop import (
    AgentEvent,
    AgentEventData,
    AgentLoop,
    AgentLoopConfig,
    AgentLoopResult,
)
from core.cognitive_memory_api import CognitiveMemory
from core.llm_config import load_llm_config
from core.tools import ToolContext, ToolRegistry, create_default_registry
from services.behavior_events import BehaviorEvent, record_behavior_event
from services.background_preparation import prepare_background_turn
from services.tool_surface_audit import hash_input_text


_SUCCESS_CRITERIA = {"text", "tool", "text_and_tool"}


@dataclass(frozen=True)
class BackgroundTaskRequest:
    session_id: UUID
    command_id: UUID
    instruction: str
    success_criteria: str


@dataclass
class _EventBridgeState:
    task_id: str
    execution_id: str
    session_id: UUID
    producer_seq: int = 0
    error: Exception | None = None


class _TerminalEventError(RuntimeError):
    def __init__(self, event_type: str, original: Exception) -> None:
        super().__init__(
            f"background task {event_type} event write failed: {original}"
        )
        self.event_type = event_type
        self.original = original


class BackgroundExecutor:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def _load_config(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            return await load_llm_config(conn, "llm.chat", fallback_key="llm")

    async def _create_registry(self) -> ToolRegistry:
        return create_default_registry(self._pool)

    async def execute_work(self, prepared: Any, on_event: Any = None) -> AgentLoopResult:
        """Execute one already prepared background turn with host policy intact."""
        options = prepared.loop_options
        config = AgentLoopConfig(
            tool_context=ToolContext.CHAT,
            system_prompt=prepared.system_prompt,
            llm_config=prepared.llm_config,
            registry=prepared.registry,
            pool=self._pool,
            energy_budget=None,
            timeout_seconds=options["timeout_seconds"],
            temperature=options["temperature"],
            max_tokens=options["max_tokens"],
            session_id=prepared.session_id,
            is_operator=options["is_operator"],
            on_approval=options["on_approval"],
            on_event=on_event,
            allowed_tool_names=None,
            active_skill_names=[skill.name for skill in prepared.selection.skills],
            surface="background",
            tool_surface_input_hash=hash_input_text(prepared.user_message),
            available_skill_count=len(prepared.selection.available),
        )
        return await AgentLoop(config).run(
            prepared.user_message,
            history=prepared.history,
            user_content=prepared.user_content,
        )

    @staticmethod
    def m5_work_result_payload(result: AgentLoopResult) -> dict[str, Any]:
        """Persist the actual work result without copying prompt/config internals."""
        return {
            "text": str(result.text or ""),
            "visible_text": str(result.visible_text or ""),
            "tool_calls_made": [
                {"name": item.get("name"), "call_id": item.get("call_id")}
                for item in (result.tool_calls_made or [])
                if isinstance(item, dict)
            ],
            "tool_results": result.tool_results or [],
            "iterations": int(result.iterations),
            "timed_out": bool(result.timed_out),
            "stopped_reason": str(result.stopped_reason),
        }

    @staticmethod
    def _succeeded(result: AgentLoopResult, criteria: str) -> bool:
        if result.timed_out or result.stopped_reason != "completed":
            return False

        has_text = bool(result.visible_text or result.text)
        has_successful_tool = any(
            item.get("success") for item in result.tool_results
        )

        if criteria == "text":
            return has_text
        if criteria == "tool":
            return has_successful_tool
        if criteria == "text_and_tool":
            return has_text and has_successful_tool
        raise ValueError("unsupported success criteria")

    @staticmethod
    def _turn_id(result: AgentLoopResult) -> UUID | None:
        if result.turn_id is None:
            return None
        return UUID(str(result.turn_id))

    async def _record_event(self, event: BehaviorEvent) -> dict[str, Any]:
        return await record_behavior_event(self._pool, event)

    async def _event_turn_id(self, turn_id: UUID | None) -> UUID | None:
        if turn_id is None:
            return None
        async with self._pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM agent_turns WHERE id=$1)",
                turn_id,
            )
        return turn_id if exists else None

    def _make_task_event(
        self,
        state: _EventBridgeState,
        *,
        event_type: str,
        payload: dict[str, Any],
        turn_id: UUID | None = None,
        call_id: str | None = None,
    ) -> BehaviorEvent:
        state.producer_seq += 1
        event_payload = dict(payload)
        event_payload["execution_id"] = state.execution_id
        return BehaviorEvent(
            event_id=uuid4(),
            schema_version=1,
            event_type=event_type,
            stream_id="background-task",
            session_id=state.session_id,
            source="background-executor",
            producer_id="background-executor",
            producer_seq=state.producer_seq,
            channel="background",
            payload=event_payload,
            ts=datetime.now(timezone.utc),
            turn_id=turn_id,
            task_id=state.task_id,
            call_id=call_id,
        )

    async def _emit_task_event(
        self,
        state: _EventBridgeState,
        *,
        event_type: str,
        payload: dict[str, Any],
        turn_id: UUID | None = None,
        call_id: str | None = None,
    ) -> BehaviorEvent:
        event = self._make_task_event(
            state,
            event_type=event_type,
            payload=payload,
            turn_id=turn_id,
            call_id=call_id,
        )
        await self._record_event(event)
        return event

    async def _on_agent_event(
        self,
        event: AgentEventData,
        state: _EventBridgeState,
    ) -> None:
        if event.event not in {AgentEvent.TOOL_START, AgentEvent.TOOL_RESULT}:
            return

        data = dict(event.data)
        call_id = data.get("call_id")
        if call_id is None:
            if state.error is None:
                state.error = ValueError("tool event call_id is required")
            return

        turn_id: UUID | None = None
        if data.get("turn_id") is not None:
            turn_id = UUID(str(data["turn_id"]))

        try:
            await self._emit_task_event(
                state,
                event_type=event.event.name,
                payload=data,
                turn_id=turn_id,
                call_id=str(call_id),
            )
        except Exception as exc:
            if state.error is None:
                state.error = exc

    async def _fail_execution(
        self,
        task_id: UUID,
        execution_id: UUID,
        error: str,
        turn_id: UUID | None,
    ) -> bool:
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    "SELECT fail_background_task($1::uuid, $2::uuid, $3::text, $4::uuid)",
                    task_id,
                    execution_id,
                    error,
                    turn_id,
                )
            )

    async def _fail_run(
        self,
        state: _EventBridgeState,
        *,
        error: str,
        turn_id: UUID | None,
        original: Exception,
    ) -> None:
        try:
            updated = await self._fail_execution(
                UUID(state.task_id),
                UUID(state.execution_id),
                error,
                turn_id,
            )
        except Exception as fail_exc:
            raise original from fail_exc

        if not updated:
            raise original

        try:
            event_turn_id = await self._event_turn_id(turn_id)
            await self._emit_task_event(
                state,
                event_type="task_failed",
                payload={"error": error},
                turn_id=event_turn_id,
            )
        except Exception as event_exc:
            raise original from event_exc
        raise original

    async def _finish_task(
        self,
        state: _EventBridgeState,
        *,
        criteria_met: bool,
        result: AgentLoopResult,
        criteria: str,
        turn_id: UUID | None,
    ) -> bool:
        result_payload = {
            "text": result.visible_text or result.text,
            "tool_results": result.tool_results,
        }
        if result.timed_out:
            error = "timeout"
        elif result.stopped_reason == "completed":
            error = f"success_criteria_not_met:{criteria}"
        else:
            error = result.stopped_reason or "task_failed"

        async with self._pool.acquire() as conn:
            if criteria_met:
                updated = bool(
                    await conn.fetchval(
                        "SELECT complete_background_task($1::uuid, $2::uuid, $3::jsonb, $4::uuid)",
                        UUID(state.task_id),
                        UUID(state.execution_id),
                        json.dumps(result_payload, default=str),
                        turn_id,
                    )
                )
                event_type = "task_completed"
                event_payload: dict[str, Any] = {"result": result_payload}
            else:
                updated = bool(
                    await conn.fetchval(
                        "SELECT fail_background_task($1::uuid, $2::uuid, $3::text, $4::uuid)",
                        UUID(state.task_id),
                        UUID(state.execution_id),
                        error,
                        turn_id,
                    )
                )
                event_type = "task_failed"
                event_payload = {"error": error}

        if not updated:
            return False

        try:
            event_turn_id = await self._event_turn_id(turn_id)
            await self._emit_task_event(
                state,
                event_type=event_type,
                payload=event_payload,
                turn_id=event_turn_id,
            )
        except Exception as exc:
            raise _TerminalEventError(event_type, exc) from exc
        return True

    async def _replay_terminal_task(
        self,
        task: Any,
        request: BackgroundTaskRequest,
    ) -> dict[str, Any]:
        execution_id = task["current_execution_id"]
        if execution_id is None:
            raise RuntimeError("terminal background task has no execution identity")

        state = _EventBridgeState(
            task_id=str(task["id"]),
            execution_id=str(execution_id),
            session_id=request.session_id,
        )
        event_type = "task_completed" if task["status"] == "completed" else "task_failed"
        payload: dict[str, Any]
        if task["status"] == "completed":
            result = task["result"]
            if isinstance(result, str):
                result = json.loads(result)
            payload = {"result": result or {}}
        else:
            payload = {"error": task["error"] or "task_failed"}

        async with self._pool.acquire() as conn:
            task_id = str(task["id"])
            existing_event = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM behavior_events
                    WHERE task_id=$1
                      AND event_type=$2
                      AND payload->>'execution_id'=$3
                )
                """,
                task_id,
                event_type,
                str(execution_id),
            )
            state.producer_seq = await conn.fetchval(
                """
                SELECT COALESCE(MAX(producer_seq), 0)
                FROM behavior_events WHERE task_id=$1
                """,
                task_id,
            )
            turn_id = await conn.fetchval(
                "SELECT turn_id FROM background_task_executions WHERE id=$1",
                execution_id,
            )

        replayed = False
        if not existing_event:
            event_turn_id = await self._event_turn_id(turn_id)
            await self._emit_task_event(
                state,
                event_type=event_type,
                payload=payload,
                turn_id=event_turn_id,
            )
            replayed = True

        succeeded = task["status"] == "completed"
        return {
            "task_id": str(task["id"]),
            "execution_id": str(execution_id),
            "criteria_met": succeeded,
            "task_updated": True,
            "succeeded": succeeded,
            "existing_terminal": True,
            "terminal_event_replayed": replayed,
        }

    async def run(self, request: BackgroundTaskRequest) -> dict[str, Any]:
        if request.success_criteria not in _SUCCESS_CRITERIA:
            raise ValueError("unsupported success criteria")

        async with self._pool.acquire() as conn:
            task_id = await conn.fetchval(
                "SELECT create_background_task($1::uuid, $2::uuid, $3::jsonb)",
                request.session_id,
                request.command_id,
                json.dumps(
                    {
                        "instruction": request.instruction,
                        "success_criteria": request.success_criteria,
                    }
                ),
            )
            task = await conn.fetchrow(
                """
                SELECT id, status, result, error, current_execution_id
                FROM background_tasks WHERE id=$1
                """,
                task_id,
            )
            if task["status"] in {"completed", "failed"}:
                return await self._replay_terminal_task(task, request)
            if task["status"] == "running":
                return {
                    "task_id": str(task["id"]),
                    "execution_id": str(task["current_execution_id"]),
                    "criteria_met": False,
                    "task_updated": False,
                    "succeeded": False,
                    "existing_terminal": False,
                }
            execution_id = await conn.fetchval(
                "SELECT claim_background_task($1::uuid)", task_id
            )
            if execution_id is None:
                raise RuntimeError("background task could not be claimed")

        state = _EventBridgeState(
            task_id=str(task_id),
            execution_id=str(execution_id),
            session_id=request.session_id,
        )
        turn_id: UUID | None = None
        try:
            await self._emit_task_event(
                state,
                event_type="task_queued",
                payload={"command_id": str(request.command_id)},
            )
            await self._emit_task_event(
                state,
                event_type="task_started",
                payload={"status": "running"},
            )

            memory = CognitiveMemory(self._pool)
            async def on_agent_event(event: AgentEventData) -> None:
                await self._on_agent_event(event, state)

            history = await memory.hydrate_chat_session(request.session_id)
            prepared = await prepare_background_turn(
                self._pool,
                request.session_id,
                request.instruction,
                history,
            )
            result = await self.execute_work(prepared, on_event=on_agent_event)
            turn_id = self._turn_id(result)
            if state.error is not None:
                raise state.error
            criteria_met = self._succeeded(result, request.success_criteria)
            task_updated = await self._finish_task(
                state,
                criteria_met=criteria_met,
                result=result,
                criteria=request.success_criteria,
                turn_id=turn_id,
            )
        except _TerminalEventError:
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            await self._fail_run(
                state,
                error=error,
                turn_id=turn_id,
                original=exc,
            )
            raise AssertionError("unreachable")

        succeeded = criteria_met and task_updated

        return {
            "task_id": str(task_id),
            "execution_id": str(execution_id),
            "criteria_met": criteria_met,
            "task_updated": task_updated,
            "succeeded": succeeded,
        }
