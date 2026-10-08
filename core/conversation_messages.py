"""Pure normalization and projection for durable conversation tool messages.

This module has no database or provider SDK dependency. It accepts internal
chat message rows, validates their frozen field contract, and creates only
Provider Chat Completions shaped messages after tool calls are durably paired.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID


_ROLES = {"system", "user", "assistant", "toolResult"}
_JSON_SCALARS = (str, int, float, bool, type(None))
_IMAGE_DATA_URL = re.compile(r"^data:image/(png|jpeg|webp|gif);base64,")


class ConversationMessageError(ValueError):
    """A newly written message violates the frozen conversation contract."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def _is_json_value(value: Any) -> bool:
    if isinstance(value, _JSON_SCALARS):
        return not isinstance(value, float) or value == value and abs(value) != float("inf")
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _uuid_text(value: Any, category: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConversationMessageError(category, "identity must be a non-empty UUID string")
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ConversationMessageError(category, "identity must be a UUID string") from exc


def _validate_visual_attachments(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 4:
        raise ConversationMessageError("invalid_visual_attachments", "visual attachments must be a list of at most four items")
    normalized: list[dict[str, Any]] = []
    for attachment in value:
        if not isinstance(attachment, dict):
            raise ConversationMessageError("invalid_visual_attachments", "visual attachment must be an object")
        if set(attachment) - {"data_url", "mime_type", "name"}:
            raise ConversationMessageError("invalid_visual_attachments", "visual attachment contains unsupported fields")
        data_url = attachment.get("data_url")
        mime_type = attachment.get("mime_type")
        if not isinstance(data_url, str) or not isinstance(mime_type, str):
            raise ConversationMessageError("invalid_visual_attachments", "visual attachment requires data_url and mime_type strings")
        match = _IMAGE_DATA_URL.match(data_url)
        if match is None or mime_type != f"image/{match.group(1)}":
            raise ConversationMessageError("invalid_visual_attachments", "visual attachment data URL and MIME type are unsupported or mismatched")
        name = attachment.get("name")
        if name is not None and not isinstance(name, str):
            raise ConversationMessageError("invalid_visual_attachments", "visual attachment name must be a string")
        item = {"data_url": data_url, "mime_type": mime_type}
        if name is not None:
            item["name"] = name
        normalized.append(item)
    return normalized


def _text_from_blocks(blocks: list[dict[str, Any]]) -> str:
    return "".join(block["text"] for block in blocks if block.get("type") == "text")


def _normalize_message(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        raise ConversationMessageError("invalid_message", "message must be an object")
    role = message.get("role")
    if not isinstance(role, str) or role not in _ROLES:
        raise ConversationMessageError("invalid_role", "role must be system, user, assistant, or toolResult")
    content = message.get("content")
    if not isinstance(content, str):
        raise ConversationMessageError("invalid_content", "content must be a string")
    metadata = message.get("metadata", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ConversationMessageError("invalid_metadata", "metadata must be an object")
    raw_blocks = message.get("content_blocks", [])
    if not isinstance(raw_blocks, list):
        raise ConversationMessageError("invalid_content_blocks", "content_blocks must be a JSON array")

    blocks: list[dict[str, Any]] = []
    call_ids: set[str] = set()
    for block in raw_blocks:
        if not isinstance(block, dict):
            raise ConversationMessageError("invalid_content_blocks", "each content block must be an object")
        block_type = block.get("type")
        if block_type == "text":
            if set(block) != {"type", "text"} or not isinstance(block.get("text"), str):
                raise ConversationMessageError("invalid_content_blocks", "text blocks require only type and string text")
            blocks.append({"type": "text", "text": block["text"]})
        elif block_type == "toolCall":
            if role != "assistant":
                raise ConversationMessageError("invalid_tool_call_role", "toolCall blocks are allowed only on assistant messages")
            if set(block) != {"type", "id", "name", "arguments"}:
                raise ConversationMessageError("invalid_tool_call", "toolCall requires exactly type, id, name, and arguments")
            call_id = block.get("id")
            name = block.get("name")
            arguments = block.get("arguments")
            if not isinstance(call_id, str) or not call_id.strip() or not isinstance(name, str) or not name.strip():
                raise ConversationMessageError("invalid_tool_call", "toolCall id and name must be non-empty strings")
            if call_id in call_ids:
                raise ConversationMessageError("duplicate_call_id", "toolCall ids must be unique within a message")
            if not isinstance(arguments, dict) or not _is_json_value(arguments):
                raise ConversationMessageError("invalid_tool_arguments", "toolCall arguments must be a JSON object")
            call_ids.add(call_id)
            blocks.append({"type": "toolCall", "id": call_id, "name": name, "arguments": copy.deepcopy(arguments)})
        else:
            # The image-block schema is intentionally not defined. Existing tool
            # images use metadata.model_visual_attachments and are validated below.
            raise ConversationMessageError("invalid_content_blocks", "unsupported content block discriminator")

    tool_result = metadata.get("tool_result")
    if role == "toolResult":
        if not isinstance(tool_result, dict):
            raise ConversationMessageError("invalid_tool_result", "toolResult requires metadata.tool_result")
        required = {"toolCallId", "toolName", "isError", "details"}
        if not required.issubset(tool_result):
            raise ConversationMessageError("invalid_tool_result", "tool_result is missing a required field")
        if not isinstance(tool_result["toolCallId"], str) or not tool_result["toolCallId"].strip():
            raise ConversationMessageError("invalid_tool_result", "toolCallId must be a non-empty string")
        if not isinstance(tool_result["toolName"], str) or not tool_result["toolName"].strip():
            raise ConversationMessageError("invalid_tool_result", "toolName must be a non-empty string")
        if type(tool_result["isError"]) is not bool:
            raise ConversationMessageError("invalid_is_error", "isError must be a JSON boolean")
        details = tool_result["details"]
        if details is not None and (not isinstance(details, (dict, list)) or not _is_json_value(details)):
            raise ConversationMessageError("invalid_details", "details must be a JSON object, array, or null")
    elif tool_result is not None:
        raise ConversationMessageError("invalid_tool_result", "metadata.tool_result is allowed only on toolResult messages")

    visuals = _validate_visual_attachments(metadata.get("model_visual_attachments"))
    if visuals and role != "toolResult":
        raise ConversationMessageError("invalid_visual_attachments", "model_visual_attachments are supported only on toolResult messages")

    text_projection = _text_from_blocks(blocks)
    if text_projection and text_projection != content:
        raise ConversationMessageError("content_projection_mismatch", "content must equal the text-block compatibility projection")
    if role == "assistant" and any(block["type"] == "toolCall" for block in blocks) and not text_projection and content:
        raise ConversationMessageError("invalid_tool_bubble", "tool-only assistant content must be empty")

    normalized = copy.deepcopy(message)
    normalized["role"] = role
    normalized["content"] = content
    normalized["content_blocks"] = blocks
    normalized["metadata"] = copy.deepcopy(metadata)
    if visuals:
        normalized["metadata"]["model_visual_attachments"] = visuals
    return normalized


def _message_session_id(message: dict[str, Any], default_session_id: str | None) -> str | None:
    value = message.get("session_id", default_session_id)
    if value is None:
        return None
    return _uuid_text(str(value), "invalid_session_identity")


def _message_turn_id(message: dict[str, Any]) -> str | None:
    metadata = message.get("metadata") or {}
    value = metadata.get("agent_turn_id")
    if value is None:
        return None
    return _uuid_text(value, "invalid_turn_identity")


def normalize_history(
    messages: Any,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Validate a history without mutating it; incomplete old pairs stay diagnostic."""
    if not isinstance(messages, list):
        return {"valid": False, "error_category": "invalid_history", "messages": [], "replay_allowed": False}
    try:
        normalized = [_normalize_message(message) for message in messages]
        seen_sources: dict[tuple[str | None, str], tuple[Any, ...]] = {}
        calls: dict[tuple[str | None, str | None, str], dict[str, Any]] = {}
        results: dict[tuple[str | None, str | None, str], dict[str, Any]] = {}
        calls_by_id: dict[tuple[str | None, str], list[tuple[str | None, str | None, str]]] = {}
        diagnostics: list[dict[str, str]] = []
        deduped: list[dict[str, Any]] = []

        for message in normalized:
            source_id = message.get("source_message_id")
            session = _message_session_id(message, session_id)
            turn = _message_turn_id(message)
            # Diagnose conflicting call identity before the broader source
            # content check, including when both messages share a source id.
            for block in message["content_blocks"]:
                if block["type"] != "toolCall":
                    continue
                key = (session, turn, block["id"])
                signature = (block["name"], json.dumps(block["arguments"], ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                previous_call = calls.get(key)
                if previous_call is not None:
                    prior_signature = (previous_call["name"], json.dumps(previous_call["arguments"], ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                    if prior_signature != signature:
                        raise ConversationMessageError("call_arguments_conflict", "same session/turn/call id has different tool name or arguments")
                    continue
                calls[key] = block
                calls_by_id.setdefault((session, block["id"]), []).append(key)
                if session is None or turn is None:
                    diagnostics.append({"code": "call_missing_turn_identity", "message": "historical tool call lacks a session or actual agent turn identity"})

            if source_id is not None:
                if not isinstance(source_id, str) or not source_id.strip():
                    raise ConversationMessageError("invalid_source_identity", "source_message_id must be null or non-empty text")
                source_key = (session, source_id)
                signature = (
                    message["role"], message["content"],
                    json.dumps(message["content_blocks"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    turn,
                    json.dumps(message["metadata"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                )
                previous = seen_sources.get(source_key)
                if previous is not None:
                    if previous != signature:
                        raise ConversationMessageError("source_identity_conflict", "same source_message_id has different message content or identity")
                    continue
                seen_sources[source_key] = signature
            deduped.append(message)

            if message["metadata"].get("action_receipts"):
                diagnostics.append({"code": "legacy_action_receipts_without_tool_pair", "message": "legacy receipt evidence is read-only and cannot replay a tool"})

            if message["role"] == "toolResult":
                tool_result = message["metadata"]["tool_result"]
                key = (session, turn, tool_result["toolCallId"])
                if session is None or turn is None:
                    diagnostics.append({"code": "result_missing_turn_identity", "message": "historical tool result lacks a session or actual agent turn identity"})
                if key in results:
                    prior = results[key]
                    if prior["content"] != message["content"] or prior["metadata"]["tool_result"] != tool_result:
                        raise ConversationMessageError("call_result_conflict", "same session/turn/call id has different tool result")
                else:
                    results[key] = message

        for key, result in results.items():
            call = calls.get(key)
            tool_result = result["metadata"]["tool_result"]
            if call is None:
                other_turn_calls = calls_by_id.get((key[0], key[2]), [])
                if other_turn_calls:
                    raise ConversationMessageError("turn_identity_mismatch", "tool result and call belong to different session/turn identities")
                diagnostics.append({"code": "tool_result_without_call", "message": "orphan historical tool result is not sent to the Provider"})
            elif call["name"] != tool_result["toolName"]:
                raise ConversationMessageError("tool_name_mismatch", "tool result name does not match its call")
        for key in calls:
            if key not in results:
                diagnostics.append({"code": "tool_call_without_result", "message": "tool call is incomplete and will not be replayed"})

        return {
            "valid": True,
            "messages": deduped,
            "diagnostics": diagnostics,
            "historical_incomplete": bool(diagnostics),
            "replay_allowed": False,
        }
    except ConversationMessageError as exc:
        return {
            "valid": False,
            "messages": [],
            "diagnostics": [{"code": exc.category, "message": str(exc)}],
            "error_category": exc.category,
            "historical_incomplete": False,
            "replay_allowed": False,
        }


def validate_new_message(message: Mapping[str, Any], *, session_id: str) -> dict[str, Any]:
    """Validate a new persisted message, including identities that old rows may omit."""
    if not isinstance(message, Mapping):
        raise ConversationMessageError("invalid_message", "message must be an object")
    if "content_blocks" not in message:
        raise ConversationMessageError("invalid_content_blocks", "new messages require content_blocks")
    normalized = _normalize_message(dict(message))
    if _message_session_id(normalized, session_id) is None:
        raise ConversationMessageError("invalid_session_identity", "new messages require a session identity")
    source_id = normalized.get("source_message_id")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ConversationMessageError("invalid_source_identity", "new messages require a stable source_message_id")
    blocks = normalized["content_blocks"]
    has_text = any(block["type"] == "text" for block in blocks)
    has_call = any(block["type"] == "toolCall" for block in blocks)
    if normalized["content"] and not has_text:
        raise ConversationMessageError("invalid_content_blocks", "non-empty new message content requires a text block")
    if (normalized["role"] == "assistant" and has_call) or normalized["role"] == "toolResult":
        turn_id = _message_turn_id(normalized)
        if turn_id is None:
            raise ConversationMessageError("invalid_turn_identity", "new tool messages require the real agent_turn_id")
    call_blocks = [block for block in blocks if block["type"] == "toolCall"]
    if len(call_blocks) > 1:
        raise ConversationMessageError("invalid_tool_call", "persist each tool call with its own source identity")
    if call_blocks:
        expected = f"tool:{turn_id}:{call_blocks[0]['id']}:call"
        if source_id != expected:
            raise ConversationMessageError("invalid_source_identity", "tool call source identity must bind the actual turn and call id")
    if normalized["role"] == "toolResult":
        result = normalized["metadata"]["tool_result"]
        expected = f"tool:{turn_id}:{result['toolCallId']}:result"
        if source_id != expected:
            raise ConversationMessageError("invalid_source_identity", "tool result source identity must bind the actual turn and call id")
    return normalized


def _compat_text(message: dict[str, Any]) -> str:
    blocks = message.get("content_blocks")
    if isinstance(blocks, list):
        text = _text_from_blocks(blocks)
        if text:
            return text
    return message.get("content", "") if isinstance(message.get("content"), str) else ""


def project_memory_texts(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    """Project ordinary user/assistant prose only; structured tool data is excluded."""
    projected: list[str] = []
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") not in {"user", "assistant"}:
            continue
        blocks = message.get("content_blocks", [])
        if message.get("role") == "assistant" and isinstance(blocks, list) and any(
            isinstance(block, dict) and block.get("type") == "toolCall" for block in blocks
        ):
            text_blocks = [block for block in blocks if isinstance(block, dict) and block.get("type") == "text"]
            text = "".join(block.get("text", "") for block in text_blocks if isinstance(block.get("text"), str))
        else:
            text = _compat_text(dict(message))
        if isinstance(text, str) and text.strip():
            projected.append(text)
    return projected


def _tool_key(message: dict[str, Any], session_id: str | None) -> tuple[str | None, str | None, str] | None:
    session = _message_session_id(message, session_id)
    turn = _message_turn_id(message)
    metadata = message.get("metadata") or {}
    tool_result = metadata.get("tool_result")
    call_id = tool_result.get("toolCallId") if isinstance(tool_result, dict) else None
    if not isinstance(call_id, str) or not call_id:
        return None
    return session, turn, call_id


def _tool_call_provider_shape(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": block["id"],
        "type": "function",
        "function": {
            "name": block["name"],
            "arguments": json.dumps(block["arguments"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        },
    }


def _visual_provider_messages(message: dict[str, Any]) -> list[dict[str, Any]]:
    attachments = (message.get("metadata") or {}).get("model_visual_attachments") or []
    output: list[dict[str, Any]] = []
    for attachment in attachments:
        name = attachment.get("name") or "tool image"
        output.append({
            "role": "user",
            "content": [
                {"type": "input_text", "text": f"[Fresh tool-provided visual context: {name}]"},
                {"type": "input_image", "image_url": attachment["data_url"], "detail": "auto"},
            ],
        })
    return output


def internal_to_provider(
    messages: Sequence[Mapping[str, Any]],
    *,
    session_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Convert paired internal history to Provider messages without metadata leakage."""
    normalized = normalize_history(list(messages), session_id=session_id)
    if not normalized["valid"]:
        diagnostic = normalized["diagnostics"][0]
        raise ConversationMessageError(diagnostic["code"], diagnostic["message"])
    source = normalized["messages"]
    diagnostics = copy.deepcopy(normalized["diagnostics"])
    calls: dict[tuple[str | None, str | None, str], dict[str, Any]] = {}
    results: dict[tuple[str | None, str | None, str], dict[str, Any]] = {}
    for message in source:
        session = _message_session_id(message, session_id)
        turn = _message_turn_id(message)
        for block in message["content_blocks"]:
            if block["type"] == "toolCall":
                calls[(session, turn, block["id"])] = block
        if message["role"] == "toolResult":
            key = _tool_key(message, session_id)
            if key is not None:
                results[key] = message

    emitted_results: set[tuple[str | None, str | None, str]] = set()
    provider_messages: list[dict[str, Any]] = []
    index = 0
    while index < len(source):
        message = source[index]
        index += 1
        role = message["role"]
        if role == "toolResult":
            key = _tool_key(message, session_id)
            if key in calls:
                # Emitted immediately after its assistant call below.
                continue
            provider_messages.append({"role": "assistant", "content": "[历史工具结果未与调用配对；未发送该结果，也不会重放。]"})
            continue
        if role == "toolResult":
            continue
        if role in {"system", "user"}:
            text = _compat_text(message)
            provider_messages.append({"role": role, "content": text})
            continue

        call_blocks = [block for block in message["content_blocks"] if block["type"] == "toolCall"]
        text = _compat_text(message)
        if not call_blocks:
            if text:
                provider_messages.append({"role": "assistant", "content": text})
            continue
        session = _message_session_id(message, session_id)
        turn = _message_turn_id(message)
        # Durable calls have one source identity per row; adjacent calls from
        # the same real turn share one Provider assistant tool_calls message.
        while index < len(source):
            adjacent = source[index]
            adjacent_calls = [block for block in adjacent["content_blocks"] if block["type"] == "toolCall"]
            if (adjacent["role"] != "assistant" or not adjacent_calls
                    or _message_session_id(adjacent, session_id) != session
                    or _message_turn_id(adjacent) != turn):
                break
            call_blocks.extend(adjacent_calls)
            text += _compat_text(adjacent)
            index += 1
        paired: list[tuple[dict[str, Any], dict[str, Any], tuple[str | None, str | None, str]]] = []
        complete = session is not None and turn is not None
        for block in call_blocks:
            key = (session, turn, block["id"])
            result = results.get(key)
            if result is None or result["metadata"]["tool_result"]["toolName"] != block["name"]:
                complete = False
                continue
            paired.append((block, result, key))
        if not complete or len(paired) != len(call_blocks):
            diagnostics.append({"code": "tool_pair_incomplete", "message": "incomplete calls are converted to ordinary history text and never replayed"})
            note = "[历史工具调用/结果未完整配对；不会重放工具。]"
            provider_messages.append({"role": "assistant", "content": f"{text}\n{note}" if text else note})
            continue
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": text if text else None,
            "tool_calls": [_tool_call_provider_shape(block) for block, _, _ in paired],
        }
        provider_messages.append(assistant_message)
        visual_messages: list[dict[str, Any]] = []
        for block, result, key in paired:
            provider_messages.append({"role": "tool", "tool_call_id": block["id"], "content": _compat_text(result)})
            visual_messages.extend(_visual_provider_messages(result))
            emitted_results.add(key)
        provider_messages.extend(visual_messages)
    return provider_messages, diagnostics


def _parse_native_m5_receipt(content: str, call_id: str, tool_name: str) -> dict[str, Any] | None:
    try:
        receipt = json.loads(content)
    except (TypeError, ValueError):
        return None
    required = {"contract_version", "tool_name", "call_id", "operation_id", "ok", "state", "data", "error"}
    if not isinstance(receipt, dict) or set(receipt) != required:
        return None
    if receipt.get("contract_version") != "m5-tool-v1" or receipt.get("call_id") != call_id or receipt.get("tool_name") != tool_name:
        return None
    if type(receipt.get("ok")) is not bool or not isinstance(receipt.get("state"), str):
        return None
    return {"toolName": tool_name, "isError": not receipt["ok"], "details": receipt}


def provider_to_internal(
    messages: Sequence[Mapping[str, Any]],
    *,
    session_id: str,
    turn_id: str | None = None,
    tool_results: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Normalize Provider messages; result status must come from explicit host evidence."""
    session = _uuid_text(session_id, "invalid_session_identity")
    normalized_turn = _uuid_text(turn_id, "invalid_turn_identity") if turn_id is not None else None
    result_metadata = tool_results or {}
    output: list[dict[str, Any]] = []
    diagnostics: list[dict[str, str]] = []
    calls_by_id: dict[str, tuple[str, dict[str, Any]]] = {}

    for message in messages:
        if not isinstance(message, Mapping):
            raise ConversationMessageError("invalid_provider_message", "Provider message must be an object")
        role = message.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            raise ConversationMessageError("invalid_provider_role", "Provider role is unsupported")
        content = message.get("content")
        text = content if isinstance(content, str) else "" if content is None else None
        if text is None:
            if isinstance(content, list):
                text_parts = [part.get("text") for part in content if isinstance(part, Mapping) and part.get("type") in {"text", "input_text", "output_text"} and isinstance(part.get("text"), str)]
                text = "".join(text_parts)
            else:
                raise ConversationMessageError("invalid_provider_content", "Provider content must be text, null, or a text-part array")
        if role in {"system", "user"}:
            output.append({"role": role, "content": text, "content_blocks": [{"type": "text", "text": text}] if text else [], "metadata": {}})
            continue
        if role == "assistant":
            raw_calls = message.get("tool_calls") or []
            if not isinstance(raw_calls, list):
                raise ConversationMessageError("invalid_tool_calls", "Provider tool_calls must be an array")
            if raw_calls and normalized_turn is None:
                raise ConversationMessageError("invalid_turn_identity", "Provider tool calls require the actual agent_turn_id before persistence")
            message_call_ids: set[str] = set()
            for call_index, raw_call in enumerate(raw_calls):
                if not isinstance(raw_call, Mapping):
                    raise ConversationMessageError("invalid_tool_call", "Provider tool call must be an object")
                call_id = raw_call.get("id")
                function = raw_call.get("function")
                if not isinstance(call_id, str) or not call_id.strip() or not isinstance(function, Mapping):
                    raise ConversationMessageError("invalid_tool_call", "Provider tool call requires id and function")
                if call_id in message_call_ids:
                    raise ConversationMessageError("duplicate_call_id", "Provider tool call ids must be unique within a message")
                message_call_ids.add(call_id)
                name = function.get("name")
                raw_arguments = function.get("arguments")
                if isinstance(raw_arguments, str):
                    try:
                        arguments = json.loads(raw_arguments)
                    except ValueError as exc:
                        raise ConversationMessageError("invalid_tool_arguments", "Provider tool arguments are not valid JSON") from exc
                else:
                    arguments = raw_arguments
                if not isinstance(arguments, dict) or not _is_json_value(arguments):
                    raise ConversationMessageError("invalid_tool_arguments", "Provider tool arguments must decode to a JSON object")
                if not isinstance(name, str) or not name.strip():
                    raise ConversationMessageError("invalid_tool_call", "Provider function name must be non-empty text")
                block = {"type": "toolCall", "id": call_id, "name": name, "arguments": copy.deepcopy(arguments)}
                calls_by_id[call_id] = (name, block)
                call_text = text if call_index == 0 else ""
                blocks = [{"type": "text", "text": call_text}] if call_text else []
                output.append({
                    "role": "assistant",
                    "session_id": session,
                    "source_message_id": f"tool:{normalized_turn}:{call_id}:call",
                    "content": call_text,
                    "content_blocks": [*blocks, block],
                    "metadata": {"agent_turn_id": normalized_turn},
                })
            if not raw_calls:
                output.append({"role": "assistant", "content": text, "content_blocks": [{"type": "text", "text": text}] if text else [], "metadata": {}})
            continue

        call_id = message.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            diagnostics.append({"code": "provider_tool_result_missing_call_id", "message": "Provider tool result has no call id and is not persisted"})
            continue
        call = calls_by_id.get(call_id)
        if call is None:
            diagnostics.append({"code": "provider_orphan_tool_result", "message": "Provider tool result has no matching call and is not persisted"})
            continue
        metadata = result_metadata.get(call_id)
        if metadata is None:
            metadata = _parse_native_m5_receipt(text, call_id, call[0])
        if metadata is None:
            diagnostics.append({"code": "provider_tool_result_missing_authority", "message": "Provider text alone cannot establish tool success/error status"})
            continue
        if metadata.get("toolName") != call[0] or type(metadata.get("isError")) is not bool:
            raise ConversationMessageError("invalid_tool_result", "host tool result metadata does not match the Provider call")
        details = metadata.get("details")
        if details is not None and (not isinstance(details, (dict, list)) or not _is_json_value(details)):
            raise ConversationMessageError("invalid_details", "host tool result details must be an object, array, or null")
        result = {
            "role": "toolResult",
            "session_id": session,
            "source_message_id": f"tool:{normalized_turn}:{call_id}:result",
            "content": text,
            "content_blocks": [{"type": "text", "text": text}] if text else [],
            "metadata": {
                "agent_turn_id": normalized_turn,
                "tool_result": {
                    "toolCallId": call_id,
                    "toolName": call[0],
                    "isError": metadata["isError"],
                    "details": copy.deepcopy(details),
                },
            },
        }
        attachments = metadata.get("model_visual_attachments")
        if attachments is not None:
            result["metadata"]["model_visual_attachments"] = _validate_visual_attachments(attachments)
        if normalized_turn is None:
            diagnostics.append({"code": "provider_result_missing_turn_identity", "message": "Provider tool result is not yet bound to a real agent turn"})
        output.append(result)

    return output, diagnostics
