"""Explicitly assembled executor for DB-authoritative background tasks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from core.agent_api import get_agent_profile_context
from core.agent_loop import (
    AgentEvent,
    AgentEventData,
    AgentLoop,
    AgentLoopConfig,
    AgentLoopResult,
)
from core.cognitive_memory_api import CognitiveMemory, render_chat_memory_context_db
from core.llm_config import load_llm_config
from core.tools import ToolContext, ToolRegistry, create_default_registry
from services.agent import build_system_prompt, render_chat_continuity_context_db
from services.behavior_events import BehaviorEvent, record_behavior_event


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


class BackgroundExecutor:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def _load_config(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            return await load_llm_config(conn, "llm.chat", fallback_key="llm")

    async def _create_registry(self) -> ToolRegistry:
        return create_default_registry(self._pool)

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
            transaction = conn.transaction()
            await transaction.start()
            marked_unknown = bool(
                await conn.fetchval(
                    "SELECT mark_background_task_unknown($1::uuid, $2::uuid, $3::text, $4::uuid)",
                    UUID(state.task_id),
                    UUID(state.execution_id),
                    "terminal event pending",
                    turn_id,
                )
            )
            if not marked_unknown:
                await transaction.rollback()
                return False

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
                await transaction.rollback()
                return False

            try:
                event_turn_id = await self._event_turn_id(turn_id)
                await self._emit_task_event(
                    state,
                    event_type=event_type,
                    payload=event_payload,
                    turn_id=event_turn_id,
                )
                await transaction.commit()
                return True
            except Exception:
                await transaction.rollback()
                raise

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

            llm_config = await self._load_config()
            registry = await self._create_registry()
            profile = await get_agent_profile_context(pool=self._pool)
            memory = CognitiveMemory(self._pool)
            context = await memory.hydrate(
                request.instruction,
                memory_limit=10,
                include_partial=True,
                include_identity=True,
                include_worldview=True,
                include_emotional_state=True,
                include_goals=True,
                include_drives=True,
                session_id=str(request.session_id),
            )
            async with self._pool.acquire() as conn:
                memory_context = await render_chat_memory_context_db(
                    conn, context, max_memories=10
                )
                continuity = await render_chat_continuity_context_db(
                    conn,
                    str(request.session_id),
                    strict=True,
                )

            prompt = await build_system_prompt(
                "chat",
                registry,
                profile,
                active_skills=[],
                available_skills=[],
                allowed_tool_names=None,
                surface="background",
            )
            user_message = "\n\n".join(
                part
                for part in (continuity, memory_context, request.instruction)
                if part
            )

            config = AgentLoopConfig(
                tool_context=ToolContext.CHAT,
                system_prompt=prompt,
                llm_config=llm_config,
                registry=registry,
                pool=self._pool,
                energy_budget=None,
                timeout_seconds=300.0,
                max_tokens=4096,
                session_id=str(request.session_id),
                allowed_tool_names=None,
                surface="background",
            )
            async def on_agent_event(event: AgentEventData) -> None:
                await self._on_agent_event(event, state)

            config.on_event = on_agent_event
            result = await AgentLoop(config).run(user_message)
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
