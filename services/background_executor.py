"""Explicitly assembled executor for DB-authoritative background tasks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from core.agent_api import get_agent_profile_context
from core.agent_loop import AgentLoop, AgentLoopConfig, AgentLoopResult
from core.cognitive_memory_api import CognitiveMemory, render_chat_memory_context_db
from core.llm_config import load_llm_config
from core.tools import ToolContext, ToolRegistry, create_default_registry
from services.agent import build_system_prompt, render_chat_continuity_context_db


@dataclass(frozen=True)
class BackgroundTaskRequest:
    session_id: UUID
    command_id: UUID
    instruction: str
    success_criteria: str


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

    async def run(self, request: BackgroundTaskRequest) -> dict[str, Any]:
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

        try:
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
            result = await AgentLoop(config).run(user_message)
            succeeded = self._succeeded(result, request.success_criteria)
            turn_id = self._turn_id(result)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            async with self._pool.acquire() as conn:
                await conn.fetchval(
                    "SELECT fail_background_task($1::uuid, $2::uuid, $3::text, $4::uuid)",
                    task_id,
                    execution_id,
                    error,
                    None,
                )
            raise

        if succeeded:
            result_payload = {
                "text": result.visible_text or result.text,
                "tool_results": result.tool_results,
            }
            async with self._pool.acquire() as conn:
                await conn.fetchval(
                    "SELECT complete_background_task($1::uuid, $2::uuid, $3::jsonb, $4::uuid)",
                    task_id,
                    execution_id,
                    json.dumps(result_payload, default=str),
                    turn_id,
                )
        else:
            if result.timed_out:
                error = "timeout"
            elif result.stopped_reason == "completed":
                error = f"success_criteria_not_met:{request.success_criteria}"
            else:
                error = result.stopped_reason or "task_failed"
            async with self._pool.acquire() as conn:
                await conn.fetchval(
                    "SELECT fail_background_task($1::uuid, $2::uuid, $3::text, $4::uuid)",
                    task_id,
                    execution_id,
                    error,
                    turn_id,
                )

        return {
            "task_id": str(task_id),
            "execution_id": str(execution_id),
            "succeeded": succeeded,
        }
