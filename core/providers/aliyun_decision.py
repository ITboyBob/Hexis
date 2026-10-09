"""HTTP adapter for Alibaba Cloud TypeSafe System One decisions."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from core.integration_reliability import request_json_response
from core.llm import _LLM_MAX_RETRIES

logger = logging.getLogger(__name__)


async def decision_completion(
    *,
    model: str,
    endpoint: str,
    api_key: str,
    state: Any,
    question: Mapping[str, Any],
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    """Run one System One decision without registering a general chat provider."""
    parsed = urlsplit(endpoint or "")
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path != "/compatible-mode/v1/systemone"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("情绪决策 endpoint 必须是完整的 System One HTTPS 地址")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("情绪决策 API Key 未显式提供")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("情绪决策 model 未显式提供")

    response = await request_json_response(
        "aliyun-systemone",
        "POST",
        endpoint,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json_body={
            "model": model,
            "state": state,
            "questions": {"emotion": question},
        },
        timeout=timeout_seconds,
        attempts=_LLM_MAX_RETRIES,
        retry_unsafe_methods=True,
        transport=transport,
        sleep=sleep,
    )
    document = response.json_data
    answers = document.get("answers") if isinstance(document, Mapping) else None
    answer = answers.get("emotion") if isinstance(answers, Mapping) else None
    if (
        not isinstance(answer, Mapping)
        or answer.get("type") != "choice"
        or not isinstance(answer.get("choice"), str)
    ):
        raise ValueError("情绪决策响应缺少合法的 emotion choice")

    logger.info(
        "System One emotion decision succeeded model=%s request_id=%s correlation_id=%s",
        document.get("model") if isinstance(document, Mapping) else None,
        document.get("request_id") if isinstance(document, Mapping) else None,
        response.correlation_id,
    )
    return {"content": answer["choice"], "tool_calls": [], "raw": document}
