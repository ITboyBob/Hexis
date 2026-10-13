"""Authoritative M5 task and submit persistence adapters."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from services.behavior_events import (
    BehaviorEvent,
    record_behavior_event_on_connection,
)


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
_RTC_AUTHORIZATION_FIELDS = {"input_modality", "media_session_id", "provider_item_id"}
_PENDING_AUTHORIZATION_FIELDS = _AUTHORIZATION_FIELDS | {"task_id"}


def task_event_stream_id(origin_session_id: UUID | str) -> str:
    return f"m5-tasks:{_uuid(origin_session_id, 'origin_session_id')}"


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

    async def _task_view(
        self, conn: Any, origin_session_id: UUID, task_id: UUID
    ) -> dict[str, Any]:
        task = await conn.fetchrow(
            """
            SELECT id, origin_session_id, task_description, task_session_id,
                   product_state, revision
            FROM public.m5_tasks
            WHERE id=$1 AND origin_session_id=$2
            """,
            task_id,
            origin_session_id,
        )
        if task is None:
            raise RuntimeError("M5 task disappeared before its projection event was saved")
        pending = await conn.fetch(
            """
            SELECT id, receive_order, message
            FROM public.m5_pending_messages
            WHERE task_id=$1 AND claimed_work_execution_id IS NULL
            ORDER BY receive_order
            """,
            task_id,
        )
        return {
            "task_id": str(task["id"]),
            "task_title": task["task_description"],
            "task_state": task["product_state"],
            "task_session_id": str(task["task_session_id"]),
            "revision": int(task["revision"]),
            "pending": [
                {
                    "message_id": str(message["id"]),
                    "receive_order": int(message["receive_order"]),
                    "message": message["message"],
                }
                for message in pending
            ],
        }

    async def record_task_changed_on_connection(
        self,
        conn: Any,
        origin_session_id: UUID,
        task_id: UUID,
        *,
        authorization: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not conn.is_in_transaction():
            raise ValueError("the supplied M5 task event connection must have an active transaction")
        view = await self._task_view(conn, origin_session_id, task_id)
        identity = authorization or {}
        await record_behavior_event_on_connection(
            conn,
            BehaviorEvent(
                event_id=uuid4(),
                schema_version=1,
                event_type="m5_task_changed",
                stream_id=task_event_stream_id(origin_session_id),
                session_id=origin_session_id,
                request_id=identity.get("request_id"),
                stimulus_revision=identity.get("stimulus_revision"),
                input_turn_id=identity.get("input_turn_id"),
                response_id=identity.get("response_id"),
                task_id=str(task_id),
                call_id=identity.get("actual_call_id"),
                ts=datetime.now(timezone.utc),
                source="m5-task-service",
                producer_id=f"m5-task:{task_id}",
                producer_seq=view["revision"],
                channel="m5-task",
                payload={"origin_session_id": str(origin_session_id), **view},
            ),
        )
        await conn.execute(
            "SELECT pg_notify('m5_task_events',$1)", str(origin_session_id)
        )
        return view

    async def list_tasks_for_origin(
        self, origin_session_id: UUID | str
    ) -> dict[str, Any] | None:
        """Read one origin's task view and event cursor from one DB snapshot."""
        origin_id = _uuid(origin_session_id, "origin_session_id")
        stream_id = task_event_stream_id(origin_id)
        async with self._pool.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                active = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM public.chat_sessions WHERE id=$1 AND status='active')",
                    origin_id,
                )
                if not active:
                    return None
                tasks = await conn.fetch(
                    """
                    SELECT id, origin_session_id, task_description, task_session_id,
                           product_state, revision
                    FROM public.m5_tasks
                    WHERE origin_session_id=$1
                    ORDER BY created_at DESC, id
                    """,
                    origin_id,
                )
                task_ids = [task["id"] for task in tasks]
                pending_rows = (
                    await conn.fetch(
                        """
                        SELECT task_id, id, receive_order, message
                        FROM public.m5_pending_messages
                        WHERE task_id=ANY($1::uuid[])
                          AND claimed_work_execution_id IS NULL
                        ORDER BY task_id, receive_order
                        """,
                        task_ids,
                    )
                    if task_ids
                    else []
                )
                pending_by_task: dict[UUID, list[dict[str, Any]]] = {
                    task_id: [] for task_id in task_ids
                }
                for message in pending_rows:
                    pending_by_task[message["task_id"]].append(
                        {
                            "message_id": str(message["id"]),
                            "receive_order": int(message["receive_order"]),
                            "message": message["message"],
                        }
                    )
                open_selections = await conn.fetch(
                    """
                    SELECT selection_id, version, message
                    FROM public.m5_selections
                    WHERE origin_session_id=$1 AND state='open'
                    ORDER BY created_at, selection_id
                    """,
                    origin_id,
                )
                cursor = await conn.fetchval(
                    "SELECT COALESCE(MAX(seq),0) FROM public.behavior_events WHERE stream_id=$1 AND session_id=$2",
                    stream_id,
                    origin_id,
                )
        return {
            "origin_session_id": str(origin_id),
            "event_cursor": {"m5-tasks": int(cursor)},
            "selections": [
                {
                    "selection_id": str(selection["selection_id"]),
                    "version": int(selection["version"]),
                    "message": selection["message"],
                }
                for selection in open_selections
            ],
            "tasks": [
                {
                    "task_id": str(task["id"]),
                    "task_title": task["task_description"],
                    "task_state": task["product_state"],
                    "task_session_id": str(task["task_session_id"]),
                    "revision": int(task["revision"]),
                    "pending": pending_by_task[task["id"]],
                }
                for task in tasks
            ],
        }

    async def submit_m5_task(
        self,
        origin_session_id: UUID | str,
        command_id: UUID | str,
        description: str,
        original_request: str | None,
        authorization: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically accept a task and preserve its original business result."""
        origin_id = _uuid(origin_session_id, "origin_session_id")
        command_uuid = _uuid(command_id, "command_id")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("task description must not be blank")
        if not isinstance(authorization, dict):
            raise ValueError("host authorization identity must be an object")
        if original_request is None:
            if authorization.get("input_modality") != "audio":
                raise ValueError("audio source metadata is required without an original user transcript")
            _uuid(authorization.get("media_session_id"), "media_session_id")
            if not isinstance(authorization.get("provider_item_id"), str) or not authorization["provider_item_id"].strip():
                raise ValueError("audio provider item identity is required")
        elif not isinstance(original_request, str) or not original_request.strip() or "input_modality" in authorization:
            raise ValueError("original text request must not be blank or mixed with audio metadata")
        unknown = set(authorization) - (_AUTHORIZATION_FIELDS | _RTC_AUTHORIZATION_FIELDS)
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
            async with conn.transaction():
                raw = await conn.fetchval(
                    "SELECT public.submit_m5_task($1, $2, $3, $4, $5::jsonb)",
                    origin_id,
                    command_uuid,
                    description,
                    original_request,
                    json.dumps(host_authorization, ensure_ascii=False),
                )
                result = _coerce_json(raw)
                if result.get("accepted") and not result.get("replayed"):
                    await self.record_task_changed_on_connection(
                        conn,
                        origin_id,
                        UUID(str(result["task_id"])),
                        authorization=host_authorization,
                    )
        return result

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
                    SELECT id, origin_session_id, product_state, revision
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
                result = {
                    "accepted": True,
                    "task_id": str(task["id"]),
                    "task_session_id": str(session_uuid),
                    "work_execution_id": str(work_id),
                    "task_state": "working",
                    "revision": int(task["revision"]) + 1,
                }
                await self.record_task_changed_on_connection(
                    conn, task["origin_session_id"], task["id"]
                )
                return result

    async def append_m5_message(
        self,
        origin_session_id: UUID | str,
        task_id: UUID | str,
        command_id: UUID | str,
        message: str,
        authorization: dict[str, Any],
        *,
        connection: Any | None = None,
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
        if connection is not None:
            if not connection.is_in_transaction():
                raise ValueError("the supplied M5 connection must have an active transaction")
            return await self._append_m5_message_on_connection(
                connection,
                origin_uuid,
                task_uuid,
                command_uuid,
                message,
                host_authorization,
            )
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                return await self._append_m5_message_on_connection(
                    conn,
                    origin_uuid,
                    task_uuid,
                    command_uuid,
                    message,
                    host_authorization,
                )

    async def _append_m5_message_on_connection(
        self,
        conn: Any,
        origin_uuid: UUID,
        task_uuid: UUID,
        command_uuid: UUID,
        message: str,
        authorization: dict[str, Any],
    ) -> dict[str, Any]:
        raw = await conn.fetchval(
            "SELECT public.append_m5_message($1,$2,$3,$4,$5::jsonb)",
            origin_uuid,
            task_uuid,
            command_uuid,
            message,
            json.dumps(authorization, ensure_ascii=False),
        )
        result = _coerce_json(raw)
        if result.get("accepted") and not result.get("replayed"):
            if not result.get("work_execution_id"):
                await conn.execute(
                    "UPDATE public.m5_tasks SET revision=revision+1,updated_at=clock_timestamp() WHERE id=$1 AND origin_session_id=$2",
                    task_uuid,
                    origin_uuid,
                )
            await self.record_task_changed_on_connection(
                conn,
                origin_uuid,
                task_uuid,
                authorization=authorization,
            )
        return result

    async def claim_next_m5_message(self, task_id: UUID | str) -> dict[str, Any] | None:
        task_uuid = _uuid(task_id, "task_id")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                origin_id = await conn.fetchval(
                    "SELECT origin_session_id FROM public.m5_tasks WHERE id=$1",
                    task_uuid,
                )
                raw = await conn.fetchval(
                    "SELECT public.claim_next_m5_message($1)", task_uuid
                )
                if raw is None:
                    return None
                result = _coerce_json(raw)
                if result and origin_id is not None:
                    await self.record_task_changed_on_connection(conn, origin_id, task_uuid)
                return result or None

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
            async with conn.transaction():
                raw = await conn.fetchval(
                    "SELECT public.finish_m5_round($1,$2,$3)",
                    task_uuid,
                    work_id,
                    report_outcome,
                )
                result = _coerce_json(raw)
                if result.get("finished"):
                    origin_id = await conn.fetchval(
                        "SELECT origin_session_id FROM public.m5_tasks WHERE id=$1",
                        task_uuid,
                    )
                    await self.record_task_changed_on_connection(conn, origin_id, task_uuid)
                return result

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
                    RETURNING t.id AS task_id,t.origin_session_id,
                              t.task_session_id,w.id AS work_id
                    """
                )
                for row in rows:
                    await conn.execute(
                        """
                        INSERT INTO public.m5_reports(
                            task_id,origin_session_id,work_execution_id
                        ) VALUES($1,$2,$3)
                        ON CONFLICT (work_execution_id) DO NOTHING
                        """,
                        row["task_id"],
                        row["origin_session_id"],
                        row["work_id"],
                    )
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
                    SELECT public.append_chat_message_blocks(
                        $1, 'user', $2, $3::jsonb, $4::jsonb, $5, TRUE
                    )
                    """,
                    session_uuid,
                    text,
                    json.dumps([{"type": "text", "text": text}]),
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
                    SELECT t.task_session_id, t.current_work_execution_id,
                           t.origin_session_id, w.status, w.input, w.turn_id
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
                if row["turn_id"] is not None and turn_uuid is not None and row["turn_id"] != turn_uuid:
                    raise ValueError("M5 work completion turn does not match its host-bound AgentLoop turn")
                if status == "completed" and assistant_text is not None and (
                    row["turn_id"] is None or turn_uuid != row["turn_id"]
                ):
                    raise ValueError("completed M5 work memory requires its host-bound AgentLoop turn")
                work_result = dict(result or {})
                if status == "completed" and assistant_text is not None:
                    source_identity = f"m5-work:{work_id}"
                    input_source_message_id = f"{source_identity}:input"
                    assistant_source_message_id = f"{source_identity}:assistant"
                    input_message = await conn.fetchrow(
                        "SELECT role,content,metadata FROM public.chat_messages "
                        "WHERE session_id=$1 AND source_message_id=$2 FOR UPDATE",
                        session_uuid,
                        input_source_message_id,
                    )
                    if (
                        input_message is None
                        or input_message["role"] != "user"
                        or input_message["content"] != row["input"]
                    ):
                        raise ValueError("M5 work input source is missing or conflicts with the claimed execution")
                    input_metadata = _coerce_json(input_message["metadata"])
                    if (
                        input_metadata.get("m5_task_id") != str(task_uuid)
                        or input_metadata.get("work_execution_id") != str(work_id)
                        or input_metadata.get("message_kind") != "m5_work_input"
                    ):
                        raise ValueError("M5 work input metadata does not match its claimed execution")
                    memory_context = {
                        "metadata": {"type": "conversation"},
                        "surface": "m5-task",
                        "host_background": True,
                        "source_identity": source_identity,
                        "user_source_message_id": input_source_message_id,
                        "assistant_source_message_id": assistant_source_message_id,
                        "user_metadata": input_metadata,
                        "assistant_metadata": {
                            **metadata,
                            **({"agent_turn_id": str(turn_uuid)} if turn_uuid is not None else {}),
                        },
                    }
                    saved_turn = _coerce_json(await conn.fetchval(
                        "SELECT public.record_chat_session_turn($1::uuid,$2,$3,'m5-task',$4::jsonb)",
                        session_uuid,
                        input_message["content"],
                        assistant_text,
                        json.dumps(memory_context, ensure_ascii=False),
                    ))
                    memory_result = saved_turn.get("memory") if isinstance(saved_turn, dict) else None
                    raw_memory = memory_result.get("raw") if isinstance(memory_result, dict) else None
                    memory_status = (
                        raw_memory.get("status") if isinstance(raw_memory, dict) else None
                    )
                    if (
                        not isinstance(memory_result, dict)
                        or memory_result.get("status") in {"failed", "conflict"}
                        or memory_status not in {"stored", "duplicate"}
                    ):
                        error_text = (
                            memory_result.get("error")
                            if isinstance(memory_result, dict)
                            else None
                        ) or f"unexpected RecMem result status: {memory_status!r}"
                        raise RuntimeError(f"M5 conversation memory write failed: {error_text}")
                    work_result["memory_status"] = memory_status
                    work_result["raw_unit_id"] = raw_memory.get("unit_id")
                updated = await conn.fetchval(
                    """
                    SELECT public.finish_m5_work_round(
                        $1, $2, $3, $4::jsonb, $5, $6
                    )
                    """,
                    task_uuid,
                    work_id,
                    status,
                    json.dumps(work_result, ensure_ascii=False) if result is not None or work_result else None,
                    error,
                    turn_uuid,
                )
                return bool(updated)

    async def repair_work_memory(
        self, work_execution_id: UUID | str
    ) -> dict[str, Any]:
        """Retry only the durable conversation-memory projection for a finished turn."""
        work_id = _uuid(work_execution_id, "work_execution_id")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT t.id AS task_id, t.task_session_id, w.status, w.input,
                           w.turn_id, turn.status AS turn_status,
                           turn.result->>'text' AS assistant_text,
                           turn.result->>'stopped_reason' AS stopped_reason,
                           COALESCE((turn.result->>'timed_out')::boolean,FALSE) AS timed_out
                    FROM public.m5_work_rounds w
                    JOIN public.m5_tasks t ON t.id=w.task_id
                    JOIN public.agent_turns turn
                      ON turn.id=w.turn_id AND turn.session_id=t.task_session_id
                    WHERE w.id=$1
                    FOR UPDATE OF w
                    """,
                    work_id,
                )
                if row is None or row["status"] != "unknown":
                    raise ValueError("only an unknown M5 work round with a bound turn can be repaired")
                if (
                    row["turn_status"] != "completed"
                    or row["stopped_reason"] != "completed"
                    or row["timed_out"]
                    or row["assistant_text"] is None
                ):
                    raise ValueError("M5 work turn has no completed final response to repair")

                task_id = row["task_id"]
                session_id = row["task_session_id"]
                source_identity = f"m5-work:{work_id}"
                input_source_id = f"{source_identity}:input"
                assistant_source_id = f"{source_identity}:assistant"
                input_message = await conn.fetchrow(
                    "SELECT role,content,metadata FROM public.chat_messages "
                    "WHERE session_id=$1 AND source_message_id=$2 FOR UPDATE",
                    session_id,
                    input_source_id,
                )
                if (
                    input_message is None
                    or input_message["role"] != "user"
                    or input_message["content"] != row["input"]
                ):
                    raise ValueError("M5 work input source is missing or conflicts with the finished execution")
                input_metadata = _coerce_json(input_message["metadata"])
                if (
                    input_metadata.get("m5_task_id") != str(task_id)
                    or input_metadata.get("work_execution_id") != str(work_id)
                    or input_metadata.get("message_kind") != "m5_work_input"
                ):
                    raise ValueError("M5 work input metadata does not match its finished execution")

                context = {
                    "metadata": {"type": "conversation"},
                    "surface": "m5-task",
                    "host_background": True,
                    "source_identity": source_identity,
                    "user_source_message_id": input_source_id,
                    "assistant_source_message_id": assistant_source_id,
                    "user_metadata": input_metadata,
                    "assistant_metadata": {
                        "m5_task_id": str(task_id),
                        "work_execution_id": str(work_id),
                        "message_kind": "m5_work_result",
                        "agent_turn_id": str(row["turn_id"]),
                    },
                }
                saved_turn = _coerce_json(await conn.fetchval(
                    "SELECT public.record_chat_session_turn($1::uuid,$2,$3,'m5-task',$4::jsonb)",
                    session_id,
                    input_message["content"],
                    row["assistant_text"],
                    json.dumps(context, ensure_ascii=False),
                ))
                memory_result = saved_turn.get("memory")
                raw_memory = memory_result.get("raw") if isinstance(memory_result, dict) else None
                memory_status = raw_memory.get("status") if isinstance(raw_memory, dict) else None
                if (
                    not isinstance(memory_result, dict)
                    or memory_result.get("status") in {"failed", "conflict"}
                    or memory_status not in {"stored", "duplicate"}
                ):
                    error_text = (
                        memory_result.get("error")
                        if isinstance(memory_result, dict)
                        else None
                    ) or f"unexpected RecMem result status: {memory_status!r}"
                    raise RuntimeError(f"M5 conversation memory repair failed: {error_text}")

                await conn.execute(
                    "UPDATE public.m5_work_rounds SET result=COALESCE(result,'{}'::jsonb) "
                    "|| jsonb_build_object('memory_status',$2::text,'raw_unit_id',$3::text) WHERE id=$1",
                    work_id,
                    memory_status,
                    raw_memory.get("unit_id"),
                )
                return {
                    "repaired": True,
                    "memory_status": memory_status,
                    "raw_unit_id": raw_memory.get("unit_id"),
                }
