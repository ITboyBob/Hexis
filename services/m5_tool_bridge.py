"""Bind realtime tool calls to authorized voice inputs and exact call ids."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import UUID

from services.m5_tool_contracts import (
    FRONTEND_TOOL_NAMES,
    M5ToolContractError,
    build_receipt,
    parse_tool_arguments,
)


@dataclass(frozen=True)
class BoundToolCall:
    actual_call_id: str
    name: str
    raw_arguments: str
    origin_session_id: UUID
    input_turn_id: str
    input_order: int
    stimulus_revision: int
    response_id: str
    supplier_response_id: str
    request_id: str | None
    tool_surface: str


ToolHandler = Callable[[BoundToolCall, dict[str, Any]], Awaitable[dict[str, Any]]]


def bind_tool_event(
    session: Any,
    pending: dict[str, BoundToolCall],
    *,
    expected_response_id: str,
    event: Any,
) -> BoundToolCall:
    """Bind one completed supplier event to its active response and input."""
    supplier_response_id = event.response_id
    if (
        not expected_response_id
        or not isinstance(supplier_response_id, str)
        or session.local_response_id(supplier_response_id) != expected_response_id
    ):
        raise ValueError("tool event response id does not map to the active response")
    bound = session.authorize_voice_tool_call(
        response_id=expected_response_id,
        supplier_response_id=supplier_response_id,
        request_id=event.event_id,
        call_id=event.call_id or "",
        name=event.tool_name or "",
        arguments=event.tool_arguments or "",
    )
    existing = pending.get(bound.actual_call_id)
    if existing is not None and existing != bound:
        raise ValueError("supplier call id conflicts with the response's earlier event")
    pending.setdefault(bound.actual_call_id, bound)
    return bound


def _failed_receipt(
    call: BoundToolCall,
    *,
    code: str,
    message: str,
) -> str:
    return json.dumps(
        build_receipt(
            tool_name=call.name,
            call_id=call.actual_call_id,
            operation_id=None,
            ok=False,
            state="failed",
            data={},
            error={"code": code, "message": message, "retryable": False},
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def execute_bound_tool(
    session: Any,
    call: BoundToolCall,
    *,
    handlers: dict[str, ToolHandler] | None = None,
) -> str:
    """Validate the bound input before parsing or invoking a real handler."""
    session.validate_voice_tool_call(call)
    if call.tool_surface != "m5" or call.name not in FRONTEND_TOOL_NAMES:
        return _failed_receipt(
            call,
            code="TOOL_NOT_ALLOWED",
            message=f"Tool {call.name} is not available on this frontend surface.",
        )
    try:
        arguments = parse_tool_arguments(call.name, call.raw_arguments)
    except M5ToolContractError as exc:
        message = str(exc)
        if message.startswith("unknown M5 tool:"):
            return _failed_receipt(call, code="TOOL_NOT_ALLOWED", message=message)
        code = (
            "INVALID_JSON"
            if any(token in message.lower() for token in ("json", "duplicate", "non-finite"))
            else "INVALID_ARGUMENTS"
        )
        return _failed_receipt(call, code=code, message=message)

    handler = (handlers or {}).get(call.name)
    if handler is None:
        return _failed_receipt(
            call,
            code="TOOL_NOT_ALLOWED",
            message=f"No backend handler is available for {call.name} in this runtime.",
        )

    result = await handler(call, arguments)
    receipt = build_receipt(
        tool_name=call.name,
        call_id=call.actual_call_id,
        operation_id=result.get("operation_id"),
        ok=result.get("ok"),
        state=result.get("state"),
        data=result.get("data", {}),
        error=result.get("error"),
    )
    return json.dumps(
        receipt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
