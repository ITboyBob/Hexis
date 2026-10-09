"""阿里云 System One 情绪专用适配器的协议与可靠性测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from core.integration_reliability import IntegrationHttpError
from core.llm import _LLM_MAX_RETRIES
from core.providers.aliyun_decision import decision_completion

ENDPOINT = (
    "https://llm-n5dppxzleuxpia6o.cn-beijing.maas.aliyuncs.com/"
    "compatible-mode/v1/systemone"
)
MODEL = "decision-model-preview"
FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1] / "avatar_preview/fixtures/context.json"
    ).read_text(encoding="utf-8")
)
STATE = {"user_message": "请解释这段代码", **FIXTURE}
QUESTION = {
    "type": "choice",
    "instructions": "只选择一个精确情绪标签。",
    "criteria": {"neutral": "未引起明显主导反应。", "curious": "探索兴趣。"},
}


@pytest.fixture(scope="module", autouse=True)
def temp_test_db():
    yield


@pytest.fixture(scope="module", autouse=True)
def sync_test_embedding_dimension_from_db():
    yield


@pytest.fixture(scope="module", autouse=True)
def configure_agent_for_tests():
    yield


@pytest.fixture(scope="module", autouse=True)
def apply_repo_migrations():
    yield


@pytest.fixture(autouse=True)
def setup_db():
    yield


def _success(choice: str = "neutral") -> dict[str, object]:
    return {
        "model": MODEL,
        "request_id": "request-systemone-test",
        "answers": {
            "emotion": {
                "type": "choice",
                "choice": choice,
                "confidence": 0.9,
                "probabilities": {choice: 1.0},
            }
        },
    }


@pytest.mark.asyncio
async def test_decision_completion_posts_exact_systemone_contract():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["content_type"] = request.headers.get("Content-Type")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_success("curious"))

    result = await decision_completion(
        model=MODEL,
        endpoint=ENDPOINT,
        api_key="systemone-test-secret",
        state=STATE,
        question=QUESTION,
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )

    assert result == {
        "content": "curious",
        "tool_calls": [],
        "raw": _success("curious"),
    }
    assert captured == {
        "url": ENDPOINT,
        "authorization": "Bearer systemone-test-secret",
        "content_type": "application/json",
        "body": {
            "model": MODEL,
            "state": STATE,
            "questions": {"emotion": QUESTION},
        },
    }
    assert not {
        "messages",
        "temperature",
        "max_tokens",
        "response_format",
        "tools",
    }.intersection(captured["body"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"answers": []},
        {"answers": {}},
        {"answers": {"emotion": []}},
        {"answers": {"emotion": {"type": "score", "choice": "neutral"}}},
        {"answers": {"emotion": {"type": "choice", "choice": 1}}},
    ],
)
async def test_decision_completion_rejects_invalid_answer_shapes(payload):
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    with pytest.raises(ValueError, match="emotion choice"):
        await decision_completion(
            model=MODEL,
            endpoint=ENDPOINT,
            api_key="test-key",
            state=STATE,
            question=QUESTION,
            timeout_seconds=1,
            transport=transport,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"api_key": ""}, "API Key"),
        ({"api_key": "   "}, "API Key"),
        ({"model": ""}, "model"),
        ({"endpoint": ""}, "endpoint"),
        ({"endpoint": "http://example.test/compatible-mode/v1/systemone"}, "endpoint"),
        ({"endpoint": "https://example.test/v1/systemone"}, "endpoint"),
        ({"endpoint": ENDPOINT + "/"}, "endpoint"),
        ({"endpoint": ENDPOINT + "?debug=1"}, "endpoint"),
        (
            {"endpoint": "https://user:pass@example.test/compatible-mode/v1/systemone"},
            "endpoint",
        ),
    ],
)
async def test_decision_completion_rejects_invalid_configuration_before_network(
    updates, message
):
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_success())

    kwargs = {
        "model": MODEL,
        "endpoint": ENDPOINT,
        "api_key": "test-key",
        "state": STATE,
        "question": QUESTION,
        "timeout_seconds": 1,
        "transport": httpx.MockTransport(handler),
        **updates,
    }
    with pytest.raises(ValueError, match=message):
        await decision_completion(**kwargs)
    assert calls == 0


@pytest.mark.asyncio
async def test_decision_completion_retries_transient_post_with_retry_after():
    calls = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"Retry-After": "2"}, text="warming")
        if calls == 2:
            return httpx.Response(429, headers={"Retry-After": "1"}, text="limited")
        return httpx.Response(200, json=_success())

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    result = await decision_completion(
        model=MODEL,
        endpoint=ENDPOINT,
        api_key="test-key",
        state=STATE,
        question=QUESTION,
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )

    assert result["content"] == "neutral"
    assert calls == 3
    assert calls <= _LLM_MAX_RETRIES
    assert sleeps == [2.0, 1.0]


@pytest.mark.asyncio
async def test_decision_completion_retries_network_error_then_succeeds():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("temporary network failure", request=request)
        return httpx.Response(200, json=_success())

    async def no_wait(_seconds: float) -> None:
        return None

    result = await decision_completion(
        model=MODEL,
        endpoint=ENDPOINT,
        api_key="test-key",
        state=STATE,
        question=QUESTION,
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
        sleep=no_wait,
    )
    assert result["content"] == "neutral"
    assert calls == 2


@pytest.mark.asyncio
async def test_decision_completion_stops_after_configured_transient_attempts():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="still unavailable")

    async def no_wait(_seconds: float) -> None:
        return None

    with pytest.raises(IntegrationHttpError) as raised:
        await decision_completion(
            model=MODEL,
            endpoint=ENDPOINT,
            api_key="test-key",
            state=STATE,
            question=QUESTION,
            timeout_seconds=1,
            transport=httpx.MockTransport(handler),
            sleep=no_wait,
        )
    assert calls == _LLM_MAX_RETRIES
    assert raised.value.attempts == _LLM_MAX_RETRIES


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403])
async def test_decision_completion_does_not_retry_permanent_http_errors(status):
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="private-provider-body")

    with pytest.raises(IntegrationHttpError) as raised:
        await decision_completion(
            model=MODEL,
            endpoint=ENDPOINT,
            api_key="test-key",
            state=STATE,
            question=QUESTION,
            timeout_seconds=1,
            transport=httpx.MockTransport(handler),
        )
    assert calls == 1
    assert raised.value.status_code == status
    assert "private-provider-body" not in str(raised.value)


@pytest.mark.asyncio
async def test_decision_completion_does_not_retry_invalid_json():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="not-json")

    with pytest.raises(IntegrationHttpError) as raised:
        await decision_completion(
            model=MODEL,
            endpoint=ENDPOINT,
            api_key="test-key",
            state=STATE,
            question=QUESTION,
            timeout_seconds=1,
            transport=httpx.MockTransport(handler),
        )
    assert calls == 1
    assert raised.value.error_kind == "invalid_response"


@pytest.mark.asyncio
async def test_decision_completion_propagates_cancellation_during_request():
    started = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        return httpx.Response(200, json=_success())

    task = asyncio.create_task(
        decision_completion(
            model=MODEL,
            endpoint=ENDPOINT,
            api_key="test-key",
            state=STATE,
            question=QUESTION,
            timeout_seconds=1,
            transport=httpx.MockTransport(handler),
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_decision_completion_propagates_cancellation_during_backoff():
    sleeping = asyncio.Event()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="retry")

    async def sleep(_seconds: float) -> None:
        sleeping.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        decision_completion(
            model=MODEL,
            endpoint=ENDPOINT,
            api_key="test-key",
            state=STATE,
            question=QUESTION,
            timeout_seconds=1,
            transport=httpx.MockTransport(handler),
            sleep=sleep,
        )
    )
    await sleeping.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
