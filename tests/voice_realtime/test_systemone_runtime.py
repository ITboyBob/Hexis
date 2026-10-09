"""System One 经 _build_runtime 进入语音情绪门控的集成测试。"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from apps.voice_realtime_harness import _build_runtime
from core import integration_reliability
from core.providers import aliyun_decision
from services.voice_instructions import eligible_gate_emotion, render_emotion

ENDPOINT = (
    "https://llm-n5dppxzleuxpia6o.cn-beijing.maas.aliyuncs.com/"
    "compatible-mode/v1/systemone"
)


def _config():
    return {
        "db_dsn_env": "EDM_RUNTIME_DATABASE_URL",
        "memory_limit": 10,
        "emotion": {
            "provider": "aliyun-systemone",
            "model": "decision-model-preview",
            "endpoint": ENDPOINT,
            "api_key_env": "DASHSCOPE_API_KEY",
            "timeout_ms": 1000,
        },
    }


def _identity(session_id):
    return {
        "session_id": str(session_id),
        "input_turn_id": str(uuid4()),
        "input_order": 0,
        "stimulus_revision": 0,
    }


class _Connection:
    async def fetchval(self, query):
        assert query == "SELECT get_appraisal_db_context()"
        return json.dumps(
            {
                "identity": [{"name": "Hexis"}],
                "worldview": [{"belief": "以事实为准"}],
                "goals": [{"goal": "帮助用户"}],
            }
        )


class _Acquire:
    async def __aenter__(self):
        return _Connection()

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def acquire(self):
        return _Acquire()


def _install_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    async def controlled_request(provider, method, url, **kwargs):
        kwargs["transport"] = transport

        async def no_wait(_seconds):
            return None

        kwargs["sleep"] = no_wait
        return await integration_reliability.request_json_response(
            provider, method, url, **kwargs
        )

    monkeypatch.setattr(aliyun_decision, "request_json_response", controlled_request)


def _hydration():
    return SimpleNamespace(
        per_response_instructions="受控上下文",
        instructions_hash="a" * 64,
        hydrate_ms=1.0,
        memory_count=0,
        persona_context="Hexis",
        continuity_context="## Conversation Continuity Packet\n当前会话",
        memory_context="## Memory Context\n无相关记忆",
        temporal_context="2026-10-09 12:00:00 (UTC)",
        token_count=1,
        tokenizer="test",
        tokenizer_is_model_exact=False,
    )


@pytest.mark.asyncio
async def test_build_runtime_sends_normalized_state_and_accepts_current_identity(
    monkeypatch,
):
    config = _config()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "runtime-test-key")
    session_id = uuid4()
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["url"] = str(request.url)
        sent["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "decision-model-preview",
                "request_id": "runtime-controlled-request",
                "answers": {"emotion": {"type": "choice", "choice": "curious"}},
            },
        )

    _install_transport(monkeypatch, handler)

    async def hydrate(*_args, **_kwargs):
        return _hydration()

    monkeypatch.setattr("apps.voice_realtime_harness.hydrate_voice_context", hydrate)
    coordinator, summary = _build_runtime(
        config,
        pool=_Pool(),
        session_id=session_id,
        stable_instructions="稳定语音指令",
    )
    windows = []

    async def visual_sink(identity, fact, label=None):
        windows.append((identity, fact, label))

    coordinator.set_visual_window_sink(visual_sink)
    identity = _identity(session_id)
    result = await coordinator.run(identity, "请解释这段代码")

    assert summary["emotion"] == {
        "provider": "aliyun-systemone",
        "model": "decision-model-preview",
        "endpoint": config["emotion"]["endpoint"],
        "api_key_env": "DASHSCOPE_API_KEY",
        "timeout_ms": 1000,
    }
    assert sent["url"] == config["emotion"]["endpoint"]
    assert sent["body"]["state"] == {
        "user_message": "请解释这段代码",
        "identity": [{"name": "Hexis"}],
        "worldview": [{"belief": "以事实为准"}],
        "goals": [{"goal": "帮助用户"}],
        "relationships": [],
    }
    assert result.released is True
    assert result.context_ready is True
    assert result.evaluation["status"] == "valid"
    assert result.evaluation["accepted"] is True
    assert result.evaluation["value"]["label"] == "curious"
    assert result.visual_window["status"] == "accepted"
    assert windows[-1] == (
        identity,
        {"status": "accepted", "owner": "service", "decision_seq": 1},
        "curious",
    )
    label, eligible = eligible_gate_emotion(result, identity)
    assert (label, eligible) == ("curious", True)
    assert render_emotion(label, eligible=eligible) == "## Emotion\ncurious"
    stale_identity = {**identity, "input_order": 1}
    assert eligible_gate_emotion(result, stale_identity) == (None, False)


@pytest.mark.asyncio
async def test_build_runtime_releases_provider_error_without_old_model_fallback(
    monkeypatch,
):
    config = _config()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "runtime-test-key")
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, text="forbidden")

    _install_transport(monkeypatch, handler)

    async def hydrate(*_args, **_kwargs):
        return _hydration()

    monkeypatch.setattr("apps.voice_realtime_harness.hydrate_voice_context", hydrate)
    session_id = uuid4()
    coordinator, _summary = _build_runtime(
        config,
        pool=_Pool(),
        session_id=session_id,
        stable_instructions="稳定语音指令",
    )
    result = await coordinator.run(_identity(session_id), "问题")

    assert result.released is True
    assert result.context_ready is True
    assert result.evaluation["status"] == "error"
    assert result.evaluation["error_type"] == "IntegrationHttpError"
    assert result.visual_window["status"] == "open"
    assert result.metrics["release_reason"] == "evaluation_error"
    assert calls == 1
