"""RTC 信令代理不泄漏凭据，也不建立第二个云 WebSocket 会话。"""

from types import SimpleNamespace
from uuid import uuid4
import asyncio

import pytest
from fastapi import HTTPException

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.voice_rtc_api import RtcRuntime
from services.voice_rtc import RtcConfig


class Pool:
    consent = "consent"

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def fetchval(self, query, *args):
        if "get_config_bool" in query:
            return True
        if "get_agent_consent_status" in query:
            return self.consent
        if "SELECT status" in query:
            return "active"
        if "INSERT INTO voice_realtime_sessions" in query:
            return args[0]
        if "jsonb_build_object" in query:
            return None
        raise AssertionError(query)

    async def execute(self, *args):
        return "UPDATE 1"


def make_app(monkeypatch):
    config = RtcConfig.from_mapping(
        {
            "endpoint": "wss://workspace.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime",
            "webrtc_endpoint": "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1/webrtc/realtime",
            "model": "chosen-model",
            "voice": "chosen-voice",
            "api_key_env": "TEST_RTC_KEY",
            "timeout": 30,
        }
    )
    monkeypatch.setenv("TEST_RTC_KEY", "test-credential")
    prepared = []
    exchanges = []

    async def prepare(pool, *, session_id):
        prepared.append(session_id)
        return SimpleNamespace(instructions="初始化上下文", context_version="a" * 64)

    async def exchange(config, key, offer):
        exchanges.append((config.model, key, offer))
        return "v=0\r\nanswer\r\n"

    monkeypatch.setattr("services.voice_rtc_api.prepare_rtc_context", prepare)
    runtime = RtcRuntime(
        pool_provider=lambda: Pool(),
        config_loader=lambda: config,
        control_url=lambda media: f"ws://127.0.0.1:43817/api/voice/realtime/sessions/{media}/control",
        exchange_sdp=exchange,
    )
    app = FastAPI()
    app.state.rtc_runtime = runtime
    app.include_router(runtime.router)
    return app, prepared, exchanges


def test_bootstrap_and_sdp_proxy_prepare_once_without_returning_credentials(
    monkeypatch,
):
    app, prepared, exchanges = make_app(monkeypatch)
    with TestClient(app) as client:
        session_id = str(uuid4())
        result = client.post(
            "/api/voice/realtime/sessions",
            json={"session_id": session_id, "tool_surface": "m4"},
        )
        assert result.status_code == 200
        descriptor = result.json()
        assert "test-credential" not in result.text
        assert descriptor["session_config"]["input_audio_transcription"] is None
        media_id = descriptor["media_session_id"]
        answer = client.post(
            f"/api/voice/realtime/sessions/{media_id}/offer",
            content="v=0\r\noffer\r\n",
            headers={"Content-Type": "application/sdp"},
        )
        assert answer.status_code == 200
        assert answer.headers["content-type"].startswith("application/sdp")
        assert exchanges == [("chosen-model", "test-credential", "v=0\r\noffer\r\n")]
        assert len(prepared) == 1
        assert (
            client.post(
                f"/api/voice/realtime/sessions/{media_id}/offer",
                content="v=0",
                headers={"Content-Type": "application/sdp"},
            ).status_code
            == 409
        )
        client.delete(f"/api/voice/realtime/sessions/{media_id}")


def test_offer_requires_sdp_and_rejects_unknown_media(monkeypatch):
    app, _, _ = make_app(monkeypatch)
    with TestClient(app) as client:
        assert (
            client.post(
                f"/api/voice/realtime/sessions/{uuid4()}/offer", json={"sdp": "v=0"}
            ).status_code
            == 404
        )


def test_only_one_active_rtc_session_per_chat(monkeypatch):
    app, _, _ = make_app(monkeypatch)
    with TestClient(app) as client:
        body = {"session_id": str(uuid4())}
        first = client.post("/api/voice/realtime/sessions", json=body)
        assert first.status_code == 200
        assert client.post("/api/voice/realtime/sessions", json=body).status_code == 409
        client.delete(
            f"/api/voice/realtime/sessions/{first.json()['media_session_id']}"
        )


