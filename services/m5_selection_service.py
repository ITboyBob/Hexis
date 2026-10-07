"""Persist user-confirmed M5 pending-message selections."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from services.behavior_events import BehaviorEvent, record_behavior_event_on_connection
from services.m5_task_service import M5TaskService, _coerce_json, _uuid, task_event_stream_id


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


def _selection_digest(message: str) -> str:
    canonical = json.dumps(
        {"tool_name": "pend_message", "arguments": {"message": message}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_bound_call(
    origin_session_id: UUID, authorization: dict[str, Any], message: str
) -> None:
    if not isinstance(message, str) or not message.strip():
        raise ValueError("pending message must not be blank")
    if not isinstance(authorization, dict):
        raise ValueError("host authorization identity must be an object")
    unknown = set(authorization) - _AUTHORIZATION_FIELDS
    if unknown:
        raise ValueError(f"host authorization contains unsupported fields: {sorted(unknown)}")
    if authorization.get("origin_session_id") != str(origin_session_id):
        raise ValueError("authorization origin does not match the source chat")
    if authorization.get("actual_call_id") is None or not str(
        authorization["actual_call_id"]
    ).strip():
        raise ValueError("a real supplier call_id must be bound by the host")
    if not isinstance(authorization.get("input_turn_id"), str):
        raise ValueError("a formally authorized input_turn_id is required")
    _uuid(authorization["input_turn_id"], "input_turn_id")
    for name in ("response_id", "supplier_response_id"):
        if not isinstance(authorization.get(name), str) or not authorization[name].strip():
            raise ValueError(f"a real {name} is required")
    for name in ("input_order", "stimulus_revision"):
        value = authorization.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"authorized {name} must be a non-negative integer")


def _selection_event(
    *,
    origin_session_id: UUID,
    selection_id: UUID,
    version: int,
    event_type: str,
    authorization: dict[str, Any],
    state: str,
    message: str | None = None,
    chosen_task_id: str | None = None,
) -> BehaviorEvent:
    payload: dict[str, Any] = {
        "origin_session_id": str(origin_session_id),
        "selection_id": str(selection_id),
        "version": version,
        "state": state,
    }
    if message is not None:
        payload["message"] = message
    if chosen_task_id is not None:
        payload["chosen_task_id"] = chosen_task_id
    return BehaviorEvent(
        event_id=uuid4(),
        schema_version=1,
        event_type=event_type,
        stream_id=task_event_stream_id(origin_session_id),
        session_id=origin_session_id,
        request_id=authorization.get("request_id"),
        stimulus_revision=authorization.get("stimulus_revision"),
        input_turn_id=authorization.get("input_turn_id"),
        response_id=authorization.get("response_id"),
        call_id=authorization.get("actual_call_id"),
        ts=datetime.now(timezone.utc),
        source="m5-selection-service",
        producer_id=f"m5-selection:{selection_id}",
        producer_seq=version,
        channel="m5-task",
        payload=payload,
    )


async def _write_event_and_notify(
    conn: Any, origin_session_id: UUID, event: BehaviorEvent
) -> None:
    await record_behavior_event_on_connection(conn, event)
    await conn.execute("SELECT pg_notify('m5_task_events',$1)", str(origin_session_id))


class M5SelectionService:
    def __init__(self, pool: Any):
        if pool is None:
            raise ValueError("a database pool is required for M5 selections")
        self._pool = pool

    async def wait_for_resolution(
        self, *, selection_id: UUID | str, origin_session_id: UUID | str
    ) -> dict[str, Any]:
        """Await the durable choice using the existing M5 notification channel."""
        selection_uuid = _uuid(selection_id, "selection_id")
        origin_id = _uuid(origin_session_id, "origin_session_id")
        changed = asyncio.Event()

        def on_notify(_conn: Any, _pid: int, _channel: str, payload: str) -> None:
            if payload == str(origin_id):
                changed.set()

        async with self._pool.acquire() as conn:
            await conn.add_listener("m5_task_events", on_notify)
            try:
                while True:
                    changed.clear()
                    row = await conn.fetchrow(
                        """SELECT s.state,s.origin_session_id,a.result,c.status AS origin_status
                           FROM public.m5_selections s
                           JOIN public.m5_actions a ON a.command_id=s.command_id
                           JOIN public.chat_sessions c ON c.id=s.origin_session_id
                           WHERE s.selection_id=$1""",
                        selection_uuid,
                    )
                    if row is None or row["origin_session_id"] != origin_id:
                        return {"accepted": False, "error": {"code": "SELECTION_NOT_FOUND", "retryable": False}}
                    if row["state"] == "confirmed" and row["result"] is not None:
                        return {"accepted": True, "state": "confirmed", "result": _coerce_json(row["result"])}
                    if row["state"] == "cancelled":
                        return {"accepted": True, "state": "cancelled"}
                    if row["state"] != "open" or row["origin_status"] != "active":
                        return {"accepted": False, "error": {"code": "SELECTION_EXPIRED", "retryable": False}}
                    try:
                        # Same reread cadence as the durable M5 SSE stream; no expiry TTL.
                        await asyncio.wait_for(changed.wait(), timeout=15.0)
                    except asyncio.TimeoutError:
                        continue
            finally:
                await conn.remove_listener("m5_task_events", on_notify)

    async def begin_pend_message(
        self,
        *,
        origin_session_id: UUID | str,
        command_id: UUID | str,
        authorization: dict[str, Any],
        message: str,
    ) -> dict[str, Any]:
        origin_id = _uuid(origin_session_id, "origin_session_id")
        command_uuid = _uuid(command_id, "command_id")
        _validate_bound_call(origin_id, authorization, message)
        selection_id = uuid4()
        digest = _selection_digest(message)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                raw = await conn.fetchval(
                    """SELECT public.begin_m5_selection(
                        $1,$2,$3,$4,$5::jsonb,$6,$7
                    )""",
                    selection_id,
                    command_uuid,
                    origin_id,
                    authorization["actual_call_id"],
                    json.dumps(authorization, ensure_ascii=False),
                    message,
                    digest,
                )
                result = _coerce_json(raw)
                if not result.get("accepted"):
                    return result
                if not result.get("replayed"):
                    await _write_event_and_notify(
                        conn,
                        origin_id,
                        _selection_event(
                            origin_session_id=origin_id,
                            selection_id=UUID(result["selection_id"]),
                            version=int(result["version"]),
                            event_type="m5_selection_opened",
                            authorization=authorization,
                            state=str(result["state"]),
                            message=message,
                        ),
                    )
                return result

    async def _load_open_selection(
        self,
        conn: Any,
        selection_id: UUID,
        version: int,
        *,
        origin_session_id: UUID | str | None,
    ) -> dict[str, Any]:
        row = await conn.fetchrow(
            "SELECT * FROM public.m5_selections WHERE selection_id=$1 FOR UPDATE",
            selection_id,
        )
        if row is None:
            return {
                "accepted": False,
                "error": {"code": "SELECTION_NOT_FOUND", "retryable": False},
            }
        origin_id = row["origin_session_id"]
        if origin_session_id is not None and _uuid(
            origin_session_id, "origin_session_id"
        ) != origin_id:
            return {
                "accepted": False,
                "error": {"code": "SELECTION_ORIGIN_MISMATCH", "retryable": False},
            }
        if row["state"] != "open":
            return {"accepted": True, "closed": True, "selection": dict(row)}
        if int(row["version"]) != version:
            return {
                "accepted": False,
                "error": {"code": "SELECTION_VERSION_CONFLICT", "retryable": False},
            }
        return {"accepted": True, "closed": False, "selection": dict(row)}

    async def _close_selection(
        self,
        conn: Any,
        row: dict[str, Any],
        *,
        version: int,
        state: str,
        task_id: UUID | None = None,
    ) -> dict[str, Any]:
        origin_id = row["origin_session_id"]
        authorization = _coerce_json(row["authorization"])
        raw = await conn.fetchval(
            "SELECT public.close_m5_selection($1,$2,$3,$4,$5)",
            row["selection_id"],
            origin_id,
            version,
            state,
            task_id,
        )
        result = _coerce_json(raw)
        if result.get("accepted") and not result.get("replayed"):
            final_state = str(result["state"])
            await _write_event_and_notify(
                conn,
                origin_id,
                _selection_event(
                    origin_session_id=origin_id,
                    selection_id=row["selection_id"],
                    version=int(result["version"]),
                    event_type="m5_selection_closed",
                    authorization=authorization,
                    state=final_state,
                    chosen_task_id=(
                        str(result["chosen_task_id"])
                        if result.get("chosen_task_id") is not None
                        else None
                    ),
                ),
            )
        return result

    async def confirm_pend_message(
        self,
        *,
        selection_id: UUID | str,
        version: int,
        task_id: UUID | str,
        origin_session_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        selection_uuid = _uuid(selection_id, "selection_id")
        task_uuid = _uuid(task_id, "task_id")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValueError("selection version must be a positive integer")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                loaded = await self._load_open_selection(
                    conn,
                    selection_uuid,
                    version,
                    origin_session_id=origin_session_id,
                )
                if not loaded.get("accepted"):
                    return loaded
                row = loaded["selection"]
                if loaded["closed"]:
                    if row["state"] != "confirmed" or row["chosen_task_id"] != task_uuid:
                        return {
                            "accepted": False,
                            "error": {"code": "SELECTION_ALREADY_CLOSED", "retryable": False},
                        }
                    action = await conn.fetchval(
                        "SELECT result FROM public.m5_actions WHERE command_id=$1",
                        row["command_id"],
                    )
                    replay = _coerce_json(action)
                    return replay | {"replayed": True}

                origin_status = await conn.fetchval(
                    "SELECT status FROM public.chat_sessions WHERE id=$1 FOR UPDATE",
                    row["origin_session_id"],
                )
                if origin_status != "active":
                    await self._close_selection(
                        conn, row, version=version, state="expired"
                    )
                    return {
                        "accepted": False,
                        "state": "failed",
                        "selection_id": str(selection_uuid),
                        "error": {"code": "SELECTION_EXPIRED", "retryable": False},
                    }

                authorization = _coerce_json(row["authorization"])
                authorization.pop("selection_argument_digest", None)
                task_authorization = dict(authorization) | {"task_id": str(task_uuid)}
                appended = await M5TaskService(self._pool).append_m5_message(
                    row["origin_session_id"],
                    task_uuid,
                    row["command_id"],
                    row["message"],
                    task_authorization,
                    connection=conn,
                )
                if not appended.get("accepted"):
                    return appended
                closed = await self._close_selection(
                    conn,
                    row,
                    version=version,
                    state="confirmed",
                    task_id=task_uuid,
                )
                if not closed.get("accepted"):
                    return closed
                return appended | {
                    "selection_id": str(selection_uuid),
                    "selection_state": closed["state"],
                    "selection_version": closed["version"],
                    "replayed": bool(appended.get("replayed")),
                }

    async def cancel_pend_message(
        self,
        *,
        selection_id: UUID | str,
        version: int,
        origin_session_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        selection_uuid = _uuid(selection_id, "selection_id")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValueError("selection version must be a positive integer")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                loaded = await self._load_open_selection(
                    conn,
                    selection_uuid,
                    version,
                    origin_session_id=origin_session_id,
                )
                if not loaded.get("accepted"):
                    return loaded
                row = loaded["selection"]
                if loaded["closed"]:
                    if row["state"] != "cancelled":
                        return {
                            "accepted": False,
                            "error": {"code": "SELECTION_ALREADY_CLOSED", "retryable": False},
                        }
                    return {
                        "accepted": True,
                        "state": "selection_cancelled",
                        "selection_id": str(selection_uuid),
                        "replayed": True,
                    }
                closed = await self._close_selection(
                    conn,
                    row,
                    version=version,
                    state="cancelled",
                )
                if not closed.get("accepted"):
                    return closed
                if closed["state"] == "expired":
                    return {
                        "accepted": False,
                        "state": "failed",
                        "selection_id": str(selection_uuid),
                        "error": {
                            "code": "SELECTION_EXPIRED",
                            "retryable": False,
                        },
                    }
                return {
                    "accepted": True,
                    "state": "selection_cancelled",
                    "selection_id": str(selection_uuid),
                    "version": closed["version"],
                    "replayed": False,
                }
