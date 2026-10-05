"""Real-loop acceptance test for the background executor.

This module is deliberately gated. It must not run as part of the ordinary
test suite because it performs a real LLM request and a real tool call.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest

from core.llm_config import load_llm_config
from services.background_executor import BackgroundExecutor, BackgroundTaskRequest
from services.behavior_events import query_behavior_events
from tests.utils import _db_dsn


_SOURCE_DB = os.getenv("POSTGRES_DB", "hexis_memory")
_REAL_GATE = "HEXIS_BACKGROUND_EXECUTOR_REAL"
_REAL_ENABLED = os.getenv(_REAL_GATE) == "1"

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        not _REAL_ENABLED,
        reason=(
            f"{_REAL_GATE} is not '1'; real LLM acceptance is explicitly "
            "disabled and no real request is attempted."
        ),
    ),
]


def _cache_dir() -> Path:
    root = Path(
        os.getenv("XDG_CACHE_HOME", str(Path.home() / ".cache")),
    )
    return root / "hexis" / "background-executor-2026-10-04"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _hexis_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


async def _real_config() -> dict[str, Any]:
    """Read the real config without copying or logging a secret."""
    conn = await asyncpg.connect(_db_dsn(_SOURCE_DB))
    try:
        raw_chat = await conn.fetchval("SELECT get_config('llm.chat')")
        raw_fallback = await conn.fetchval("SELECT get_config('llm')")
        raw = _json(raw_chat)
        source = "llm.chat"
        if raw in (None, "null"):
            raw = _json(raw_fallback)
            source = "llm"
        if not isinstance(raw, dict):
            raise RuntimeError("no usable llm.chat or llm configuration")

        normalized = await load_llm_config(
            conn,
            "llm.chat",
            fallback_key="llm",
        )
        return {
            "source": source,
            "provider": normalized["provider"],
            "model": normalized["model"],
            "endpoint": normalized.get("endpoint"),
            "api_key_env": raw.get("api_key_env"),
        }
    finally:
        await conn.close()


async def _install_real_config(db_pool: Any, config: dict[str, Any]) -> None:
    safe_config = {
        "provider": config["provider"],
        "model": config["model"],
        "endpoint": config.get("endpoint"),
        "api_key_env": config.get("api_key_env"),
    }
    async with db_pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('llm.chat', $1::jsonb)",
            json.dumps(safe_config),
        )


async def _task_rows(
    db_pool: Any,
    command_id: UUID,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    async with db_pool.acquire() as conn:
        task = await conn.fetchrow(
            """
            SELECT
                id, session_id, command_id, input, status,
                current_execution_id, result, error,
                created_at, started_at, completed_at, updated_at
            FROM background_tasks
            WHERE command_id=$1
            """,
            command_id,
        )
        if task is None:
            return None, []
        executions = await conn.fetch(
            """
            SELECT
                id, task_id, status, turn_id, result, error,
                started_at, finished_at
            FROM background_task_executions
            WHERE task_id=$1
            ORDER BY started_at DESC
            """,
            task["id"],
        )
    return dict(task), [dict(row) for row in executions]


async def _agent_events(
    db_pool: Any,
    turn_id: UUID | None,
) -> list[dict[str, Any]]:
    if turn_id is None:
        return []
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT event_type, payload, created_at
            FROM agent_turn_events
            WHERE turn_id=$1
            ORDER BY created_at
            """,
            turn_id,
        )
    return [
        {
            "event_type": row["event_type"],
            "payload": _json(row["payload"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


async def _behavior_events(
    db_pool: Any,
    session_id: UUID,
    task_id: UUID,
) -> list[dict[str, Any]]:
    return (
        await query_behavior_events(
            db_pool,
            session_id=session_id,
            task_id=str(task_id),
            limit=100,
        )
    )["events"]


def _tool_summary(
    behavior_events: list[dict[str, Any]],
    task: dict[str, Any] | None,
) -> dict[str, Any]:
    starts = [event for event in behavior_events if event["event_type"] == "TOOL_START"]
    results = [event for event in behavior_events if event["event_type"] == "TOOL_RESULT"]
    start_ids = [str(event.get("call_id")) for event in starts if event.get("call_id")]
    result_ids = [str(event.get("call_id")) for event in results if event.get("call_id")]
    shared_ids = sorted(set(start_ids).intersection(result_ids))

    task_result = _json(task.get("result")) if task else None
    tool_results = []
    if isinstance(task_result, dict):
        raw_tools = task_result.get("tool_results")
        if isinstance(raw_tools, list):
            tool_results = raw_tools

    return {
        "tool_start_count": len(starts),
        "tool_result_count": len(results),
        "call_ids": shared_ids,
        "tool_results": tool_results,
    }


@pytest.mark.asyncio(loop_scope="session")
async def test_background_executor_real_loop(db_pool):
    config = await _real_config()
    await _install_real_config(db_pool, config)

    api_key_env = config.get("api_key_env")
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hexis_head": _hexis_head(),
        "python_version": platform.python_version(),
        "config": {
            "source": config["source"],
            "provider": config["provider"],
            "model": config["model"],
            "endpoint_present": bool(config.get("endpoint")),
            "api_key_env": api_key_env,
            "api_key_available_in_process": bool(os.getenv(api_key_env))
            if api_key_env
            else False,
        },
        "task_input": {
            "instruction": "调用 recall 工具，然后回复 done",
            "success_criteria": "text_and_tool",
        },
        "regression_results_file": "regressions.json",
    }

    command_id = uuid4()
    evidence["command_id"] = str(command_id)
    started_at = datetime.now(timezone.utc)
    start_perf = perf_counter()
    outcome: dict[str, Any] | None = None
    run_error: dict[str, Any] | None = None

    try:
        async with db_pool.acquire() as conn:
            session_id = await conn.fetchval(
                "INSERT INTO chat_sessions DEFAULT VALUES RETURNING id"
            )
        evidence["session_id"] = str(session_id)

        executor = BackgroundExecutor(db_pool)
        request = BackgroundTaskRequest(
            session_id=session_id,
            command_id=command_id,
            instruction=evidence["task_input"]["instruction"],
            success_criteria=evidence["task_input"]["success_criteria"],
        )
        outcome = await executor.run(request)
        evidence["outcome"] = outcome
    except Exception as exc:
        run_error = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        evidence["run_error"] = run_error
        raise
    finally:
        finished_at = datetime.now(timezone.utc)
        evidence["finished_at"] = finished_at.isoformat()
        evidence["duration_seconds"] = round(perf_counter() - start_perf, 3)

        task, executions = await _task_rows(db_pool, command_id)
        evidence["background_task"] = task
        evidence["background_task_executions"] = executions

        current_execution = executions[0] if executions else None
        turn_id = (
            UUID(str(current_execution["turn_id"]))
            if current_execution and current_execution.get("turn_id")
            else None
        )
        evidence["turn_id"] = str(turn_id) if turn_id else None
        evidence["agent_turn_events"] = await _agent_events(db_pool, turn_id)

        if task is not None:
            behavior = await _behavior_events(
                db_pool,
                UUID(str(task["session_id"])),
                UUID(str(task["id"])),
            )
            evidence["behavior_events"] = behavior
            evidence["tool_summary"] = _tool_summary(behavior, task)
        else:
            evidence["behavior_events"] = []
            evidence["tool_summary"] = {
                "tool_start_count": 0,
                "tool_result_count": 0,
                "call_ids": [],
                "tool_results": [],
            }

        evidence["status"] = "failed" if run_error else "completed"
        _write_json(_cache_dir() / "real-task.json", evidence)

    assert task is not None
    assert current_execution is not None
    assert turn_id is not None
    assert task["status"] == "completed"
    assert current_execution["status"] == "completed"

    assert outcome is not None
    assert outcome["criteria_met"] is True
    assert outcome["task_updated"] is True
    assert outcome["succeeded"] is True
    assert outcome["task_id"] == str(task["id"])
    assert outcome["execution_id"] == str(current_execution["id"])
    assert str(task["current_execution_id"]) == outcome["execution_id"]
    assert current_execution["turn_id"] == turn_id

    task_result = _json(task["result"])
    assert isinstance(task_result, dict)
    assert task_result["text"] == "done"

    raw_tool_results = task_result.get("tool_results")
    assert isinstance(raw_tool_results, list)
    successful_recall_results = [
        item
        for item in raw_tool_results
        if isinstance(item, dict)
        and item.get("name") == "recall"
        and item.get("success") is True
        and item.get("id")
    ]
    assert successful_recall_results
    successful_call_ids = {
        str(item["id"]) for item in successful_recall_results
    }

    event_types = [event["event_type"] for event in evidence["behavior_events"]]
    assert "task_queued" in event_types
    assert "task_started" in event_types
    assert "TOOL_START" in event_types
    assert "TOOL_RESULT" in event_types
    assert "task_completed" in event_types
    assert "task_failed" not in event_types

    behavior_starts = [
        event
        for event in evidence["behavior_events"]
        if event["event_type"] == "TOOL_START"
    ]
    behavior_results = [
        event
        for event in evidence["behavior_events"]
        if event["event_type"] == "TOOL_RESULT"
    ]
    behavior_start_ids = {
        str(event["call_id"])
        for event in behavior_starts
        if event.get("call_id")
    }
    behavior_result_ids = {
        str(event["call_id"])
        for event in behavior_results
        if event.get("call_id")
    }
    assert successful_call_ids.issubset(behavior_start_ids)
    assert successful_call_ids.issubset(behavior_result_ids)
    assert successful_call_ids.issubset(
        set(evidence["tool_summary"]["call_ids"])
    )
