from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException

from apps import hexis_api, voice_m4_probe, voice_realtime_harness
from services import behavior_events, qwen_realtime_client
from services.voice_realtime_session import VoiceRealtimeSessionRegistry


@pytest.mark.parametrize("value", [True, False, 0, -1, float("inf"), float("-inf"), float("nan"), "30", None, 10**400])
def test_start_timeout_rejects_non_numeric_non_finite_or_non_positive_values(value):
    with pytest.raises((TypeError, ValueError), match="finite positive number of seconds"):
        hexis_api._voice_realtime_start_timeout({"timeout": value})


@pytest.mark.parametrize("value", [30, 0.25])
def test_start_timeout_preserves_the_selected_seconds(value):
    assert hexis_api._voice_realtime_start_timeout({"timeout": value}) == value


@pytest_asyncio.fixture
async def scenario(monkeypatch):
    state = SimpleNamespace(
        config={"endpoint": "wss://unit.invalid/realtime", "model": "unit-model", "voice": "Tina", "api_key_env": "UNIT_KEY", "timeout": 0.01},
        ready=True, block_connect=False, close_error=False, clients=[], sockets=[], queries=[],
        registry=VoiceRealtimeSessionRegistry(),
        request=hexis_api.VoiceRealtimeSessionRequest(session_id=uuid4()),
    )

    class Connection:
        async def fetchval(self, *args):
            return None

        async def execute(self, sql, *args):
            state.queries.append(sql)

    class Acquisition:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *args):
            return None

    class Pool:
        def acquire(self):
            return Acquisition()

    class WebSocket:
        def __init__(self):
            self.messages = asyncio.Queue()
            self.closed = False

        async def send(self, message):
            if state.ready:
                self.messages.put_nowait(json.dumps({"type": "session.updated", "session": {"id": "unit-cloud-session"}}))

        async def recv(self):
            return await self.messages.get()

        async def close(self):
            self.closed = True
            if state.close_error:
                raise RuntimeError("controlled socket close failure")

    async def connect(*args, **kwargs):
        if state.block_connect:
            await asyncio.Future()
        socket = WebSocket()
        state.sockets.append(socket)
        return socket

    actual_client = qwen_realtime_client.QwenRealtimeClient

    def create_client(**kwargs):
        client = actual_client(**kwargs)
        state.clients.append(client)
        return client

    async def record_event(*args, **kwargs):
        return {"seq": 1}

    monkeypatch.setattr(hexis_api, "_pool", Pool())
    monkeypatch.setattr(hexis_api, "_voice_realtime_registry", state.registry)
    monkeypatch.delenv("HEXIS_REALTIME_CONFIG", raising=False)
    monkeypatch.delenv("HEXIS_M5_ACCEPTANCE_RUN", raising=False)
    monkeypatch.setenv("HEXIS_BROWSER_API_URL", "http://127.0.0.1:43817")
    monkeypatch.setenv("HEXIS_M4_VOICE_CONFIG", "/unit/m4-runtime.json")
    monkeypatch.setattr(voice_m4_probe, "load_selected_config", lambda: state.config)
    monkeypatch.setattr(voice_m4_probe, "selected_env_value", lambda name: "unit-key")
    monkeypatch.setattr(voice_m4_probe, "load_m4_stable_instructions", lambda: "unit-instructions")
    monkeypatch.setattr(voice_realtime_harness, "_load_config", lambda path: {})
    monkeypatch.setattr(voice_realtime_harness, "_build_runtime", lambda *args, **kwargs: (SimpleNamespace(), {}))
    monkeypatch.setattr(voice_realtime_harness, "_default_connect_factory", connect)
    monkeypatch.setattr(qwen_realtime_client, "QwenRealtimeClient", create_client)
    monkeypatch.setattr(behavior_events, "record_behavior_event", record_event)
    try:
        yield state
    finally:
        for session in tuple(state.registry._sessions.values()):
            tasks = tuple(session._background_tasks)
            await session.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for client in state.clients:
            try:
                await client.close()
            except RuntimeError:
                pass


@pytest.mark.asyncio
async def test_api_start_succeeds_with_the_selected_deadline(scenario):
    scenario.config["timeout"] = 0.25
    result = await hexis_api.create_voice_realtime_session(scenario.request)
    session = scenario.registry.get(scenario.request.session_id)
    assert result["session_id"] == str(scenario.request.session_id)
    assert session.cloud_session_id == "unit-cloud-session"
    assert session._ready is True
    assert result["barge_in_capability"] == session.barge_in_capability.model_dump(mode="json")
    assert result["barge_in_capability"] == {
        "supported": True,
        "configuration_id": "voice-barge-in-playback-initial-v1",
        "maximum_receipt_prefix_items": 256,
    }
    with pytest.raises(AttributeError):
        session.barge_in_capability = None
    assert len(session._background_tasks) == 1
    assert scenario.sockets[0].closed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,close_error", [("connect", False), ("receipt", False), ("receipt", True)])
async def test_api_timeout_closes_the_session_removes_registry_and_leaves_no_receiver(scenario, stage, close_error):
    scenario.ready = False
    scenario.block_connect = stage == "connect"
    scenario.close_error = close_error
    with pytest.raises(HTTPException) as error:
        await asyncio.wait_for(hexis_api.create_voice_realtime_session(scenario.request), timeout=0.2)
    assert error.value.status_code == 504
    assert "0.01s (C5 timeout)" in error.value.detail
    assert scenario.registry.get(scenario.request.session_id) is None
    assert not scenario.registry._by_media_session_id
    assert not any("INSERT INTO voice_realtime_sessions" in sql for sql in scenario.queries)
    if stage == "receipt":
        assert scenario.sockets[0].closed is True
        assert scenario.clients[0]._receive_task.done()
        assert scenario.clients[0]._receive_task.cancelled()
    else:
        assert scenario.clients[0]._receive_task is None


@pytest.mark.asyncio
async def test_api_invalid_timeout_is_rejected_before_provider_construction(scenario):
    scenario.config["timeout"] = True
    with pytest.raises(HTTPException) as error:
        await hexis_api.create_voice_realtime_session(scenario.request)
    assert error.value.status_code == 503
    assert "C5 timeout" in error.value.detail
    assert not scenario.clients
    assert not scenario.registry._sessions


@pytest.mark.asyncio
async def test_api_rejects_a_missing_c7_config_before_provider_construction(scenario, monkeypatch):
    def missing_config():
        raise FileNotFoundError("controlled missing C7 config")

    monkeypatch.setattr(hexis_api, "load_playback_coordination_config", missing_config)
    with pytest.raises(HTTPException) as error:
        await hexis_api.create_voice_realtime_session(scenario.request)
    assert error.value.status_code == 503
    assert "C7 playback coordination configuration" in error.value.detail
    assert not scenario.clients
    assert not scenario.registry._sessions
