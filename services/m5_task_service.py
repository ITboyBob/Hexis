"""Authoritative M5 task and submit persistence adapters."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID


_AUTHORIZATION_FIELDS = {
    "origin_session_id",
    "input_turn_id",
    "input_order",
    "stimulus_revision",
    "response_id",
    "supplier_response_id",
    "request_id",
    "actual_call_id",
}
_PENDING_AUTHORIZATION_FIELDS = _AUTHORIZATION_FIELDS | {"task_id"}
def decode_task_work_input(
    value: str, context: Any = None
) -> tuple[str, list[str], list[dict], list[dict]]:
    """Decode host-owned task-window context without interpreting user text."""
    message = value
    payload = _coerce_json(context) if context is not None else {}
    prompt_addenda = payload.get("prompt_addenda", [])
    visuals = payload.get("visual_attachments", [])
    attachments = payload.get("attachments", [])
    if not isinstance(message, str) or not message.strip():
        raise ValueError("M5 work input must contain a non-empty message")
    if not isinstance(prompt_addenda, list) or not all(
        isinstance(item, str) for item in prompt_addenda
    ):
        raise ValueError("task-window prompt addenda are malformed")
    if not isinstance(visuals, list) or not all(isinstance(item, dict) for item in visuals):
        raise ValueError("task-window visual attachments are malformed")
    if not isinstance(attachments, list) or not all(
        isinstance(item, dict) for item in attachments
    ):
        raise ValueError("task-window attachments are malformed")
    return message, prompt_addenda, visuals, attachments


def _coerce_json(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


async def _remove_window_work_context(conn: Any, task_session_id: UUID, work_id: UUID) -> None:
    row = await conn.fetchrow(
        "SELECT metadata FROM public.chat_sessions WHERE id=$1 FOR UPDATE",
        task_session_id,
    )
    if row is None:
        return
    metadata = _coerce_json(row["metadata"])
    contexts = metadata.get("m5_work_contexts")
    if not isinstance(contexts, dict) or str(work_id) not in contexts:
        return
    contexts.pop(str(work_id), None)
    if contexts:
        await conn.execute(
            "UPDATE public.chat_sessions SET metadata=jsonb_set(COALESCE(metadata,'{}'::jsonb),'{m5_work_contexts}',$2::jsonb,TRUE), updated_at=clock_timestamp() WHERE id=$1",
            task_session_id,
            json.dumps(contexts, ensure_ascii=False),
        )
    else:
        await conn.execute(
            "UPDATE public.chat_sessions SET metadata=COALESCE(metadata,'{}'::jsonb)-'m5_work_contexts', updated_at=clock_timestamp() WHERE id=$1",
            task_session_id,
        )


def _uuid(value: UUID | str, name: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be a real UUID") from exc


def _submit_digest(description: str) -> str:
    business_arguments = {
        "tool_name": "submit",
        "arguments": {"task_description": description},
    }
    canonical = json.dumps(
        business_arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _pending_digest(task_id: UUID, message: str) -> str:
    business_arguments = {
        "tool_name": "pend_message",
        "arguments": {"task_id": str(task_id), "message": message},
    }
    canonical = json.dumps(
        business_arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class M5TaskService:
    def __init__(self, pool: Any) -> None:
        if pool is None:
            raise ValueError("a database pool is required for M5 tasks")
        self._pool = pool

    async def submit_m5_task(
        self,
        origin_session_id: UUID | str,
        command_id: UUID | str,
        description: str,
        original_request: str,
        authorization: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically accept a task and preserve its original business result."""
        origin_id = _uuid(origin_session_id, "origin_session_id")
        command_uuid = _uuid(command_id, "command_id")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("task description must not be blank")
        if not isinstance(original_request, str) or not original_request.strip():
            raise ValueError("original user request must not be blank")
        if not isinstance(authorization, dict):
            raise ValueError("host authorization identity must be an object")
        unknown = set(authorization) - _AUTHORIZATION_FIELDS
        if unknown:
            raise ValueError(
                f"host authorization contains unsupported fields: {sorted(unknown)}"
            )
        actual_call_id = authorization.get("actual_call_id")
        input_turn_id = authorization.get("input_turn_id")
        response_id = authorization.get("response_id")
        supplier_response_id = authorization.get("supplier_response_id")
        if not isinstance(actual_call_id, str) or not actual_call_id.strip():
            raise ValueError("a real supplier call_id must be bound by the host")
        if not isinstance(input_turn_id, str) or not input_turn_id.strip():
            raise ValueError("a formally authorized input_turn_id is required")
        _uuid(input_turn_id, "input_turn_id")
        if not isinstance(response_id, str) or not response_id.strip():
            raise ValueError("a local response_id is required")
        if not isinstance(supplier_response_id, str) or not supplier_response_id.strip():
            raise ValueError("a supplier response_id is required")
        if authorization.get("origin_session_id") != str(origin_id):
            raise ValueError("authorization origin does not match the requested chat")
        input_order = authorization.get("input_order")
        stimulus_revision = authorization.get("stimulus_revision")
        if (
            isinstance(input_order, bool)
            or not isinstance(input_order, int)
            or input_order < 0
            or isinstance(stimulus_revision, bool)
            or not isinstance(stimulus_revision, int)
            or stimulus_revision < 0
        ):
            raise ValueError("authorized input order and revision must be non-negative integers")

        host_authorization = dict(authorization)
        host_authorization.update(
            command_id=str(command_uuid),
            argument_digest=_submit_digest(description),
        )
        async with self._pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT public.submit_m5_task($1, $2, $3, $4, $5::jsonb)",
                origin_id,
                command_uuid,
                description,
                original_request,
                json.dumps(host_authorization, ensure_ascii=False),
            )
        return _coerce_json(raw)

    async def list_claimable_work(self, *, limit: int = 10) -> list[UUID]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("claimable-work limit must be between 1 and 100")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT w.id
                FROM public.m5_work_rounds w
                JOIN public.m5_tasks t ON t.id=w.task_id
                WHERE w.status='claimed'
                  AND t.product_state='working'
                  AND t.current_work_execution_id=w.id
                ORDER BY t.created_at, w.id
                LIMIT $1
                """,
                limit,
            )
        return [row["id"] for row in rows]

    async def resolve_task_session(self, session_id: UUID | str) -> dict[str, Any] | None:
        session_uuid = _uuid(session_id, "session_id")
        async with self._pool.acquire() as conn:
            ready = await conn.fetchval(
                "SELECT to_regclass('public.m5_tasks') IS NOT NULL"
            )
            if not ready:
                return None
            row = await conn.fetchrow(
                """
                SELECT id AS task_id, origin_session_id, task_session_id,
                       product_state, current_work_execution_id, revision
                FROM public.m5_tasks
                WHERE task_session_id=$1
                """,
                session_uuid,
            )
        return dict(row) if row is not None else None

    async def create_idle_work_round(
        self,
        task_session_id: UUID | str,
        user_message: str,
        *,
        prompt_addenda: list[str] | None = None,
        visual_attachments: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Atomically move one authoritative idle task into a new work round."""
        session_uuid = _uuid(task_session_id, "task_session_id")
        if not isinstance(user_message, str) or not user_message.strip():
            raise ValueError("task-session input must not be blank")
        context_payload = {
            "prompt_addenda": list(prompt_addenda or []),
            "visual_attachments": list(visual_attachments or []),
            "attachments": list(attachments or []),
        }
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                task = await conn.fetchrow(
                    """
                    SELECT id, product_state, revision
                    FROM public.m5_tasks
                    WHERE task_session_id=$1
                    FOR UPDATE
                    """,
                    session_uuid,
                )
                if task is None:
                    return {"accepted": False, "reason": "not_task_session"}
                if task["product_state"] != "idle":
                    return {
                        "accepted": False,
                        "reason": "working",
                        "task_id": str(task["id"]),
                        "task_state": task["product_state"],
                    }
                work_id = await conn.fetchval(
                    """
                    INSERT INTO public.m5_work_rounds (task_id, status, input)
                    VALUES ($1, 'claimed', $2)
                    RETURNING id
                    """,
                    task["id"],
                    user_message,
                )
                await conn.execute(
                    """
                    UPDATE public.chat_sessions
                    SET metadata = jsonb_set(
                        COALESCE(metadata, '{}'::jsonb),
                        '{m5_work_contexts}',
                        COALESCE(metadata->'m5_work_contexts', '{}'::jsonb)
                            || jsonb_build_object($2::text, $3::jsonb),
                        TRUE
                    ),
                    updated_at=clock_timestamp()
                    WHERE id=$1
                    """,
                    session_uuid,
                    str(work_id),
                    json.dumps(context_payload, ensure_ascii=False),
                )
                await conn.execute(
                    """
                    UPDATE public.m5_tasks
                    SET product_state='working',
                        current_work_execution_id=$2,
                        revision=revision+1,
                        updated_at=clock_timestamp()
                    WHERE id=$1 AND product_state='idle'
                    """,
                    task["id"],
                    work_id,
                )
                return {
                    "accepted": True,
                    "task_id": str(task["id"]),
                    "task_session_id": str(session_uuid),
                    "work_execution_id": str(work_id),
                    "task_state": "working",
                    "revision": int(task["revision"]) + 1,
                }

    async def append_m5_message(
        self,
        origin_session_id: UUID | str,
        task_id: UUID | str,
        command_id: UUID | str,
        message: str,
        authorization: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one user-confirmed message and preserve FIFO/idempotency."""
        origin_uuid = _uuid(origin_session_id, "origin_session_id")
        task_uuid = _uuid(task_id, "task_id")
        command_uuid = _uuid(command_id, "command_id")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("pending message must not be blank")
        if not isinstance(authorization, dict):
            raise ValueError("host authorization identity must be an object")
        unknown = set(authorization) - _PENDING_AUTHORIZATION_FIELDS
        if unknown:
            raise ValueError(
                f"host authorization contains unsupported fields: {sorted(unknown)}"
            )
        if authorization.get("origin_session_id") != str(origin_uuid):
            raise ValueError("authorization origin does not match the source chat")
        if authorization.get("task_id") != str(task_uuid):
            raise ValueError("selected task does not match the host authorization")
        if not isinstance(authorization.get("actual_call_id"), str) or not authorization["actual_call_id"].strip():
            raise ValueError("a real supplier call_id must be bound by the host")
        if not isinstance(authorization.get("input_turn_id"), str):
            raise ValueError("a formally authorized input_turn_id is required")
        _uuid(authorization["input_turn_id"], "input_turn_id")
        if not isinstance(authorization.get("response_id"), str) or not authorization["response_id"].strip():
            raise ValueError("a local response_id is required")
        if not isinstance(authorization.get("supplier_response_id"), str) or not authorization["supplier_response_id"].strip():
            raise ValueError("a supplier response_id is required")
        input_order = authorization.get("input_order")
        stimulus_revision = authorization.get("stimulus_revision")
        if (
            isinstance(input_order, bool)
            or not isinstance(input_order, int)
            or input_order < 0
            or isinstance(stimulus_revision, bool)
            or not isinstance(stimulus_revision, int)
            or stimulus_revision < 0
        ):
            raise ValueError("authorized input order and revision must be non-negative integers")

        host_authorization = dict(authorization)
        host_authorization.update(
            command_id=str(command_uuid),
            argument_digest=_pending_digest(task_uuid, message),
        )
        async with self._pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT public.append_m5_message($1,$2,$3,$4,$5::jsonb)",
                origin_uuid,
                task_uuid,
                command_uuid,
                message,
                json.dumps(host_authorization, ensure_ascii=False),
            )
        return _coerce_json(raw)

    async def claim_next_m5_message(self, task_id: UUID | str) -> dict[str, Any] | None:
        task_uuid = _uuid(task_id, "task_id")
        async with self._pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT public.claim_next_m5_message($1)", task_uuid
            )
        return _coerce_json(raw) if raw is not None else None

    async def list_pending_messages(self, task_id: UUID | str) -> list[dict[str, Any]]:
        task_uuid = _uuid(task_id, "task_id")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, task_id, receive_order, message, received_at
                FROM public.m5_pending_messages
                WHERE task_id=$1 AND claimed_work_execution_id IS NULL
                ORDER BY receive_order
                """,
                task_uuid,
            )
        return [dict(row) for row in rows]

    async def finish_m5_round(
        self,
        task_id: UUID | str,
        work_execution_id: UUID | str,
        *,
        report_outcome: str,
    ) -> dict[str, Any]:
        if report_outcome not in {"delivered", "exhausted", "refused"}:
            raise ValueError("report_outcome must be terminal")
        task_uuid = _uuid(task_id, "task_id")
        work_id = _uuid(work_execution_id, "work_execution_id")
        async with self._pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT public.finish_m5_round($1,$2,$3)",
                task_uuid,
                work_id,
                report_outcome,
            )
        return _coerce_json(raw)

    async def claim_work_round(self, work_execution_id: UUID | str) -> dict[str, Any] | None:
        work_id = _uuid(work_execution_id, "work_execution_id")
        async with self._pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT public.claim_m5_work_round($1)", work_id
            )
        return _coerce_json(raw) if raw is not None else None

    async def mark_interrupted_work_unknown(self) -> int:
        """Fence work left running by an API process that exited unexpectedly."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    UPDATE public.m5_work_rounds w
                    SET status='unknown',
                        error='Worker process stopped before recording a terminal result.',
                        finished_at=clock_timestamp()
                    FROM public.m5_tasks t
                    WHERE t.id=w.task_id AND w.status='running'
                    RETURNING t.task_session_id, w.id AS work_id
                    """
                )
                for row in rows:
                    await _remove_window_work_context(
                        conn, row["task_session_id"], row["work_id"]
                    )
                return len(rows)

    async def append_work_input(
        self,
        *,
        task_id: UUID | str,
        work_execution_id: UUID | str,
        task_session_id: UUID | str,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        task_uuid = _uuid(task_id, "task_id")
        work_id = _uuid(work_execution_id, "work_execution_id")
        session_uuid = _uuid(task_session_id, "task_session_id")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("M5 work input must not be blank")
        metadata = {
            "m5_task_id": str(task_uuid),
            "work_execution_id": str(work_id),
            "message_kind": "m5_work_input",
            "attachments": list(attachments or []),
        }
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT t.task_session_id, t.current_work_execution_id, w.status
                    FROM public.m5_tasks t
                    JOIN public.m5_work_rounds w ON w.task_id=t.id
                    WHERE t.id=$1 AND w.id=$2
                    FOR UPDATE OF t, w
                    """,
                    task_uuid,
                    work_id,
                )
                if (
                    row is None
                    or row["task_session_id"] != session_uuid
                    or row["current_work_execution_id"] != work_id
                    or row["status"] != "running"
                ):
                    raise ValueError("M5 work input does not match the current claimed task session")
                raw = await conn.fetchval(
                    """
                    SELECT public.append_chat_message(
                        $1, 'user', $2, $3::jsonb, $4, TRUE
                    )
                    """,
                    session_uuid,
                    text,
                    json.dumps(metadata),
                    f"m5-work:{work_id}:input",
                )
                return _coerce_json(raw)

    async def finish_work_round(
        self,
        *,
        task_id: UUID | str,
        work_execution_id: UUID | str,
        task_session_id: UUID | str,
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
        turn_id: UUID | str | None,
        assistant_text: str | None = None,
    ) -> bool:
        task_uuid = _uuid(task_id, "task_id")
        work_id = _uuid(work_execution_id, "work_execution_id")
        session_uuid = _uuid(task_session_id, "task_session_id")
        turn_uuid = _uuid(turn_id, "turn_id") if turn_id is not None else None
        if status not in {"completed", "failed", "unknown"}:
            raise ValueError("invalid M5 work terminal status")
        metadata = {
            "m5_task_id": str(task_uuid),
            "work_execution_id": str(work_id),
            "message_kind": "m5_work_result",
        }
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT t.task_session_id, t.current_work_execution_id, w.status
                    FROM public.m5_tasks t
                    JOIN public.m5_work_rounds w ON w.task_id=t.id
                    WHERE t.id=$1 AND w.id=$2
                    FOR UPDATE OF t, w
                    """,
                    task_uuid,
                    work_id,
                )
                if (
                    row is None
                    or row["task_session_id"] != session_uuid
                    or row["current_work_execution_id"] != work_id
                    or row["status"] != "running"
                ):
                    return False
                if status == "completed" and assistant_text and assistant_text.strip():
                    await conn.fetchval(
                        """
                        SELECT public.append_chat_message(
                            $1, 'assistant', $2, $3::jsonb, $4, TRUE
                        )
                        """,
                        session_uuid,
                        assistant_text,
                        json.dumps(metadata),
                        f"m5-work:{work_id}:assistant",
                    )
                updated = await conn.fetchval(
                    """
                    SELECT public.finish_m5_work_round(
                        $1, $2, $3, $4::jsonb, $5, $6
                    )
                    """,
                    task_uuid,
                    work_id,
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error,
                    turn_uuid,
                )
                return bool(updated)
