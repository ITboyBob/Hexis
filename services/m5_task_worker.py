"""Independent, DB-backed worker for accepted M5 task rounds."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from core.cognitive_memory_api import CognitiveMemory
from services.background_executor import BackgroundExecutor
from services.background_preparation import prepare_background_turn
from services.m5_task_service import M5TaskService, decode_task_work_input

logger = logging.getLogger(__name__)


class M5TaskWorker:
    def __init__(
        self,
        pool: Any,
        *,
        executor: Any | None = None,
        poll_interval: float = 0.5,
    ) -> None:
        if pool is None:
            raise ValueError("a database pool is required for M5 task work")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._pool = pool
        self._service = M5TaskService(pool)
        self._executor = executor or BackgroundExecutor(pool)
        self._poll_interval = poll_interval
        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="hexis-m5-task-worker")

    def wake(self) -> None:
        """Wake-up is only a hint; persisted database rows remain authoritative."""
        self._wake_event.set()

    async def wait_idle(self) -> None:
        await self._idle_event.wait()

    async def close(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        task = self._task
        if task is not None:
            await task
        self._task = None

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.clear()
            try:
                processed = await self.process_one()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("M5 worker pass failed")
                processed = False
                self._idle_event.set()
            if processed:
                continue
            self._idle_event.set()
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(), timeout=self._poll_interval
                )
            except asyncio.TimeoutError:
                pass

    async def process_one(self) -> bool:
        work_ids = await self._service.list_claimable_work(limit=1)
        if not work_ids:
            self._idle_event.set()
            return False
        claim = await self._service.claim_work_round(work_ids[0])
        if claim is None:
            return True

        self._idle_event.clear()
        await self._execute_claim(claim)
        self._idle_event.set()
        return True

    async def _execute_claim(self, claim: dict[str, Any]) -> None:
        task_id = UUID(str(claim["task_id"]))
        work_id = UUID(str(claim["work_execution_id"]))
        task_session_id = UUID(str(claim["task_session_id"]))
        origin_session_id = UUID(str(claim["origin_session_id"]))
        user_message, prompt_addenda, visual_attachments, attachments = (
            decode_task_work_input(
                str(claim["input"]), claim.get("input_context")
            )
        )
        execution_started = False
        turn_id: UUID | None = None

        try:
            memory = CognitiveMemory(self._pool)
            history = await memory.hydrate_chat_session(task_session_id)
            await self._service.append_work_input(
                task_id=task_id,
                work_execution_id=work_id,
                task_session_id=task_session_id,
                text=user_message,
                attachments=attachments,
            )
            prepared = await prepare_background_turn(
                self._pool,
                task_session_id,
                user_message,
                history,
                prompt_addenda=prompt_addenda,
                visual_attachments=visual_attachments,
            )
            execution_started = True
            result = await self._executor.execute_work(prepared)
            turn_id = UUID(str(result.turn_id)) if result.turn_id is not None else None
            result_payload = BackgroundExecutor.m5_work_result_payload(result)
            result_text = result_payload["visible_text"] or result_payload["text"]
            successful_terminal = (
                not result.timed_out and result.stopped_reason == "completed"
            )
            status = "completed" if successful_terminal else "unknown"
            error = None if successful_terminal else (
                f"AgentLoop stopped without a certain completion: "
                f"{result.stopped_reason}"
            )
            saved = await self._service.finish_work_round(
                task_id=task_id,
                work_execution_id=work_id,
                task_session_id=task_session_id,
                status=status,
                result=result_payload,
                error=error,
                turn_id=turn_id,
                assistant_text=result_text if successful_terminal else None,
            )
            if not saved:
                logger.error("M5 work round lost its authoritative claim: %s", work_id)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._service.finish_work_round(
                    task_id=task_id,
                    work_execution_id=work_id,
                    task_session_id=task_session_id,
                    status="unknown",
                    result=None,
                    error="M5 API worker shutdown interrupted the work round.",
                    turn_id=turn_id,
                )
            )
            raise
        except Exception as exc:
            status = "unknown" if execution_started else "failed"
            try:
                saved = await self._service.finish_work_round(
                    task_id=task_id,
                    work_execution_id=work_id,
                    task_session_id=task_session_id,
                    status=status,
                    result=None,
                    error=f"{type(exc).__name__}: {exc}",
                    turn_id=turn_id,
                )
                if not saved:
                    logger.error("M5 failed work round lost its claim: %s", work_id)
            except Exception:
                logger.exception("Could not persist M5 work failure: %s", work_id)
            logger.exception(
                "M5 work round failed task=%s work=%s origin=%s",
                task_id,
                work_id,
                origin_session_id,
            )
