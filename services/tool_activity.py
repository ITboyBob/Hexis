"""将 AgentLoop 工具事件转换为 Chat Activity 的稳定展示字段。"""

from __future__ import annotations

import json
from typing import Any


def format_tool_start(event_data: dict[str, Any], *, event_id: str) -> dict[str, Any]:
    """格式化工具请求，沿用文本 Chat 的标题和 500 字参数摘要。"""
    return {
        "id": event_id,
        "kind": "tool_call",
        "title": event_data.get("tool_name", "tool"),
        "detail": json.dumps(event_data.get("arguments", {}))[:500],
    }


def _error_text(error: Any) -> str | None:
    if isinstance(error, dict):
        message = error.get("message")
        return message if isinstance(message, str) and message else None
    if isinstance(error, str) and error:
        return error
    return None


def _recall_detail(output: Any, *, tool_name: str) -> str:
    malformed = "Recall 返回结构异常：无法读取记忆正文"
    if not isinstance(output, dict):
        return malformed
    memories = output.get("memories")
    if not isinstance(memories, list) or any(
        not isinstance(memory, dict) or not isinstance(memory.get("content"), str)
        for memory in memories
    ):
        return malformed

    maximum = 2000 if tool_name == "voice_recall" else None
    lines = [
        f"{index}. {memory['content'][:maximum] if maximum else memory['content']}"
        for index, memory in enumerate(memories, 1)
    ]
    partial = output.get("partial_activations")
    if tool_name == "voice_recall" and isinstance(partial, list):
        for item in partial:
            if not isinstance(item, dict):
                continue
            name = item.get("cluster_name")
            keywords = item.get("keywords")
            if isinstance(name, str) and isinstance(keywords, list):
                rendered = ", ".join(str(keyword) for keyword in keywords[:20])
                lines.append(f"相关主题：{name}" + (f"（{rendered}）" if rendered else ""))
    if lines:
        return "\n\n".join(lines)
    return "未召回记忆"


def format_tool_result(
    event_data: dict[str, Any],
    *,
    event_id: str,
    state: str | None = None,
) -> dict[str, Any] | None:
    """格式化工具结果；轻量工具状态仅按权威 receipt state 决定。"""
    if state == "accepted":
        return None

    tool_name = event_data.get("tool_name", "tool")
    error = event_data.get("error")
    payload: dict[str, Any] = {
        "id": event_id,
        "kind": "tool_result",
        "title": tool_name,
    }

    if state == "unknown":
        detail = "工具调用结果无法确认。"
        reason = _error_text(error)
        if reason:
            detail += f"\n原因：{reason}"
        payload.update(
            detail=detail,
            status_label="结果未确认",
            state="unknown",
        )
        return payload

    success = event_data.get("success", False)
    display_output = event_data.get("display_output")
    output = event_data.get("output")
    detail = "OK" if success else "FAILED"
    if error:
        detail += f": {error}"
    elif isinstance(display_output, str) and display_output.strip():
        detail = display_output.strip()[:1000]

    if tool_name in {"recall", "voice_recall"}:
        if error:
            detail = f"FAILED: {error}"
        elif tool_name == "voice_recall" and isinstance(output, dict) and output.get("status") in {"error", "failed"}:
            reason = _error_text(output.get("error"))
            detail = f"FAILED: {reason}" if reason else "FAILED"
        elif not success:
            detail = "FAILED"
        else:
            detail = _recall_detail(output, tool_name=tool_name)

    payload["detail"] = detail
    if tool_name != "recall":
        payload["display_output"] = display_output
    ui_payload = output.get("ui") if isinstance(output, dict) else None
    if isinstance(ui_payload, dict):
        payload["ui"] = ui_payload
    return payload


__all__ = ["format_tool_result", "format_tool_start"]
