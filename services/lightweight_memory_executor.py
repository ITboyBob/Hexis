"""Persistent, non-AgentLoop execution for M5 remember/add_evidence calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from core.tools import ToolContext, ToolExecutionContext, ToolResult, create_default_registry
from core.tools.base import ToolErrorType
from core.tools.hooks import HookContext, HookEvent
from services.behavior_events import (
    BehaviorEvent,
    _validate as _validate_behavior_event,
    record_behavior_event,
)
from services.m5_tool_contracts import M5ToolContractError, build_receipt, parse_tool_arguments

logger = logging.getLogger(__name__)

LIGHTWEIGHT_TOOL_NAMES = frozenset({"remember", "add_evidence"})
DEFAULT_LIGHTWEIGHT_CONCURRENCY = 4


class _LightweightPreflightError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _argument_digest(tool_name: str, arguments: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"tool": tool_name, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LightweightMemoryExecutor:
    """Queue accepted light actions durably and execute each claimed command once."""

    def __init__(
        self,
        pool: Any,
        *,
        registry: Any | None = None,
        max_concurrency: int = DEFAULT_LIGHTWEIGHT_CONCURRENCY,
        poll_interval: float = 0.25,
    ) -> None:
        if pool is None:
            raise ValueError("a database pool is required for lightweight memory actions")
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or not 1 <= max_concurrency <= DEFAULT_LIGHTWEIGHT_CONCURRENCY
        ):
            raise ValueError("lightweight concurrency must be between 1 and 4")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._pool = pool
        self._registry = registry
        self._max_concurrency = max_concurrency
        self._poll_interval = poll_interval
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._active: set[asyncio.Task[None]] = set()
        self._producer_id = f"m5-lightweight-{uuid4()}"
        self._producer_seq = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    async def submit_lightweight(
        self, call: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist an accepted action before the worker can execute its effect."""
        tool_name = getattr(call, "name", None)
        if tool_name not in LIGHTWEIGHT_TOOL_NAMES:
            return self._receipt(
                call,
                ok=False,
                state="failed",
                error={
                    "code": "TOOL_NOT_ALLOWED",
                    "message": "Only remember and add_evidence use the lightweight M5 executor.",
                    "retryable": False,
                },
            )
        if getattr(call, "tool_surface", None) != "m5":
            return self._receipt(
                call,
                ok=False,
                state="failed",
                error={
                    "code": "TOOL_NOT_ALLOWED",
                    "message": "Lightweight memory actions are available only on the M5 surface.",
                    "retryable": False,
                },
            )
        if not isinstance(arguments, dict):
            return self._receipt(
                call,
                ok=False,
                state="failed",
                error={
                    "code": "INVALID_ARGUMENTS",
                    "message": "Lightweight tool arguments must be an object.",
                    "retryable": False,
                },
            )

        try:
            parsed = parse_tool_arguments(
                tool_name,
                json.dumps(arguments, ensure_ascii=False, allow_nan=False),
            )
            origin_id = UUID(str(call.origin_session_id))
            command_id = UUID(str(call.command_id))
            input_turn_id = str(UUID(str(call.input_turn_id)))
        except (M5ToolContractError, TypeError, ValueError) as exc:
            return self._receipt(
                call,
                ok=False,
                state="failed",
                error={
                    "code": "INVALID_ARGUMENTS",
                    "message": str(exc),
                    "retryable": False,
                },
            )

        actual_call_id = getattr(call, "actual_call_id", None)
        response_id = getattr(call, "response_id", None)
        supplier_response_id = getattr(call, "supplier_response_id", None)
        if not all(
            isinstance(value, str) and value.strip()
            for value in (actual_call_id, response_id, supplier_response_id)
        ):
            return self._receipt(
                call,
                ok=False,
                state="failed",
                error={
                    "code": "UNAUTHORIZED_INPUT",
                    "message": "The host could not bind the lightweight action to an authorized input.",
                    "retryable": False,
                },
            )
        input_order = getattr(call, "input_order", None)
        stimulus_revision = getattr(call, "stimulus_revision", None)
        if (
            isinstance(input_order, bool)
            or not isinstance(input_order, int)
            or input_order < 0
            or isinstance(stimulus_revision, bool)
            or not isinstance(stimulus_revision, int)
            or stimulus_revision < 0
        ):
            return self._receipt(
                call,
                ok=False,
                state="failed",
                error={
                    "code": "UNAUTHORIZED_INPUT",
                    "message": "The authorized input order or revision is invalid.",
                    "retryable": False,
                },
            )

        digest = _argument_digest(tool_name, parsed)
        authorization = {
            "origin_session_id": str(origin_id),
            "command_id": str(command_id),
            "argument_digest": digest,
            "actual_call_id": actual_call_id,
            "input_turn_id": input_turn_id,
            "input_order": input_order,
            "stimulus_revision": stimulus_revision,
            "response_id": response_id,
            "supplier_response_id": supplier_response_id,
            "request_id": getattr(call, "request_id", None),
        }
        try:
            async with self._pool.acquire() as conn:
                raw = await conn.fetchval(
                    "SELECT public.submit_m5_lightweight($1,$2,$3,$4::jsonb,$5::jsonb)",
                    origin_id,
                    command_id,
                    tool_name,
                    json.dumps(parsed, ensure_ascii=False, allow_nan=False),
                    json.dumps(authorization, ensure_ascii=False),
                )
            result = _json(raw)
        except Exception:
            logger.exception("Could not persist M5 lightweight action command=%s", command_id)
            return self._receipt(
                call,
                ok=False,
                state="unknown",
                error={
                    "code": "EXECUTION_UNKNOWN",
                    "message": "The host could not confirm persistence of this lightweight action.",
                    "retryable": False,
                },
            )
        if not isinstance(result, dict):
            return self._receipt(
                call,
                ok=False,
                state="unknown",
                error={
                    "code": "EXECUTION_UNKNOWN",
                    "message": "The lightweight action host returned no durable receipt.",
                    "retryable": False,
                },
            )
        if result.get("state") == "accepted":
            self.wake()
        return result

    @staticmethod
    def _receipt(
        call: Any,
        *,
        ok: bool,
        state: str,
        error: dict[str, Any] | None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        actual_call_id = getattr(call, "actual_call_id", None)
        if not isinstance(actual_call_id, str) or not actual_call_id.strip():
            raise M5ToolContractError("a real supplier call_id is required for a lightweight receipt")
        return build_receipt(
            tool_name=str(getattr(call, "name", "remember")),
            call_id=actual_call_id,
            operation_id=getattr(call, "command_id", None),
            ok=ok,
            state=state,
            data=data or {},
            error=error,
        )

    async def mark_interrupted_unknown(self) -> int:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    SELECT command_id,origin_session_id,tool_name,arguments,
                           "authorization",actual_call_id
                    FROM public.m5_actions
                    WHERE tool_name IN ('remember','add_evidence')
                      AND state='accepted'
                      AND execution_started_at IS NOT NULL
                      AND result->>'state'='accepted'
                    ORDER BY command_id
                    FOR UPDATE
                    """
                )
                recovered = 0
                for row in rows:
                    action = dict(row)
                    receipt = self._error_receipt(
                        action,
                        "EXECUTION_UNKNOWN",
                        "The lightweight worker stopped after claiming this action; it was not rerun.",
                        state="unknown",
                    )
                    result_payload = dict(receipt)
                    result_payload.pop("contract_version", None)
                    result_payload.pop("tool_name", None)
                    result_payload.pop("call_id", None)
                    event = self._make_event(
                        action,
                        "TOOL_RESULT",
                        {
                            "arguments": _json(action.get("arguments")) or {},
                            "success": False,
                            "output": None,
                            "display_output": None,
                            "error": receipt["error"]["message"],
                            "error_type": None,
                            "energy_spent": 0,
                            "duration_seconds": 0.0,
                        },
                    )
                    raw = await conn.fetchval(
                        "SELECT public.finish_m5_lightweight_action($1,$2,$3::jsonb)",
                        UUID(str(action["command_id"])),
                        "unknown",
                        json.dumps(result_payload, ensure_ascii=False, default=str),
                    )
                    persisted = _json(raw)
                    if isinstance(persisted, dict) and not persisted.get("replayed"):
                        await self._record_event_in_transaction(conn, event)
                        recovered += 1
                return recovered

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run_loop(), name="hexis-m5-lightweight-memory-worker"
        )

    def wake(self) -> None:
        self._wake_event.set()

    async def close(self) -> None:
        self._stop_event.set()
        self.wake()
        if self._task is not None:
            await self._task
        self._task = None

    async def process_one(self) -> bool:
        action = await self._claim_one()
        if action is None:
            return False
        async with self._semaphore:
            await self._execute_claim(action)
        return True

    async def _claim_one(self) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            raw = await conn.fetchval("SELECT public.claim_m5_lightweight_action()")
        action = _json(raw)
        return action if isinstance(action, dict) else None

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.clear()
            self._active = {task for task in self._active if not task.done()}
            claimed_any = False
            try:
                while len(self._active) < self._max_concurrency and not self._stop_event.is_set():
                    action = await self._claim_one()
                    if action is None:
                        break
                    claimed_any = True
                    task = asyncio.create_task(self._execute_claim(action))
                    self._active.add(task)
                if self._active:
                    done, _pending = await asyncio.wait(
                        self._active,
                        timeout=self._poll_interval if not claimed_any else None,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        self._active.discard(task)
                        try:
                            await task
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.exception("M5 lightweight action task escaped its terminal handler")
                elif not claimed_any:
                    try:
                        await asyncio.wait_for(
                            self._wake_event.wait(), timeout=self._poll_interval
                        )
                    except asyncio.TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("M5 lightweight worker pass failed")
                try:
                    await asyncio.wait_for(
                        self._wake_event.wait(), timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    pass
        if self._active:
            await asyncio.gather(*self._active, return_exceptions=True)

    async def _record_event_in_transaction(self, conn: Any, event: BehaviorEvent) -> None:
        _validate_behavior_event(event)
        await conn.fetchval(
            """
            SELECT record_behavior_event(
                $1::uuid,$2::int,$3,$4,$5::uuid,$6,$7::int,$8,$9,$10,$11::int,
                $12::uuid,$13,$14,$15::timestamptz,$16,$17,$18::bigint,$19,$20::jsonb
            )
            """,
            event.event_id,
            event.schema_version,
            event.event_type,
            event.stream_id,
            event.session_id,
            event.request_id,
            event.stimulus_revision,
            event.input_turn_id,
            event.response_id,
            event.media_session_id,
            event.output_epoch,
            event.turn_id,
            event.task_id,
            event.call_id,
            event.ts,
            event.source,
            event.producer_id,
            event.producer_seq,
            event.channel,
            json.dumps(event.payload, ensure_ascii=False, default=str),
        )

    async def _validate_evidence_target(self, memory_id: str) -> None:
        try:
            target_id = UUID(memory_id)
        except (TypeError, ValueError) as exc:
            raise _LightweightPreflightError(
                "TARGET_NOT_FOUND", "memory_id must be a valid UUID."
            ) from exc
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id,type::text AS type,status::text AS status FROM public.memories WHERE id=$1 FOR SHARE",
                target_id,
            )
        if row is None or row["status"] != "active":
            raise _LightweightPreflightError(
                "TARGET_NOT_FOUND", "The selected memory does not exist or is inactive."
            )
        if row["type"] != "semantic":
            raise _LightweightPreflightError(
                "TARGET_TYPE_MISMATCH", "add_evidence requires an active semantic memory."
            )

    @staticmethod
    def _normalized_success(action: dict[str, Any], result: ToolResult) -> dict[str, Any]:
        output = result.output
        if not isinstance(output, dict):
            raise ValueError("memory handler returned no structured result")
        tool_name = str(action["tool_name"])
        memory_id = output.get("memory_id")
        if not isinstance(memory_id, str):
            raise ValueError("memory handler returned no actual memory_id")
        UUID(memory_id)
        data: dict[str, Any] = {"memory_id": memory_id}
        if tool_name == "remember":
            reused = output.get("reused")
            if not isinstance(reused, bool):
                raise ValueError("remember returned no authoritative reuse fact")
            data["write_effect"] = "reused" if reused else "created"
        else:
            prior = output.get("prior")
            posterior = output.get("posterior")
            if (
                isinstance(prior, bool)
                or not isinstance(prior, (int, float))
                or not math.isfinite(float(prior))
                or isinstance(posterior, bool)
                or not isinstance(posterior, (int, float))
                or not math.isfinite(float(posterior))
            ):
                raise ValueError("add_evidence returned no authoritative confidence values")
            data["prior_confidence"] = float(prior)
            data["posterior_confidence"] = float(posterior)
            # The DB path records source bookkeeping (including last_validated)
            # even when a duplicate source leaves confidence unchanged.
            data["write_effect"] = "updated"
        return build_receipt(
            tool_name=tool_name,
            call_id=str(action["actual_call_id"]),
            operation_id=UUID(str(action["command_id"])),
            ok=True,
            state="done",
            data=data,
            error=None,
        )

    async def _persist_terminal(
        self,
        action: dict[str, Any],
        receipt: dict[str, Any],
        *,
        tool_result: ToolResult | None,
        emit_result_event: bool,
    ) -> dict[str, Any]:
        result_payload = dict(receipt)
        result_payload.pop("contract_version", None)
        result_payload.pop("tool_name", None)
        result_payload.pop("call_id", None)
        event = None
        if emit_result_event:
            output = tool_result.output if tool_result and tool_result.success else None
            event = self._make_event(
                action,
                "TOOL_RESULT",
                {
                    "arguments": _json(action.get("arguments")) or {},
                    "success": bool(receipt.get("ok")),
                    "output": output,
                    "display_output": tool_result.display_output if tool_result else None,
                    "error": receipt.get("error", {}).get("message") if receipt.get("error") else None,
                    "error_type": tool_result.error_type.value if tool_result and tool_result.error_type else None,
                    "energy_spent": tool_result.energy_spent if tool_result else 0,
                    "duration_seconds": tool_result.duration_seconds if tool_result else 0.0,
                },
            )
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                raw = await conn.fetchval(
                    "SELECT public.finish_m5_lightweight_action($1,$2,$3::jsonb)",
                    UUID(str(action["command_id"])),
                    str(receipt["state"]),
                    json.dumps(result_payload, ensure_ascii=False, default=str),
                )
                persisted = _json(raw)
                if event is not None and isinstance(persisted, dict) and not persisted.get("replayed"):
                    await self._record_event_in_transaction(conn, event)
        return persisted if isinstance(persisted, dict) else result_payload

    def _make_event(
        self, action: dict[str, Any], event_type: str, payload: dict[str, Any]
    ) -> BehaviorEvent:
        authorization = _json(action.get("authorization")) or {}
        self._producer_seq += 1
        return BehaviorEvent(
            event_id=uuid4(),
            schema_version=1,
            event_type=event_type,
            stream_id=f"m5-lightweight:{action['origin_session_id']}",
            session_id=UUID(str(action["origin_session_id"])),
            request_id=authorization.get("request_id"),
            stimulus_revision=authorization.get("stimulus_revision"),
            input_turn_id=authorization.get("input_turn_id"),
            response_id=authorization.get("response_id"),
            task_id=None,
            call_id=str(action["actual_call_id"]),
            ts=datetime.now(timezone.utc),
            source="m5-lightweight",
            producer_id=self._producer_id,
            producer_seq=self._producer_seq,
            channel="voice",
            payload={
                **payload,
                "execution_lane": "lightweight",
                "command_id": str(action["command_id"]),
                "task_id": None,
                "call_id": str(action["actual_call_id"]),
                "tool_name": str(action["tool_name"]),
            },
        )

    async def _execute_claim(self, action: dict[str, Any]) -> None:
        arguments = _json(action.get("arguments"))
        if not isinstance(arguments, dict):
            await self._persist_terminal(
                action,
                self._error_receipt(action, "EXECUTION_UNKNOWN", "Claimed action arguments are unavailable.", state="unknown"),
                tool_result=None,
                emit_result_event=True,
            )
            return

        try:
            handler, energy_cost, context, registry = await self._preflight(action, arguments)
        except _LightweightPreflightError as exc:
            receipt = self._error_receipt(action, exc.code, str(exc), state="failed")
            await self._persist_terminal(
                action, receipt, tool_result=None, emit_result_event=True
            )
            return

        start_event = self._make_event(
            action,
            "TOOL_START",
            {"arguments": arguments, "iteration": 0},
        )
        try:
            await record_behavior_event(self._pool, start_event)
        except Exception as exc:
            logger.exception("M5 lightweight TOOL_START could not be recorded command=%s", action["command_id"])
            await self._persist_terminal(
                action,
                self._error_receipt(action, "MEMORY_WRITE_FAILED", f"Tool start audit failed: {exc}"),
                tool_result=None,
                emit_result_event=True,
            )
            return

        tool_result: ToolResult | None = None
        try:
            timeout = handler.spec.execution_timeout_seconds
            started_at = time.perf_counter()
            if timeout is None:
                tool_result = await handler.execute(arguments, context)
            else:
                tool_result = await asyncio.wait_for(
                    handler.execute(arguments, context), timeout=timeout
                )
            tool_result.duration_seconds = time.perf_counter() - started_at
            tool_result.energy_spent = energy_cost
            await registry.hooks.run(
                HookEvent.AFTER_TOOL_CALL,
                HookContext(
                    event=HookEvent.AFTER_TOOL_CALL,
                    tool_name=handler.spec.name,
                    arguments=arguments,
                    result=tool_result,
                    metadata={
                        "tool_context": context.tool_context.value,
                        "call_id": context.call_id,
                        "session_id": context.session_id,
                    },
                ),
            )
            if tool_result.success:
                receipt = self._normalized_success(action, tool_result)
            elif tool_result.error_type in {
                ToolErrorType.INVALID_PARAMS,
                ToolErrorType.DISABLED,
                ToolErrorType.CONTEXT_DENIED,
                ToolErrorType.APPROVAL_REQUIRED,
            }:
                receipt = self._error_receipt(
                    action,
                    "INVALID_ARGUMENTS" if tool_result.error_type == ToolErrorType.INVALID_PARAMS else "PERMISSION_DENIED",
                    tool_result.error or "The memory action was refused.",
                    state="failed",
                )
            else:
                receipt = self._error_receipt(
                    action,
                    "EXECUTION_UNKNOWN",
                    tool_result.error or "The memory write result could not be confirmed.",
                    state="unknown",
                )
        except _LightweightPreflightError as exc:
            tool_result = ToolResult.error_result(str(exc))
            receipt = self._error_receipt(action, exc.code, str(exc), state="failed")
        except asyncio.CancelledError:
            receipt = self._error_receipt(
                action,
                "EXECUTION_UNKNOWN",
                "The lightweight worker was interrupted while the memory write could be in flight.",
                state="unknown",
            )
            await asyncio.shield(
                self._persist_terminal(
                    action,
                    receipt,
                    tool_result=tool_result,
                    emit_result_event=True,
                )
            )
            raise
        except Exception as exc:
            logger.exception("M5 lightweight action outcome unknown command=%s", action["command_id"])
            tool_result = ToolResult.error_result(str(exc))
            receipt = self._error_receipt(
                action,
                "EXECUTION_UNKNOWN",
                f"The host cannot confirm whether the memory write completed: {exc}",
                state="unknown",
            )
        try:
            await self._persist_terminal(
                action, receipt, tool_result=tool_result, emit_result_event=True
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("M5 lightweight terminal action could not be persisted command=%s", action["command_id"])

    async def _preflight(self, action: dict[str, Any], arguments: dict[str, Any]):
        registry = self._registry
        if registry is None:
            registry = create_default_registry(self._pool)
            self._registry = registry
        tool_name = str(action["tool_name"])
        if tool_name not in LIGHTWEIGHT_TOOL_NAMES:
            raise _LightweightPreflightError("TOOL_NOT_ALLOWED", "Only approved lightweight memory tools may execute.")
        handler = registry.get(tool_name)
        if handler is None:
            raise _LightweightPreflightError("TOOL_NOT_ALLOWED", f"No handler is available for {tool_name}.")
        context = ToolExecutionContext(
            tool_context=ToolContext.CHAT,
            call_id=str(action["actual_call_id"]),
            session_id=str(action["origin_session_id"]),
            surface="chat",
            registry=registry,
        )
        errors = handler.validate(arguments)
        if errors:
            raise _LightweightPreflightError("INVALID_ARGUMENTS", "; ".join(errors))
        config = await registry.get_config()
        allowed, denial, _cost = await registry._evaluate_tool_policy(
            handler.spec, arguments, context, config
        )
        if not allowed:
            code = "PERMISSION_DENIED"
            if denial and denial.error_type == ToolErrorType.UNKNOWN_TOOL:
                code = "TOOL_NOT_ALLOWED"
            raise _LightweightPreflightError(
                code, denial.error if denial and denial.error else "The tool policy denied this action."
            )
        if tool_name == "add_evidence":
            await self._validate_evidence_target(arguments["memory_id"])
        return handler, int(_cost or 0), context, registry

    async def _validate_evidence_target(self, memory_id: str) -> None:
        try:
            target_id = UUID(memory_id)
        except (TypeError, ValueError) as exc:
            raise _LightweightPreflightError("TARGET_NOT_FOUND", "memory_id must be a valid UUID.") from exc
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT type::text AS type,status::text AS status FROM public.memories WHERE id=$1 FOR SHARE",
                target_id,
            )
        if row is None or row["status"] != "active":
            raise _LightweightPreflightError(
                "TARGET_NOT_FOUND", "The selected memory does not exist or is inactive."
            )
        if row["type"] != "semantic":
            raise _LightweightPreflightError(
                "TARGET_TYPE_MISMATCH", "add_evidence requires an active semantic memory."
            )

    @staticmethod
    def _error_receipt(
        action: dict[str, Any], code: str, message: str, *, state: str = "failed"
    ) -> dict[str, Any]:
        return build_receipt(
            tool_name=str(action["tool_name"]),
            call_id=str(action["actual_call_id"]),
            operation_id=UUID(str(action["command_id"])),
            ok=False,
            state=state,
            data={},
            error={"code": code, "message": message, "retryable": False},
        )
