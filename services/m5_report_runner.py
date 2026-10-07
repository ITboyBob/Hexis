"""Prompt assembly and one-call orchestration for an M5 report round."""

from __future__ import annotations

import json
import asyncio
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from core.llm import normalize_llm_config, resolve_api_key
from core.llm_config import load_llm_config
from services.m5_report_receiver import M5ReportReceiver, ReportDeliveryError
from services.m5_tool_contracts import load_contract
from services.m5_tool_contracts import (
    M5ToolContractError,
    decode_tool_arguments_object,
    validate_tool_arguments_object,
)

from .m5_report_model import (
    REPORT_TOOL_NAME,
    request_report_once,
    validate_report_config,
)


PROMPT_PATH = Path(__file__).with_name("prompts") / "m5_report.md"
REPORT_CONFIG_KEY = "llm.m5_report"


def report_tool_definition() -> dict[str, Any]:
    contract = load_contract()
    for item in contract["tools"]:
        function = item.get("function", {})
        if function.get("name") == REPORT_TOOL_NAME:
            return item
    raise RuntimeError("M5 report tool is missing from the frozen contract")


async def load_selected_report_config(pool: Any) -> dict[str, Any]:
    """Load only the dedicated, explicitly selected M5 report configuration."""
    async with pool.acquire() as conn:
        raw = await conn.fetchval("SELECT get_config($1)", REPORT_CONFIG_KEY)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = None
        if not isinstance(raw, dict) or not raw:
            raise ValueError(
                "M5 report requires an explicitly selected llm.m5_report configuration"
            )
        if (
            raw.get("provider") != "openai-chat-completions-endpoint"
            or raw.get("model") != "qwen-plus"
        ):
            raise ValueError("llm.m5_report must explicitly select qwen-plus Chat Completions")
        if not isinstance(raw.get("endpoint"), str) or not raw["endpoint"].strip():
            raise ValueError("llm.m5_report endpoint is missing")
        timeout = raw.get("timeout_seconds")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("llm.m5_report timeout_seconds must be explicitly configured")
        env_name = raw.get("api_key_env")
        if not isinstance(env_name, str) or not env_name.strip():
            raise ValueError("llm.m5_report must reference a selected credential environment variable")
        api_key = resolve_api_key(env_name)
        if not api_key:
            raise ValueError(f"selected report credential environment variable is unavailable: {env_name}")

        loaded = await load_llm_config(conn, REPORT_CONFIG_KEY, fallback_key=None)
        normalized = normalize_llm_config(
            {
                "provider": raw["provider"],
                "model": raw["model"],
                "endpoint": raw["endpoint"],
                "api_key": api_key,
            }
        )
        if (
            loaded.get("provider") != normalized.get("provider")
            or loaded.get("model") != normalized.get("model")
            or loaded.get("endpoint") != normalized.get("endpoint")
        ):
            raise ValueError("resolved M5 report configuration differs from its selected source")
        return normalized | {"timeout_seconds": float(timeout)}


def build_report_messages(
    task_title: str,
    work_result: Mapping[str, Any],
    restrictions: list[str] | None = None,
) -> list[dict[str, str]]:
    if not isinstance(task_title, str) or not task_title.strip():
        raise ValueError("M5 report requires the actual task title")
    if not isinstance(work_result, Mapping):
        raise ValueError("M5 report requires the persisted work result")
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    facts = {
        "task_title": task_title,
        "work_result": dict(work_result),
        "restrictions": list(restrictions or []),
    }
    return [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": json.dumps(facts, ensure_ascii=False, sort_keys=True),
        },
    ]


async def request_report(
    config: Mapping[str, Any],
    task_title: str,
    work_result: Mapping[str, Any],
    restrictions: list[str] | None = None,
) -> dict[str, Any]:
    """Issue one dedicated report request; delivery is handled by the receiver."""
    return await request_report_once(
        config,
        build_report_messages(task_title, work_result, restrictions),
        report_tool_definition(),
    )


class ReportRunError(RuntimeError):
    """The independent report response was not an acceptable tool call."""


