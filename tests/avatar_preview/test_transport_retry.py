import asyncio
import inspect
from types import SimpleNamespace

import httpx
from openai import APIStatusError
import pytest

from core import llm
from services import avatar_emotion

CONTEXT = {
    "identity": [],
    "worldview": [],
    "goals": [],
    "relationships": [],
}
CONFIG = {
    "provider": "openai-chat-completions-endpoint",
    "model": "qwen-plus",
    "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "resolved_api_key": "unit-test-secret",
    "timeout_seconds": 1,
}


def _response(content):
    message = SimpleNamespace(content=content, tool_calls=[])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _client(create):
    completions = SimpleNamespace(create=create)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def _status_error(status_code, message):
    request = httpx.Request(
        "POST", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    )
    response = httpx.Response(status_code=status_code, request=request)
    return APIStatusError(message, response=response, body=None)


@pytest.mark.asyncio
async def test_public_evaluate_retries_transient_transport_inside_one_call(monkeypatch):
    evaluate = getattr(avatar_emotion, "evaluate", None)
    assert callable(evaluate), "Phase 2 must add the independent evaluator"
    attempts = 0
    request_args = []

    async def no_wait(_seconds):
        return None

    async def transport(**kwargs):
        nonlocal attempts
        request_args.append(kwargs)
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("simulated transient connection error")
        if attempts == 2:
            raise _status_error(503, "temporary service error")
        if attempts == 3:
            raise _status_error(429, "rate limited")
        return _response("curious")

    monkeypatch.setattr(llm, "_should_try_responses", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        llm, "_get_openai_client", lambda *_args, **_kwargs: _client(transport)
    )
    monkeypatch.setattr(asyncio, "sleep", no_wait)

    result = await evaluate("有新发现", CONTEXT, CONFIG)
    assert result["label"] == "curious"
    assert result["logical_call_count"] == 1
    assert result["transport_attempts"] is None
    assert attempts == 4
    assert all(call["model"] == "qwen-plus" for call in request_args)
    assert all(
        call["max_tokens"]
        == inspect.signature(llm.chat_completion).parameters["max_tokens"].default
        for call in request_args
    )


@pytest.mark.asyncio
async def test_public_evaluate_does_not_retry_auth_failure_or_invalid_label(
    monkeypatch,
):
    evaluate = getattr(avatar_emotion, "evaluate", None)
    assert callable(evaluate), "Phase 2 must add the independent evaluator"
    attempts = 0

    async def unauthorized(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise _status_error(401, "unauthorized")

    monkeypatch.setattr(llm, "_should_try_responses", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        llm, "_get_openai_client", lambda *_args, **_kwargs: _client(unauthorized)
    )
    with pytest.raises(APIStatusError, match="unauthorized"):
        await evaluate("问题", CONTEXT, CONFIG)
    assert attempts == 1

    async def invalid(**_kwargs):
        nonlocal attempts
        attempts += 1
        return _response("Happy")

    monkeypatch.setattr(
        llm, "_get_openai_client", lambda *_args, **_kwargs: _client(invalid)
    )
    attempts = 0
    with pytest.raises(ValueError, match="精确的 v1"):
        await evaluate("问题", CONTEXT, CONFIG)
    assert attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["\nhappy", "happy\n"])
async def test_chat_completion_wrapper_does_not_normalize_newlines_as_labels(
    monkeypatch, content
):
    evaluate = getattr(avatar_emotion, "evaluate", None)
    assert callable(evaluate), "Phase 2 must add the independent evaluator"
    attempts = 0

    async def with_newline(**_kwargs):
        nonlocal attempts
        attempts += 1
        return _response(content)

    monkeypatch.setattr(llm, "_should_try_responses", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        llm, "_get_openai_client", lambda *_args, **_kwargs: _client(with_newline)
    )
    with pytest.raises(ValueError, match="精确的 v1"):
        await evaluate("问题", CONTEXT, CONFIG)
    assert attempts == 1


@pytest.mark.asyncio
async def test_public_evaluate_timeout_and_cancellation_do_not_add_service_retries(
    monkeypatch,
):
    evaluate = getattr(avatar_emotion, "evaluate", None)
    assert callable(evaluate), "Phase 2 must add the independent evaluator"
    attempts = 0
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked(**_kwargs):
        nonlocal attempts
        attempts += 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(llm, "_should_try_responses", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        llm, "_get_openai_client", lambda *_args, **_kwargs: _client(blocked)
    )
    with pytest.raises(asyncio.TimeoutError):
        await evaluate("超时", CONTEXT, {**CONFIG, "timeout_seconds": 0.01})
    assert attempts == 1

    attempts = 0
    started.clear()
    cancelled.clear()
    task = asyncio.create_task(evaluate("取消", CONTEXT, CONFIG))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await cancelled.wait()
    assert attempts == 1
