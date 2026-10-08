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


# 此模块只使用替身连接、替身 WebSocket；禁止全局 fixture 开真实数据库。
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
        ready=True, block_connect=False, close_error=False, fail_ready_event=False, block_ready_event=False,
        ready_event_started=asyncio.Event(),
        clients=[], sockets=[], queries=[], row_statuses={},
        registry=VoiceRealtimeSessionRegistry(),
        request=hexis_api.VoiceRealtimeSessionRequest(session_id=uuid4()),
    )

    class Connection:
        async def fetchval(self, *args):
            return None

        async def execute(self, sql, *args):
            state.queries.append(sql)
            if "INSERT INTO voice_realtime_sessions" in sql:
                state.row_statuses[(args[0], args[1])] = "active"
            if "SET status='failed'" in sql:
                assert "qwen_session_id=$2" in sql and "status='active'" in sql
                identity = (args[0], args[1])
                if state.row_statuses.get(identity) == "active":
                    state.row_statuses[identity] = "failed"

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
            self.sent = []

        async def send(self, message):
            self.sent.append(json.loads(message))
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
        if state.block_ready_event and args[1].event_type == "voice_session_ready":
            state.ready_event_started.set()
            await asyncio.Future()
        if state.fail_ready_event and args[1].event_type == "voice_session_ready":
            raise RuntimeError("controlled ready-event persistence failure")
        return {"seq": 1}

    monkeypatch.setattr(hexis_api, "_pool", Pool())
    monkeypatch.setattr(hexis_api, "_voice_realtime_registry", state.registry)
    monkeypatch.delenv("HEXIS_REALTIME_CONFIG", raising=False)
    monkeypatch.delenv("HEXIS_M5_ACCEPTANCE_RUN", raising=False)
    monkeypatch.delenv("HEXIS_VOICE_BARGE_IN_ENV", raising=False)
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
async def test_api_missing_input_gate_config_creates_no_provider_or_active_session(scenario, monkeypatch):
    monkeypatch.delenv("HEXIS_M4_VOICE_CONFIG", raising=False)
    with pytest.raises(HTTPException) as error:
        await hexis_api.create_voice_realtime_session(scenario.request)
    assert error.value.status_code == 503
    assert "HEXIS_M4_VOICE_CONFIG is not selected" in error.value.detail
    assert not scenario.clients
    assert not scenario.sockets
    assert not scenario.registry._sessions
    assert not scenario.row_statuses
    assert not any("INSERT INTO voice_realtime_sessions" in sql for sql in scenario.queries)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("browser_url", [None, "", "https://unselected.example", "ws://127.0.0.1:43817"])
async def test_api_rejects_invalid_browser_url_before_provider_or_session_persistence(scenario, monkeypatch, browser_url):
    if browser_url is None:
        monkeypatch.delenv("HEXIS_BROWSER_API_URL", raising=False)
    else:
        monkeypatch.setenv("HEXIS_BROWSER_API_URL", browser_url)
    with pytest.raises(HTTPException) as error:
        await hexis_api.create_voice_realtime_session(scenario.request)
    assert error.value.status_code == 503
    assert "HEXIS_BROWSER_API_URL" in error.value.detail
    assert not scenario.clients and not scenario.sockets
    assert not scenario.queries and not scenario.row_statuses and not scenario.registry._sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("close_error", [False, True])
async def test_post_connect_failure_closes_client_registry_and_only_exact_active_row(scenario, close_error):
    unrelated = (uuid4(), "unrelated-cloud-session")
    scenario.row_statuses[unrelated] = "active"
    scenario.fail_ready_event = True
    scenario.close_error = close_error
    with pytest.raises(RuntimeError, match="controlled ready-event persistence failure"):
        await hexis_api.create_voice_realtime_session(scenario.request)
    assert scenario.row_statuses[(scenario.request.session_id, "unit-cloud-session")] == "failed"
    assert scenario.row_statuses[unrelated] == "active"
    assert scenario.sockets[0].closed
    assert scenario.clients[0]._receive_task.done()
    assert not scenario.registry._sessions and not scenario.registry._by_media_session_id


