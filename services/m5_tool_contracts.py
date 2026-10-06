"""Strict host-side adapters for the approved M5 tool contract."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker


CONTRACT_PATH = Path(__file__).resolve().parent / "contracts" / "m5-tools-v1.json"
FRONTEND_TOOL_NAMES = frozenset(
    {"submit", "pend_message", "remember", "add_evidence"}
)
REPORT_TOOL_NAMES = frozenset({"report_task_progress"})


class M5ToolContractError(ValueError):
    """An M5 tool payload or receipt is outside the frozen contract."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise M5ToolContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> Any:
    raise M5ToolContractError(f"non-finite JSON number is forbidden: {value}")


def load_contract() -> dict[str, Any]:
    try:
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise M5ToolContractError("M5 tool contract artifact is unavailable") from exc
    if not isinstance(contract, dict) or contract.get("contract_version") != "m5-tool-v1":
        raise M5ToolContractError("M5 tool contract artifact has an unsupported version")
    tools = contract.get("tools")
    if not isinstance(tools, list) or len(tools) != 5:
        raise M5ToolContractError("M5 tool contract must contain five definitions")
    names = [
        item.get("function", {}).get("name")
        for item in tools
        if isinstance(item, dict) and isinstance(item.get("function"), dict)
    ]
    if len(names) != 5 or len(set(names)) != 5:
        raise M5ToolContractError("M5 tool contract names must be unique")
    if set(names) != FRONTEND_TOOL_NAMES | REPORT_TOOL_NAMES:
        raise M5ToolContractError("M5 tool names differ from the approved surfaces")
    return copy.deepcopy(contract)


def realtime_tool_definitions() -> list[dict[str, Any]]:
    """Return only the four frontend tools in Qwen Realtime's flat shape."""
    contract = load_contract()
    definitions = {
        item["function"]["name"]: item["function"] for item in contract["tools"]
    }
    return [
        {
            "type": "function",
            "name": name,
            "description": definitions[name]["description"],
            "parameters": copy.deepcopy(definitions[name]["parameters"]),
        }
        for name in ("submit", "pend_message", "remember", "add_evidence")
    ]


def _nonblank_strings(value: Any, path: str = "arguments") -> None:
    if isinstance(value, str):
        if not value.strip():
            raise M5ToolContractError(f"{path} must not be blank")
    elif isinstance(value, dict):
        for key, child in value.items():
            _nonblank_strings(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _nonblank_strings(child, f"{path}[{index}]")


def _require_traceable_source(tool_name: str, arguments: dict[str, Any]) -> None:
    sources: list[dict[str, Any]] = []
    if tool_name == "add_evidence":
        source = arguments.get("source")
        if isinstance(source, dict):
            sources.append(source)
    elif tool_name == "remember":
        source_list = arguments.get("sources", [])
        if isinstance(source_list, list):
            sources.extend(item for item in source_list if isinstance(item, dict))

    for source in sources:
        if not any(
            isinstance(source.get(key), str) and source[key].strip()
            for key in ("ref", "label")
        ):
            raise M5ToolContractError("source requires a non-empty ref or label")


def parse_tool_arguments(tool_name: str, raw_arguments: str) -> dict[str, Any]:
    """Parse one complete JSON object and validate schema and business qualifiers."""
    if not isinstance(raw_arguments, str) or not raw_arguments.strip():
        raise M5ToolContractError("tool arguments must be a non-empty JSON object")
    contract = load_contract()
    definitions = {
        item["function"]["name"]: item["function"]["parameters"]
        for item in contract["tools"]
    }
    if tool_name not in definitions:
        raise M5ToolContractError(f"unknown M5 tool: {tool_name}")
    try:
        arguments = json.loads(
            raw_arguments,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except M5ToolContractError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise M5ToolContractError("tool arguments must be one complete JSON value") from exc
    if not isinstance(arguments, dict):
        raise M5ToolContractError("tool arguments must be a JSON object")

    validator = Draft202012Validator(
        definitions[tool_name], format_checker=FormatChecker()
    )
    try:
        validator.validate(arguments)
    except Exception as exc:
        if not hasattr(exc, "message"):
            raise
        path = ".".join(str(part) for part in exc.absolute_path) or "arguments"
        raise M5ToolContractError(f"{path}: {exc.message}") from exc
    _nonblank_strings(arguments)
    _require_traceable_source(tool_name, arguments)
    return arguments


def build_receipt(
    *,
    tool_name: str,
    call_id: str,
    operation_id: UUID | str | None,
    ok: bool,
    state: str,
    data: dict[str, Any],
    error: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a schema-valid receipt tied to the supplier's actual call id."""
    if not isinstance(call_id, str) or not call_id.strip():
        raise M5ToolContractError("a real non-empty call_id is required")
    if isinstance(operation_id, UUID):
        operation_id = str(operation_id)
    if not isinstance(data, dict):
        raise M5ToolContractError("receipt data must be an object")

    receipt = {
        "contract_version": "m5-tool-v1",
        "tool_name": tool_name,
        "call_id": call_id,
        "operation_id": operation_id,
        "ok": ok,
        "state": state,
        "data": copy.deepcopy(data),
        "error": copy.deepcopy(error),
    }
    schema = load_contract()["receipt_schema"]
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(receipt)
    except Exception as exc:
        if not hasattr(exc, "message"):
            raise
        path = ".".join(str(part) for part in exc.absolute_path) or "receipt"
        raise M5ToolContractError(f"{path}: {exc.message}") from exc

    if ok and tool_name in {"remember", "add_evidence"}:
        if operation_id is None:
            raise M5ToolContractError("lightweight success requires its host operation_id")
        if state == "accepted" and "memory_id" in data:
            raise M5ToolContractError("accepted is not a completed memory write")
    if tool_name == "pend_message" and state == "selection_cancelled":
        if "message_id" in data:
            raise M5ToolContractError("cancelled selection cannot contain a message_id")
    return receipt
