"""Google Cloud Code Assist SSE streaming client.

Used by ``google-gemini-cli`` and ``google-antigravity`` providers.
Implements the Gemini REST API via SSE (``alt=sse``) as used by Google's
Cloud Code Assist service (not the public Gemini API).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from core.integration_reliability import IntegrationHttpError, iter_sse_json_events

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GEMINI_CLI_USER_AGENT = "google-api-nodejs-client/9.15.1"
_GEMINI_CLI_API_CLIENT = "google-cloud-sdk"

_ANTIGRAVITY_USER_AGENT = "google-api-nodejs-client/9.15.1"
_ANTIGRAVITY_API_CLIENT = "google-cloud-sdk vscode_cloudshelleditor/0.1"

_CLIENT_METADATA_GEMINI = json.dumps({
    "ideType": "IDE_UNSPECIFIED",
    "platform": "PLATFORM_UNSPECIFIED",
    "pluginType": "GEMINI",
})

_CLIENT_METADATA_ANTIGRAVITY = json.dumps({
    "ideType": "IDE_UNSPECIFIED",
    "platform": "PLATFORM_UNSPECIFIED",
    "pluginType": "GEMINI",
})


# ---------------------------------------------------------------------------
# Message / Tool conversion
# ---------------------------------------------------------------------------

def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"text", "input_text", "output_text"}:
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content or "")


def _image_url_from_part(part: dict[str, Any]) -> str | None:
    raw = part.get("image_url")
    if isinstance(raw, dict):
        url = raw.get("url")
        return str(url) if url else None
    if isinstance(raw, str):
        return raw
    url = part.get("url")
    return str(url) if url else None


def _data_url_parts(url: str) -> tuple[str | None, str | None]:
    if not url.startswith("data:") or "," not in url:
        return None, None
    header, data = url.split(",", 1)
    media_type = header[5:].split(";", 1)[0] or None
    return media_type, data


def _convert_content_parts(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        text = _content_text(content)
        return [{"text": text}] if text else []

    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in {"text", "input_text", "output_text"}:
            text = part.get("text")
            if isinstance(text, str) and text:
                parts.append({"text": text})
            continue
        if ptype in {"image_url", "input_image"}:
            url = _image_url_from_part(part)
            if not url:
                continue
            media_type, data = _data_url_parts(url)
            if media_type and data:
                parts.append({
                    "inlineData": {
                        "mimeType": media_type,
                        "data": data,
                    }
                })
    return parts or ([{"text": _content_text(content)}] if _content_text(content) else [])


def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    """Convert Chat Completions messages to Gemini ``contents`` format.

    Returns (system_instruction, contents).
    """
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    call_id_to_name: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls") or []:
            function = tool_call.get("function", {})
            call_id = str(tool_call.get("id") or "")
            name = str(function.get("name") or "")
            if call_id and name:
                call_id_to_name[call_id] = name

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "") or ""

        if role == "system":
            text = _content_text(content)
            if text.strip():
                system_parts.append(text)
            continue

        if role == "user":
            contents.append({
                "role": "user",
                "parts": _convert_content_parts(content),
            })
            continue

        if role == "assistant":
            parts: list[dict[str, Any]] = []
            if content:
                text = _content_text(content)
                if text:
                    parts.append({"text": text})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                args = fn.get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                parts.append({
                    "functionCall": {
                        "name": fn.get("name", ""),
                        "args": args,
                    }
                })
            if parts:
                contents.append({"role": "model", "parts": parts})
            continue

        if role == "tool":
            # Tool result → functionResponse
            tool_content = content
            if isinstance(tool_content, str):
                try:
                    tool_content = json.loads(tool_content)
                except Exception:
                    tool_content = {"result": tool_content}
            contents.append({
                "role": "function",
                "parts": [{
                    "functionResponse": {
                        "name": msg.get("name") or call_id_to_name.get(
                            str(msg.get("tool_call_id") or "")
                        ) or "tool",
                        "response": tool_content if isinstance(tool_content, dict) else {"result": str(tool_content)},
                    }
                }],
            })

    system = "\n\n".join(p for p in system_parts if p.strip()) or None
    return system, contents


def _convert_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Convert OpenAI-format tools to Gemini ``functionDeclarations``."""
    if not tools:
        return None
    decls: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function", {})
        name = fn.get("name")
        if not name:
            continue
        decl: dict[str, Any] = {
            "name": name,
            "description": fn.get("description", ""),
        }
        params = fn.get("parameters")
        if params:
            decl["parameters"] = params
        decls.append(decl)
    if not decls:
        return None
    return [{"functionDeclarations": decls}]


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------