def test_control_failure_has_sequence_and_preserves_real_error(monkeypatch):
    app, _, _ = make_app(monkeypatch)
    with TestClient(app) as client:
        descriptor = client.post(
            "/api/voice/realtime/sessions", json={"session_id": str(uuid4())}
        ).json()
        envelope = {
            "schema_version": 2,
            "session_id": descriptor["session_id"],
            "media_session_id": descriptor["media_session_id"],
        }
        with client.websocket_connect(
            f"/api/voice/realtime/sessions/{descriptor['media_session_id']}/control",
            headers={"origin": "http://localhost:3477"},
        ) as socket:
            assert socket.receive_json()["producer_seq"] == 0
            socket.send_json(
                {
                    **envelope,
                    "producer_seq": 0,
                    "type": "provider_event",
                    "event": {"type": "session.created", "session": {"id": "cloud"}},
                }
            )
            socket.send_json(
                {
                    **envelope,
                    "producer_seq": 1,
                    "type": "provider_event",
                    "event": {
                        "type": "session.updated",
                        "session": descriptor["session_config"],
                    },
                }
            )
            assert socket.receive_json()["producer_seq"] == 1
            socket.send_json(
                {
                    **envelope,
                    "producer_seq": 2,
                    "type": "provider_event",
                    "event": {
                        "type": "response.created",
                        "response": {"id": "unbound"},
                    },
                }
            )
            error = socket.receive_json()
            assert error["type"] == "error" and error["producer_seq"] == 2
            assert "committed input identity" in error["message"]


def test_initialization_without_agent_consent_never_prepares_context_or_signals(
    monkeypatch,
):
    app, prepared, exchanges = make_app(monkeypatch)
    monkeypatch.setattr(Pool, "consent", "decline")
    with TestClient(app) as client:
        result = client.post(
            "/api/voice/realtime/sessions", json={"session_id": str(uuid4())}
        )
        assert result.status_code == 403
        assert prepared == [] and exchanges == []


@pytest.mark.parametrize(
    "content,content_type,status",
    [
        (b"v=0", "application/json", 415),
        (b"invalid", "application/sdp", 400),
        (b"\xff", "application/sdp", 400),
        (b"v=0" + b"x" * 131072, "application/sdp", 413),
    ],
)
def test_offer_validation_never_calls_provider(
    monkeypatch, content, content_type, status
):
    app, _, exchanges = make_app(monkeypatch)
    with TestClient(app) as client:
        descriptor = client.post(
            "/api/voice/realtime/sessions", json={"session_id": str(uuid4())}
        ).json()
        result = client.post(
            descriptor["offer_url"],
            content=content,
            headers={"Content-Type": content_type},
        )
        assert result.status_code == status
        assert exchanges == []
        client.delete(descriptor["close_url"])


@pytest.mark.parametrize(
    "failure", [asyncio.TimeoutError(), HTTPException(502, "受控信令失败")]
)
def test_signaling_failure_releases_session_and_close_is_idempotent(
    monkeypatch, failure
):
    app, _, _ = make_app(monkeypatch)

    async def exchange(*args):
        raise failure

    app.state.rtc_runtime.exchange_sdp = exchange
    with TestClient(app) as client:
        descriptor = client.post(
            "/api/voice/realtime/sessions", json={"session_id": str(uuid4())}
        ).json()
        result = client.post(
            descriptor["offer_url"],
            content="v=0\r\n",
            headers={"Content-Type": "application/sdp"},
        )
        assert result.status_code == 502
        assert not app.state.rtc_runtime.entries
        assert client.delete(descriptor["close_url"]).status_code == 200
        assert client.delete(descriptor["close_url"]).status_code == 200


def test_context_failure_never_signals_or_keeps_media_session(monkeypatch):
    app, _, exchanges = make_app(monkeypatch)

    async def prepare(*args, **kwargs):
        raise ValueError("受控初始化失败")

    monkeypatch.setattr("services.voice_rtc_api.prepare_rtc_context", prepare)
    with TestClient(app) as client:
        result = client.post(
            "/api/voice/realtime/sessions", json={"session_id": str(uuid4())}
        )
        assert result.status_code == 503
        assert not app.state.rtc_runtime.entries
        assert not app.state.rtc_runtime._creating
        assert exchanges == []