class M5ReportRunner:
    def __init__(
        self,
        pool: Any,
        *,
        model_request: Any = request_report_once,
        on_next_work: Any | None = None,
    ) -> None:
        self._pool = pool
        self._model_request = model_request
        self._receiver = M5ReportReceiver(pool, on_next_work=on_next_work)
        self._on_next_work = on_next_work

    def set_next_work_callback(self, callback: Any) -> None:
        self._on_next_work = callback
        self._receiver.set_next_work_callback(callback)

    async def pending_report_ids(self, limit: int = 1) -> list[UUID]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT r.report_id
                FROM public.m5_reports r
                JOIN public.m5_tasks t ON t.id=r.task_id
                JOIN public.m5_work_rounds w
                  ON w.id=r.work_execution_id AND w.task_id=r.task_id
                WHERE r.outcome='pending'
                  AND t.product_state='working'
                  AND t.current_work_execution_id=r.work_execution_id
                  AND w.status IN ('completed','failed','unknown')
                ORDER BY r.updated_at,r.report_id
                LIMIT $1
                """,
                limit,
            )
        return [row["report_id"] for row in rows]

    async def stop_report(
        self,
        report_id: UUID | str,
        *,
        outcome: str,
        code: str,
        message: str,
        actual_call_id: str | None = None,
    ) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT public.finish_m5_report_terminal($1,$2,$3,$4,$5)",
                UUID(str(report_id)),
                outcome,
                code,
                message,
                actual_call_id,
            )
        result = json.loads(raw) if isinstance(raw, str) else raw
        self._wake_next_work(result)
        return result

    def _wake_next_work(self, result: Mapping[str, Any]) -> None:
        round_result = result.get("round")
        if (
            self._on_next_work
            and isinstance(round_result, Mapping)
            and round_result.get("next_work_execution_id")
        ):
            self._on_next_work()

    async def _report_state(self, report_id: UUID) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT r.report_id,r.task_id,r.origin_session_id,r.work_execution_id,
                       r.outcome,r.failure_count,r.attempt_count,r.rejected_signatures,
                       r.receiver_receipt,r.last_error,r.last_call_id,
                       t.task_description,t.product_state,t.current_work_execution_id,
                       w.status AS work_status,w.result,w.error AS work_error
                FROM public.m5_reports r
                JOIN public.m5_tasks t ON t.id=r.task_id
                JOIN public.m5_work_rounds w
                  ON w.id=r.work_execution_id AND w.task_id=r.task_id
                WHERE r.report_id=$1
                """,
                report_id,
            )
        return dict(row) if row is not None else None

    async def _record_failure(
        self,
        report_id: UUID,
        *,
        code: str,
        message: str,
        actual_call_id: str | None = None,
        rejected_signature: str | None = None,
    ) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT public.record_m5_report_failure($1,$2,$3,$4,$5)",
                report_id,
                code,
                message,
                actual_call_id,
                rejected_signature,
            )
        result = json.loads(raw) if isinstance(raw, str) else raw
        self._wake_next_work(result)
        return result

    @staticmethod
    def _signature(tool_name: str, arguments: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"tool": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _tool_error(code: str, message: str, retryable: bool = False) -> str:
        return json.dumps(
            {
                "ok": False,
                "error": {"code": code, "message": message, "retryable": retryable},
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _receipt_for_call(
        receipt: Any, actual_call_id: str | None = None
    ) -> dict[str, Any]:
        value = json.loads(receipt) if isinstance(receipt, str) else receipt
        if not isinstance(value, dict):
            return {"outcome": "delivered", "receiver_receipt": value}
        original_call = value.get("actual_call_id")
        if actual_call_id and actual_call_id != original_call:
            return value | {
                "receiver_actual_call_id": original_call,
                "actual_call_id": actual_call_id,
                "replayed": True,
            }
        return value | {"replayed": False}

    async def _terminal_from_state(
        self, row: Mapping[str, Any], *, actual_call_id: str | None = None
    ) -> dict[str, Any]:
        if row["outcome"] == "delivered":
            return {
                "outcome": "delivered",
                "receipt": self._receipt_for_call(row["receiver_receipt"], actual_call_id),
            }
        last_error = row["last_error"]
        if isinstance(last_error, str):
            try:
                last_error = json.loads(last_error)
            except ValueError:
                last_error = {"message": last_error}
        return {
            "outcome": row["outcome"],
            "code": last_error.get("code") if isinstance(last_error, dict) else None,
            "error": last_error,
            "failure_count": row["failure_count"],
            "attempt_count": row["attempt_count"],
        }

    async def run_once(
        self, report_id: UUID | str, config: Mapping[str, Any]
    ) -> dict[str, Any]:
        report_uuid = UUID(str(report_id))
        messages: list[dict[str, Any]] | None = None
        config_checked = False
        while True:
            row = await self._report_state(report_uuid)
            if row is None:
                raise ReportRunError("M5 report identity is unavailable")
            if row["outcome"] != "pending":
                return await self._terminal_from_state(row)
            if not config_checked:
                try:
                    validate_report_config(config)
                except (TypeError, ValueError) as exc:
                    return await self.stop_report(
                        report_uuid,
                        outcome="refused",
                        code="CONFIGURATION_INVALID",
                        message=str(exc),
                    )
                config_checked = True
            if (
                row["work_status"] not in {"completed", "failed", "unknown"}
                or row["product_state"] != "working"
                or row["current_work_execution_id"] != row["work_execution_id"]
            ):
                raise ReportRunError("M5 report requires the bound work round to be terminal")
            if row["failure_count"] >= 3:
                return await self.stop_report(
                    report_uuid,
                    outcome="exhausted",
                    code="RETRY_EXHAUSTED",
                    message="The report failure budget is exhausted.",
                    actual_call_id=row["last_call_id"],
                )
            if row["attempt_count"] >= 16:
                return await self.stop_report(
                    report_uuid,
                    outcome="exhausted",
                    code="TOOL_ATTEMPT_LIMIT",
                    message="The report tool attempt budget is exhausted.",
                    actual_call_id=row["last_call_id"],
                )

            work_result = (
                json.loads(row["result"])
                if isinstance(row["result"], str)
                else row["result"]
            )
            if work_result is None:
                work_result = {
                    "status": row["work_status"],
                    "error": row["work_error"],
                    "result": None,
                }
            elif isinstance(work_result, dict):
                work_result = dict(work_result) | {"status": row["work_status"]}
            else:
                raise ReportRunError("M5 report work result is not an object")
            if messages is None:
                messages = build_report_messages(
                    row["task_description"],
                    work_result,
                    ["摘要只代表本轮真实结果，不表示持续任务整体完成。"],
                )
            try:
                raw_response = await self._model_request(
                    config, messages, report_tool_definition()
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error_text = str(exc) or type(exc).__name__
                code = (
                    "MODEL_REQUEST_TIMEOUT"
                    if isinstance(exc, (TimeoutError, asyncio.TimeoutError))
                    or "timeout" in error_text.lower()
                    else "MODEL_REQUEST_FAILED"
                )
                result = await self._record_failure(
                    report_uuid, code=code, message=error_text
                )
                if result.get("outcome") != "pending":
                    return result
                continue

            try:
                message = raw_response["choices"][0]["message"]
                tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
            except (KeyError, IndexError, TypeError, AttributeError):
                message = None
                tool_calls = None
            if not isinstance(message, dict) or message.get("role") != "assistant":
                message = {"role": "assistant", "content": ""}
                tool_calls = None
            if not isinstance(tool_calls, list) or not tool_calls:
                result = await self._record_failure(
                    report_uuid,
                    code="MISSING_TOOL_CALL",
                    message="The report response did not contain Function Calling.",
                )
                if isinstance(message.get("content"), str) and message["content"]:
                    messages.append({"role": "assistant", "content": message["content"]})
                if result.get("outcome") != "pending":
                    return result
                continue
            if len(tool_calls) != 1:
                call_ids = [
                    item.get("id") for item in tool_calls if isinstance(item, dict)
                ]
                return await self.stop_report(
                    report_uuid,
                    outcome="refused",
                    code="TOOL_NOT_ALLOWED",
                    message="The report response returned multiple tool calls.",
                    actual_call_id=next((value for value in call_ids if isinstance(value, str)), None),
                )

            call = tool_calls[0]
            function = call.get("function") if isinstance(call, dict) else None
            actual_call_id = call.get("id") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                result = await self._record_failure(
                    report_uuid,
                    code="INVALID_TOOL_CALL",
                    message="The report tool call envelope was malformed.",
                    actual_call_id=actual_call_id if isinstance(actual_call_id, str) else None,
                )
                if result.get("outcome") != "pending":
                    return result
                continue
            tool_name = function.get("name")
            if tool_name != REPORT_TOOL_NAME:
                return await self.stop_report(
                    report_uuid,
                    outcome="refused",
                    code="TOOL_NOT_ALLOWED",
                    message=f"The report model called an unsupported tool: {tool_name}.",
                    actual_call_id=actual_call_id if isinstance(actual_call_id, str) else None,
                )
            if not isinstance(actual_call_id, str) or not actual_call_id.strip():
                result = await self._record_failure(
                    report_uuid,
                    code="MISSING_CALL_ID",
                    message="The report tool call has no provider call_id.",
                )
                if result.get("outcome") != "pending":
                    return result
                continue

            try:
                arguments_object = decode_tool_arguments_object(function.get("arguments"))
            except M5ToolContractError as exc:
                result = await self._record_failure(
                    report_uuid,
                    code="INVALID_JSON",
                    message=str(exc),
                    actual_call_id=actual_call_id,
                )
                messages.extend(
                    [
                        {"role": "assistant", "content": message.get("content"), "tool_calls": [call]},
                        {
                            "role": "tool",
                            "tool_call_id": actual_call_id,
                            "content": self._tool_error("INVALID_JSON", str(exc), True),
                        },
                    ]
                )
                if result.get("outcome") != "pending":
                    return result
                continue

            async with self._pool.acquire() as conn:
                raw_reservation = await conn.fetchval(
                    "SELECT public.reserve_m5_report_attempt($1,$2)",
                    report_uuid,
                    actual_call_id,
                )
            reservation = json.loads(raw_reservation) if isinstance(raw_reservation, str) else raw_reservation
            if not isinstance(reservation, dict) or not reservation.get("allowed"):
                return await self.stop_report(
                    report_uuid,
                    outcome="exhausted",
                    code=(reservation or {}).get("code", "TOOL_ATTEMPT_LIMIT"),
                    message="The report tool attempt budget is exhausted.",
                    actual_call_id=actual_call_id,
                )

            try:
                arguments = validate_tool_arguments_object(REPORT_TOOL_NAME, arguments_object)
            except M5ToolContractError as exc:
                result = await self._record_failure(
                    report_uuid,
                    code="INVALID_ARGUMENTS",
                    message=str(exc),
                    actual_call_id=actual_call_id,
                    rejected_signature=self._signature(REPORT_TOOL_NAME, arguments_object),
                )
                messages.extend(
                    [
                        {"role": "assistant", "content": message.get("content"), "tool_calls": [call]},
                        {
                            "role": "tool",
                            "tool_call_id": actual_call_id,
                            "content": self._tool_error(result.get("code", "INVALID_ARGUMENTS"), str(exc), True),
                        },
                    ]
                )
                if result.get("outcome") != "pending":
                    return result
                continue

            try:
                receipt = await self._receiver.deliver(
                    row["report_id"],
                    row["origin_session_id"],
                    arguments["summary"],
                    actual_call_id,
                )
            except ReportDeliveryError as exc:
                refreshed = await self._report_state(report_uuid)
                if refreshed and refreshed["outcome"] == "delivered":
                    return await self._terminal_from_state(
                        refreshed, actual_call_id=actual_call_id
                    )
                if not exc.retryable:
                    return await self.stop_report(
                        report_uuid,
                        outcome="refused",
                        code=exc.code,
                        message=str(exc),
                        actual_call_id=actual_call_id,
                    )
                result = await self._record_failure(
                    report_uuid,
                    code=exc.code,
                    message=str(exc),
                    actual_call_id=actual_call_id,
                )
                messages.extend(
                    [
                        {"role": "assistant", "content": message.get("content"), "tool_calls": [call]},
                        {
                            "role": "tool",
                            "tool_call_id": actual_call_id,
                            "content": self._tool_error(exc.code, str(exc), True),
                        },
                    ]
                )
                if result.get("outcome") != "pending":
                    return result
                continue

            self._wake_next_work(receipt)
            return {"outcome": "delivered", "receipt": receipt, "raw_response": raw_response}
