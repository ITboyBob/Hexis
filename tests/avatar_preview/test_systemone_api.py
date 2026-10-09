"""System One 适配器经真实 evaluate 进入 Avatar Preview API。"""

from __future__ import annotations

import json
import os
import uuid

import httpx
import pytest

from apps import avatar_preview_api
from core import integration_reliability
from core.providers import aliyun_decision

ENDPOINT = (
    "https://llm-n5dppxzleuxpia6o.cn-beijing.maas.aliyuncs.com/"
    "compatible-mode/v1/systemone"
)
CONTEXT = {
    "identity": [],
    "worldview": [],
    "goals": [],
    "relationships": [],
}


def _config(**updates):
    return {
        "provider": "aliyun-systemone",
        "model": "decision-model-preview",
        "endpoint": ENDPOINT,
        "api_key_env": "DASHSCOPE_API_KEY",
        "timeout_seconds": 1,
        "total_timeout_seconds": 2,
        "fixture": CONTEXT,
        **updates,
    }


def _payload(**updates):
    return {
        "identity": {
            "session_id": str(uuid.uuid4()),
            "input_turn_id": str(uuid.uuid4()),
            "input_order": 0,
            "stimulus_revision": 0,
        },
        "message": "请解释这段代码",
        "context_source": "fixture",
        **updates,
    }


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


@pytest.mark.asyncio
async def test_preview_api_consumes_real_evaluate_and_caches_same_identity(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "preview-test-key")
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "decision-model-preview",
                "request_id": "preview-controlled-request",
                "answers": {"emotion": {"type": "choice", "choice": "curious"}},
            },
        )

    _install_transport(monkeypatch, handler)
    app = avatar_preview_api.create_app(_config(), environ=os.environ)
    body = _payload()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        status = (await client.get("/status")).json()
        first = (await client.post("/evaluate", json=body)).json()
        second = (await client.post("/evaluate", json=body)).json()
        query = (
            await client.get(avatar_preview_api.evaluation_path(body["identity"]))
        ).json()

    assert status["configured"] is True
    assert status["provider"] == "aliyun-systemone"
    assert status["model"] == "decision-model-preview"
    assert status["credential_available"] is True
    assert first == second == query
    assert first["status"] == "valid"
    assert first["label"] == "curious"
    assert first["visual_window"] == {
        "status": "accepted",
        "owner": "service",
        "decision_seq": 1,
    }
    assert len(requests) == 1
    assert requests[0]["state"] == {
        "user_message": body["message"],
        **CONTEXT,
    }


@pytest.mark.asyncio
async def test_preview_db_context_normalizes_missing_relationships_before_http(
    monkeypatch,
):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "preview-test-key")
    monkeypatch.setenv("EDM_RUNTIME_DATABASE_URL", "postgresql://selected/test")
    sent_state = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_state
        sent_state = json.loads(request.content)["state"]
        return httpx.Response(
            200,
            json={
                "model": "decision-model-preview",
                "request_id": "preview-db-request",
                "answers": {"emotion": {"type": "choice", "choice": "neutral"}},
            },
        )

    _install_transport(monkeypatch, handler)

    class Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return False

    class Connection:
        def transaction(self, *, readonly):
            assert readonly is True
            return Transaction()

        async def fetchval(self, query):
            assert query == "SELECT get_appraisal_db_context()"
            return json.dumps(
                {key: value for key, value in CONTEXT.items() if key != "relationships"}
            )

        async def close(self):
            return None

    async def connect(dsn, *, timeout):
        assert dsn == "postgresql://selected/test"
        assert timeout == 1
        return Connection()

    app = avatar_preview_api.create_app(
        _config(db_dsn_env="EDM_RUNTIME_DATABASE_URL"),
        environ=os.environ,
        conn_factory=connect,
    )
    body = _payload(context_source="db")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = (await client.post("/evaluate", json=body)).json()

    assert result["status"] == "valid"
    assert result["label"] == "neutral"
    assert sent_state == {"user_message": body["message"], **CONTEXT}


@pytest.mark.asyncio
async def test_preview_reports_provider_error_without_fallback(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "preview-test-key")
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, text="private provider detail")

    _install_transport(monkeypatch, handler)
    app = avatar_preview_api.create_app(_config(), environ=os.environ)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = (await client.post("/evaluate", json=_payload())).json()

    assert result["status"] == "error"
    assert result["stage"] == "model"
    assert result["error_code"] == "IntegrationHttpError"
    assert result["logical_call_count"] == 1
    assert calls == 1
    assert "private provider detail" not in json.dumps(result)


@pytest.mark.asyncio
async def test_preview_missing_selected_key_never_calls_http(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    _install_transport(monkeypatch, handler)
    app = avatar_preview_api.create_app(_config(), environ=os.environ)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = (await client.post("/evaluate", json=_payload())).json()

    assert result["status"] == "error"
    assert result["stage"] == "configuration"
    assert result["error_code"] == "ConfigurationUnavailable"
    assert calls == 0