def _build_headers(access_token: str, project_id: str, *, is_antigravity: bool) -> dict[str, str]:
    headers: dict[str, str] = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": _ANTIGRAVITY_USER_AGENT if is_antigravity else _GEMINI_CLI_USER_AGENT,
        "X-Goog-Api-Client": _ANTIGRAVITY_API_CLIENT if is_antigravity else _GEMINI_CLI_API_CLIENT,
    }
    if project_id:
        headers["X-Goog-User-Project"] = project_id
    # Client-Metadata
    headers["Client-Metadata"] = _CLIENT_METADATA_ANTIGRAVITY if is_antigravity else _CLIENT_METADATA_GEMINI
    return headers


def _build_request_body(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    system_prompt: str | None,
    project_id: str,
) -> dict[str, Any]:
    system_from_msgs, contents = _convert_messages(messages)
    gemini_tools = _convert_tools(tools)

    body: dict[str, Any] = {
        "contents": contents,
    }

    # System instruction
    sys_text = system_prompt or system_from_msgs
    if sys_text:
        body["systemInstruction"] = {
            "parts": [{"text": sys_text}],
        }

    if gemini_tools:
        body["tools"] = gemini_tools

    # Generation config
    body["generationConfig"] = {
        "candidateCount": 1,
    }

    return body


def _build_url(endpoint: str, model: str, *, stream: bool) -> str:
    base = endpoint.rstrip("/")
    method = "streamGenerateContent" if stream else "generateContent"
    url = f"{base}/v1internal/{model}:{method}"
    if stream:
        url += "?alt=sse"
    return url


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------

def _parse_candidates(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Extract text + tool calls from a Gemini response/chunk."""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for candidate in data.get("candidates") or []:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            if "text" in part:
                text_parts.append(part["text"])
            if "functionCall" in part:
                fc = part["functionCall"]
                tool_calls.append({
                    "id": str(uuid.uuid4()),
                    "name": fc.get("name", ""),
                    "arguments": fc.get("args") or {},
                })

    return "".join(text_parts), tool_calls


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def google_code_assist_completion(
    endpoint: str,
    access_token: str,
    project_id: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    is_antigravity: bool = False,
    system_prompt: str | None = None,
) -> dict[str, Any]:
    """Non-streaming Cloud Code Assist completion."""
    url = _build_url(endpoint, model, stream=False)
    headers = _build_headers(access_token, project_id, is_antigravity=is_antigravity)
    body = _build_request_body(
        messages, tools, system_prompt=system_prompt, project_id=project_id,
    )

    timeout = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=body)
    if resp.status_code < 200 or resp.status_code >= 300:
        raise RuntimeError(
            f"Google Code Assist request failed: HTTP {resp.status_code}: {resp.text}"
        )

    data = resp.json()
    text, tool_calls = _parse_candidates(data)
    return {"content": text, "tool_calls": tool_calls, "raw": data}


async def stream_google_code_assist_completion(
    endpoint: str,
    access_token: str,
    project_id: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    is_antigravity: bool = False,
    system_prompt: str | None = None,
    on_text_delta: Any | None = None,
) -> dict[str, Any]:
    """Streaming Cloud Code Assist completion via SSE.

    Returns the same shape as ``google_code_assist_completion()``.
    Optionally calls ``on_text_delta(text)`` for each text token.
    """
    import asyncio

    url = _build_url(endpoint, model, stream=True)
    headers = _build_headers(access_token, project_id, is_antigravity=is_antigravity)
    body = _build_request_body(
        messages, tools, system_prompt=system_prompt, project_id=project_id,
    )

    all_text: list[str] = []
    all_tool_calls: list[dict[str, Any]] = []

    timeout = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)
    try:
        async for chunk in iter_sse_json_events(
            "google-code-assist",
            "POST",
            url,
            headers=headers,
            json_body=body,
            timeout=timeout,
            attempts=3,
            max_delay=60.0,
            retry_unsafe_methods=True,
        ):
            text, tool_calls = _parse_candidates(chunk)
            if text:
                all_text.append(text)
                if on_text_delta:
                    result = on_text_delta(text)
                    if asyncio.iscoroutine(result):
                        await result
            if tool_calls:
                all_tool_calls.extend(tool_calls)
    except IntegrationHttpError as exc:
        raise RuntimeError(str(exc)) from exc

    return {"content": "".join(all_text), "tool_calls": all_tool_calls, "raw": None}
