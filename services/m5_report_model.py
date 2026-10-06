"""One-shot, isolated Chat Completions adapter for M5 reports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.llm import normalize_llm_config, resolve_api_key

try:
    import openai
except ImportError:  # pragma: no cover - exercised only in minimal installs
    openai = None


REPORT_TOOL_NAME = "report_task_progress"


def _resolved_selected_config(config: Mapping[str, Any]) -> dict[str, Any]:
    provider = str(config.get("provider") or "").strip()
    model = str(config.get("model") or "").strip()
    if model != "qwen-plus":
        raise ValueError("M5 report requires the explicitly selected qwen-plus model")
    if provider != "openai-chat-completions-endpoint":
        raise ValueError("M5 report requires an explicitly selected Chat Completions endpoint")

    endpoint = str(config.get("endpoint") or "").strip()
    timeout = config.get("timeout_seconds")
    if not endpoint:
        raise ValueError("M5 report endpoint is missing from the selected configuration")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("M5 report timeout_seconds must come from the selected configuration")

    api_key = config.get("api_key")
    if not api_key:
        api_key_env = str(config.get("api_key_env") or "").strip()
        api_key = resolve_api_key(api_key_env) if api_key_env else None
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("M5 report selected credential is unavailable")

    # Normalize only after supplying the explicitly selected credential. This
    # prevents the shared normalizer from filling an unrelated ambient key.
    normalized = normalize_llm_config(
        {
            "provider": provider,
            "model": model,
            "endpoint": endpoint,
            "api_key": api_key,
        }
    )
    return normalized | {"timeout_seconds": float(timeout)}


def _validate_report_tool(report_tool: Mapping[str, Any]) -> dict[str, Any]:
    tool = dict(report_tool)
    function = tool.get("function")
    if (
        tool.get("type") != "function"
        or not isinstance(function, Mapping)
        or function.get("name") != REPORT_TOOL_NAME
    ):
        raise ValueError("M5 report request accepts only report_task_progress")
    return tool


async def request_report_once(
    config: Mapping[str, Any],
    messages: list[dict[str, Any]],
    report_tool: Mapping[str, Any],
) -> dict[str, Any]:
    """Make exactly one non-streaming request and preserve the raw response."""
    selected = _resolved_selected_config(config)
    tool = _validate_report_tool(report_tool)
    if openai is None:
        raise RuntimeError("openai package is required for the M5 report adapter")

    client = openai.AsyncOpenAI(
        api_key=selected["api_key"],
        base_url=selected["endpoint"],
        timeout=selected["timeout_seconds"],
        max_retries=0,
    )
    try:
        response = await client.chat.completions.create(
            model=selected["model"],
            messages=messages,
            tools=[tool],
            tool_choice={
                "type": "function",
                "function": {"name": REPORT_TOOL_NAME},
            },
            parallel_tool_calls=False,
            stream=False,
            extra_body={"enable_thinking": False},
        )
        if isinstance(response, Mapping):
            return dict(response)
        model_dump = getattr(response, "model_dump", None)
        if not callable(model_dump):
            raise TypeError("M5 report SDK response has no raw mapping representation")
        return model_dump()
    finally:
        await client.close()
