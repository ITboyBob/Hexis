"""Prompt assembly and one-call orchestration for an M5 report round."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from core.llm import normalize_llm_config, resolve_api_key
from core.llm_config import load_llm_config
from services.m5_report_receiver import M5ReportReceiver
from services.m5_tool_contracts import load_contract
from services.m5_tool_contracts import parse_tool_arguments

from .m5_report_model import REPORT_TOOL_NAME, request_report_once


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

    def set_next_work_callback(self, callback: Any) -> None:
        self._receiver.set_next_work_callback(callback)

    async def run_once(
        self, report_id: UUID | str, config: Mapping[str, Any]
    ) -> dict[str, Any]:
        report_uuid = UUID(str(report_id))
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT r.report_id,r.task_id,r.origin_session_id,r.work_execution_id,
                       r.outcome,t.task_description,t.product_state,t.current_work_execution_id,
                       w.status AS work_status,w.result,w.error AS work_error
                FROM public.m5_reports r
                JOIN public.m5_tasks t ON t.id=r.task_id
                JOIN public.m5_work_rounds w
                  ON w.id=r.work_execution_id AND w.task_id=r.task_id
                WHERE r.report_id=$1
                """,
                report_uuid,
            )
        if row is None or row["outcome"] != "pending":
            raise ReportRunError("M5 report identity is missing or already terminal")
        if (
            row["work_status"] not in {"completed", "failed", "unknown"}
            or row["product_state"] != "working"
            or row["current_work_execution_id"] != row["work_execution_id"]
        ):
            raise ReportRunError("M5 report requires the bound work round to be terminal")
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

        messages = build_report_messages(
            row["task_description"],
            work_result,
            ["摘要只代表本轮真实结果，不表示持续任务整体完成。"],
        )
        tool = report_tool_definition()
        raw_response = await self._model_request(config, messages, tool)
        try:
            choices = raw_response["choices"]
            message = choices[0]["message"]
            tool_calls = message["tool_calls"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ReportRunError("M5 report response did not contain Function Calling") from exc
        if not isinstance(message, dict):
            raise ReportRunError("M5 report assistant message is malformed")
        if message.get("role") != "assistant":
            raise ReportRunError("M5 report response is not an assistant message")
        if not isinstance(tool_calls, list) or len(tool_calls) != 1:
            raise ReportRunError("M5 report requires exactly one Function Calling result")
        call = tool_calls[0]
        function = call.get("function") if isinstance(call, dict) else None
        actual_call_id = call.get("id") if isinstance(call, dict) else None
        if (
            not isinstance(function, dict)
            or function.get("name") != REPORT_TOOL_NAME
            or not isinstance(actual_call_id, str)
            or not actual_call_id.strip()
        ):
            raise ReportRunError("M5 report Function Calling identity is invalid")
        raw_arguments = function.get("arguments")
        try:
            arguments = parse_tool_arguments(REPORT_TOOL_NAME, raw_arguments)
        except Exception as exc:
            raise ReportRunError("M5 report Function Calling arguments are invalid") from exc

        receipt = await self._receiver.deliver(
            row["report_id"],
            row["origin_session_id"],
            arguments["summary"],
            actual_call_id,
        )
        return {
            "raw_response": raw_response,
            "raw_tool_call": call,
            "arguments": arguments,
            "receipt": receipt,
        }