@pytest.mark.asyncio
async def test_post_connect_cancellation_marks_exact_row_failed_and_releases_cloud_client(scenario):
    scenario.block_ready_event = True
    creating = asyncio.create_task(hexis_api.create_voice_realtime_session(scenario.request))
    await asyncio.wait_for(scenario.ready_event_started.wait(), timeout=0.2)
    creating.cancel()
    with pytest.raises(asyncio.CancelledError):
        await creating
    assert scenario.row_statuses[(scenario.request.session_id, "unit-cloud-session")] == "failed"
    assert scenario.sockets[0].closed and scenario.clients[0]._receive_task.done()
    assert not scenario.registry._sessions and not scenario.registry._by_media_session_id


@pytest.mark.asyncio
async def test_normal_api_keeps_m5_worker_readiness_requirement(scenario, monkeypatch):
    monkeypatch.setattr(hexis_api, "_m5_task_worker", None)
    scenario.request.tool_surface = "m5"
    with pytest.raises(HTTPException) as error:
        await hexis_api.create_voice_realtime_session(scenario.request)
    assert error.value.status_code == 503
    assert "task worker is not ready" in error.value.detail
    assert not scenario.clients and not scenario.sockets


@pytest.mark.asyncio
@pytest.mark.parametrize("registered,key_selected", [(True, True), (False, True), (True, False)])
async def test_m6_registered_session_reaches_only_selected_c5_ready_without_m5_workers(scenario, monkeypatch, registered, key_selected):
    import hashlib
    from pathlib import Path
    from apps.voice_barge_in_environment import M6_SESSION_EXECUTION_POLICY, load_profile

    policy = dict(M6_SESSION_EXECUTION_POLICY)
    profile = load_profile()
    root = Path(hexis_api.__file__).resolve().parents[1]
    c5 = json.loads((root / profile["realtime_config"]).read_text())
    if key_selected:
        monkeypatch.setenv("HEXIS_VOICE_BARGE_IN_PROVIDER_KEY_ENV", c5["api_key_env"])
    else:
        monkeypatch.delenv("HEXIS_VOICE_BARGE_IN_PROVIDER_KEY_ENV", raising=False)
    monkeypatch.setattr(hexis_api, "_selected_m6_session_policy", lambda: policy)
    monkeypatch.setattr(hexis_api, "_m5_task_worker", None)
    monkeypatch.setattr(hexis_api, "_m5_lightweight_executor", None)
    monkeypatch.setattr(hexis_api, "_m6_registered_chat_sessions", {scenario.request.session_id} if registered else set())
    monkeypatch.setattr(hexis_api, "_voice_barge_in_start_record", {
        "acceptance_session_id": str(scenario.request.session_id),
        "credential_env_names": [c5["api_key_env"]],
        "config_fingerprints": {
            key: hashlib.sha256((root / profile[path]).read_bytes()).hexdigest()
            for key, path in (("C4", "runtime_config"), ("C5", "realtime_config"))
        },
    })
    scenario.request.tool_surface = "m5"
    runtime_policies = []
    def build_runtime(*args, **kwargs):
        runtime_policies.append(kwargs["session_execution_policy"])
        return SimpleNamespace(), {}
    monkeypatch.setattr(voice_realtime_harness, "_build_runtime", build_runtime)
    if not registered or not key_selected:
        with pytest.raises(HTTPException) as error:
            await hexis_api.create_voice_realtime_session(scenario.request)
        assert error.value.status_code == (403 if not registered else 503)
        assert not scenario.clients and not runtime_policies
        return
    result = await hexis_api.create_voice_realtime_session(scenario.request)
    session = scenario.registry.get(scenario.request.session_id)
    assert result["session_id"] == str(scenario.request.session_id)
    assert session._ready and session.session_execution_policy == policy
    assert runtime_policies == [policy]
    assert scenario.clients[0].model == c5["model"]
    update = next(message for message in scenario.sockets[0].sent if message["type"] == "session.update")
    assert {tool["name"] for tool in update["session"]["tools"]} == {"submit", "pend_message", "remember", "add_evidence", "voice_recall"}
    assert hexis_api._m5_task_worker is None and hexis_api._m5_lightweight_executor is None
    assert not any("m5_" in sql or "embed" in sql or "http" in sql for sql in scenario.queries)
