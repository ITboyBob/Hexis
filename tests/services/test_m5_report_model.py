from __future__ import annotations

from types import SimpleNamespace

import pytest


def _report_tool():
    return {
        "type": "function",
        "function": {
            "name": "report_task_progress",
            "description": "仅交付工作结果摘要。",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        },
    }


def _config():
    return {
        "provider": "openai-chat-completions-endpoint",
        "model": "qwen-plus",
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "selected-test-key",
        "timeout_seconds": 17.5,
    }


@pytest.mark.asyncio
async def test_report_request_sends_only_report_tool_payload_and_preserves_raw_call(monkeypatch):
    from services import m5_report_model

    raw_response = {
        "id": "chatcmpl-test",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-real-17",
                            "type": "function",
                            "function": {
                                "name": "report_task_progress",
                                "arguments": '{"summary":"任务名称本轮回复：已完成"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured["payload"] = kwargs
            return SimpleNamespace(model_dump=lambda: raw_response)

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.chat = SimpleNamespace(
                completions=FakeCompletions()
            )

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(
        m5_report_model.openai,
        "AsyncOpenAI",
        FakeAsyncOpenAI,
    )

    result = await m5_report_model.request_report_once(
        _config(),
        [{"role": "user", "content": "基于本轮工作结果写摘要"}],
        _report_tool(),
    )

    assert captured["client"]["max_retries"] == 0
    assert captured["closed"] is True
    assert captured["client"]["timeout"] == 17.5
    assert captured["client"]["base_url"] == _config()["endpoint"]
    assert captured["payload"] == {
        "model": "qwen-plus",
        "messages": [{"role": "user", "content": "基于本轮工作结果写摘要"}],
        "tools": [_report_tool()],
        "tool_choice": {
            "type": "function",
            "function": {"name": "report_task_progress"},
        },
        "parallel_tool_calls": False,
        "stream": False,
        "extra_body": {"enable_thinking": False},
    }
    assert result == raw_response
    assert result["choices"][0]["message"]["tool_calls"][0]["id"] == "call-real-17"
    assert result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == (
        '{"summary":"任务名称本轮回复：已完成"}'
    )


@pytest.mark.asyncio
async def test_report_request_rejects_unselected_identity_or_missing_timeout(monkeypatch):
    from services import m5_report_model

    class ForbiddenClient:
        def __init__(self, **kwargs):
            pytest.fail("invalid report config must fail before constructing the client")

    monkeypatch.setattr(m5_report_model.openai, "AsyncOpenAI", ForbiddenClient)

    wrong_model = _config() | {"model": "ambient-default"}
    with pytest.raises(ValueError, match="qwen-plus"):
        await m5_report_model.request_report_once(wrong_model, [], _report_tool())

    no_timeout = _config()
    no_timeout.pop("timeout_seconds")
    with pytest.raises(ValueError, match="timeout"):
        await m5_report_model.request_report_once(no_timeout, [], _report_tool())

    no_key = _config() | {"api_key": None}
    with pytest.raises(ValueError, match="credential"):
        await m5_report_model.request_report_once(no_key, [], _report_tool())


@pytest.mark.asyncio
async def test_report_config_loader_requires_dedicated_selected_source(monkeypatch):
    from services import m5_report_runner

    class _Connection:
        async def fetchval(self, query, key):
            assert "get_config" in query
            assert key == "llm.m5_report"
            return {
                "provider": "openai-chat-completions-endpoint",
                "model": "qwen-plus",
                "endpoint": "https://selected.example/v1",
                "api_key_env": "M5_REPORT_TEST_KEY",
                "timeout_seconds": 23,
            }

    class _Acquire:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    monkeypatch.setenv("M5_REPORT_TEST_KEY", "selected-test-key")
    async def load_selected(*_args, **_kwargs):
        return _resolved_selected_config()

    monkeypatch.setattr(m5_report_runner, "load_llm_config", load_selected)
    config = await m5_report_runner.load_selected_report_config(_Pool())
    assert config["model"] == "qwen-plus"
    assert config["timeout_seconds"] == 23
    assert config["api_key"] == "selected-test-key"


def _resolved_selected_config():
    return {
        "provider": "openai-chat-completions-endpoint",
        "model": "qwen-plus",
        "endpoint": "https://selected.example/v1",
    }
