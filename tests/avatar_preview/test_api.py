"""Contract and lifecycle tests for the isolated Avatar preview HTTP API."""

import asyncio
import importlib
import importlib.util
import json
import os
import uuid
import httpx
import pytest

from services import avatar_emotion


def _api_module():
    spec = importlib.util.find_spec("apps.avatar_preview_api")
    assert spec is not None, "Phase 2 must add the standalone preview API module"
    return importlib.import_module("apps.avatar_preview_api")


def _config(**updates):
    return {
        "provider": "openai-chat-completions-endpoint",
        "model": "qwen-plus",
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "timeout_seconds": 1,
        "total_timeout_seconds": 2,
        "fixture": {
            "identity": [],
            "worldview": [],
            "goals": [],
            "relationships": [],
        },
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
        "message": "请解释一个概念",
        "context_source": "fixture",
        **updates,
    }


@pytest.mark.asyncio
async def test_config_requires_explicit_env_name_and_does_not_fallback_to_ambient_key(
    monkeypatch,
):
    api = _api_module()
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-unselected-key")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    app = api.create_app(_config(), environ=os.environ)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        status = (await client.get("/status")).json()
    assert status["credential_available"] is False
    assert "ambient-unselected-key" not in json.dumps(status)
    assert "api_key" not in status


@pytest.mark.asyncio
async def test_status_never_exposes_secret_dsn_or_endpoint(monkeypatch):
    api = _api_module()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "status-secret-do-not-return")
    monkeypatch.setenv("AVATAR_TEST_DSN", "postgresql://user:password@db.invalid/hexis")
    app = api.create_app(_config(db_dsn_env="AVATAR_TEST_DSN"), environ=os.environ)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = (await client.get("/status")).json()
    serialized = json.dumps(body)
    assert body["provider"] == _config()["provider"]
    assert body["model"] == "qwen-plus"
    assert body["wordset_version"] == "v1"
    assert body["prompt_version"] == "v1"
    assert body["business_budget"] == "none"
    assert "status-secret-do-not-return" not in serialized
    assert "postgresql://" not in serialized
    assert "dashscope.aliyuncs.com" not in serialized


@pytest.mark.asyncio
async def test_fixture_source_is_marked_and_evaluation_failure_can_be_queried(
    monkeypatch,
):
    api = _api_module()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    calls = 0

    async def fail_once(message, context, config):
        nonlocal calls
        calls += 1
        assert message == "请解释一个概念"
        assert set(context) == {"identity", "worldview", "goals", "relationships"}
        raise ValueError("private provider detail")

    app = api.create_app(_config(), environ=os.environ, evaluator=fail_once)
    body = _payload()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/evaluate", json=body)
        query = await client.get(api.evaluation_path(body["identity"]))
    result = response.json()
    assert response.status_code == 200
    assert result["status"] == "error"
    assert result["context_source"] == "fixture"
    assert result["emotion_source"] == "real"
    assert "private provider detail" not in json.dumps(result)
    assert query.json() == result
    assert calls == 1


@pytest.mark.asyncio
async def test_duplicate_concurrent_requests_share_one_evaluation(monkeypatch):
    api = _api_module()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def evaluate_once(message, context, config):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {
            "label": "curious",
            "wordset_version": "v1",
            "logical_call_count": 1,
            "transport_attempts": None,
        }

    app = api.create_app(_config(), environ=os.environ, evaluator=evaluate_once)
    body = _payload()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = asyncio.create_task(client.post("/evaluate", json=body))
        await started.wait()
        pending = await client.get(api.evaluation_path(body["identity"]))
        assert pending.json()["status"] == "pending"
        assert pending.json()["identity"] == body["identity"]
        assert pending.json()["emotion_source"] == "real"
        assert pending.json()["context_source"] == "fixture"
        second = asyncio.create_task(client.post("/evaluate", json=body))
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        responses = await asyncio.gather(first, second)
    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json()["label"] for response in responses] == [
        "curious",
        "curious",
    ]
    assert calls == 1


@pytest.mark.asyncio
async def test_context_value_error_reports_context_problem_not_model_label(monkeypatch):
    api = _api_module()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("AVATAR_TEST_DSN", "postgresql://selected/test")

    class Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return False

    class Connection:
        def transaction(self, *, readonly):
            assert readonly
            return Transaction()

        async def fetchval(self, _query):
            return "not valid json"

        async def close(self):
            return None

    async def connect(_dsn, *, timeout):
        assert timeout == 1
        return Connection()

    app = api.create_app(
        _config(db_dsn_env="AVATAR_TEST_DSN"),
        environ=os.environ,
        conn_factory=connect,
    )
    body = _payload(context_source="db")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/evaluate", json=body)
    result = response.json()
    assert result["stage"] == "context"
    assert "上下文" in result["message"]
    assert "标签协议" not in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["message", "input_order", "context_source"])
async def test_same_identity_with_changed_content_returns_conflict(monkeypatch, change):
    api = _api_module()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    calls = 0

    async def evaluate_once(*_args):
        nonlocal calls
        calls += 1
        return {"label": "neutral", "wordset_version": "v1"}

    app = api.create_app(
        _config(db_dsn_env="AVATAR_TEST_DSN"),
        environ=os.environ,
        evaluator=evaluate_once,
    )
    body = _payload()
    changed = json.loads(json.dumps(body))
    if change == "message":
        changed["message"] = "不同内容"
    elif change == "input_order":
        changed["identity"]["input_order"] += 1
    else:
        changed["context_source"] = "db"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post("/evaluate", json=body)
        second = await client.post("/evaluate", json=changed)
    assert first.status_code == 200
    assert second.status_code == 409
    assert calls == 1


@pytest.mark.asyncio
async def test_client_cancellation_does_not_cancel_shared_registry_operation():
    api = _api_module()
    registry = api.Evaluations()
    body = _payload()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"status": "valid", "label": "happy"}

    waiter = asyncio.create_task(registry.get_or_start(body, operation))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    duplicate = asyncio.create_task(registry.get_or_start(body, operation))
    await asyncio.sleep(0)
    assert calls == 1
    release.set()
    assert await duplicate == {"status": "valid", "label": "happy"}
    assert calls == 1


@pytest.mark.asyncio
async def test_shutdown_query_keeps_identity_and_source_metadata():
    api = _api_module()
    registry = api.Evaluations()
    body = _payload(context_source="db")
    started = asyncio.Event()

    async def blocked_operation():
        started.set()
        await asyncio.Event().wait()

    waiter = asyncio.create_task(registry.get_or_start(body, blocked_operation))
    await started.wait()
    await registry.close()
    identity = body["identity"]
    result = await registry.query(
        uuid.UUID(identity["session_id"]),
        uuid.UUID(identity["input_turn_id"]),
        identity["stimulus_revision"],
    )
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert result["status"] == "error"
    assert result["identity"] == identity
    assert result["context_source"] == "db"
    assert result["emotion_source"] == "real"


@pytest.mark.asyncio
async def test_database_context_is_read_only_and_connection_is_closed(monkeypatch):
    api = _api_module()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("AVATAR_TEST_DSN", "postgresql://selected/test")
    context = {"identity": [], "worldview": [], "goals": [], "relationships": []}
    seen = {"readonly": False, "closed": False, "dsn": None}

    class Transaction:
        async def __aenter__(self):
            seen["readonly"] = True

        async def __aexit__(self, *_args):
            return False

    class Connection:
        def transaction(self, *, readonly):
            assert readonly is True
            return Transaction()

        async def fetchval(self, query):
            assert query == "SELECT get_appraisal_db_context()"
            return json.dumps({**context, "dopamine_state": {"secret": True}})

        async def close(self):
            seen["closed"] = True

    async def connect(dsn, *, timeout):
        seen["dsn"] = dsn
        assert timeout == 1
        return Connection()

    async def evaluate_once(message, received_context, config):
        assert received_context == context
        return {"label": "curious", "wordset_version": "v1"}

    app = api.create_app(
        _config(db_dsn_env="AVATAR_TEST_DSN"),
        environ=os.environ,
        conn_factory=connect,
        evaluator=evaluate_once,
    )
    body = _payload(context_source="db")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/evaluate", json=body)
    assert response.status_code == 200
    assert response.json()["context_source"] == "db"
    assert seen == {
        "readonly": True,
        "closed": True,
        "dsn": "postgresql://selected/test",
    }


@pytest.mark.asyncio
async def test_total_deadline_includes_context_read_and_connection_cleanup(monkeypatch):
    api = _api_module()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("AVATAR_TEST_DSN", "postgresql://selected/test")
    closed = asyncio.Event()

    class Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return False

    class Connection:
        def transaction(self, *, readonly):
            assert readonly
            return Transaction()

        async def fetchval(self, _query):
            await asyncio.Event().wait()

        async def close(self):
            closed.set()

    async def connect(_dsn, *, timeout):
        assert timeout == 1
        return Connection()

    app = api.create_app(
        _config(db_dsn_env="AVATAR_TEST_DSN", total_timeout_seconds=0.03),
        environ=os.environ,
        conn_factory=connect,
    )
    body = _payload(context_source="db")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/evaluate", json=body)
    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["error_code"] == "TimeoutError"
    assert closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_stage", ["connect", "close"])
async def test_total_deadline_also_bounds_database_connect_and_close(
    monkeypatch, blocked_stage
):
    api = _api_module()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("AVATAR_TEST_DSN", "postgresql://selected/test")
    close_started = asyncio.Event()
    terminated = asyncio.Event()
    release_close = asyncio.Event()
    context = {"identity": [], "worldview": [], "goals": [], "relationships": []}

    class Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return False

    class Connection:
        def transaction(self, *, readonly):
            assert readonly
            return Transaction()

        async def fetchval(self, _query):
            return json.dumps(context)

        async def close(self):
            close_started.set()
            if blocked_stage == "close":
                try:
                    await release_close.wait()
                except asyncio.CancelledError:
                    await release_close.wait()

        def terminate(self):
            terminated.set()
            release_close.set()

    async def connect(_dsn, *, timeout):
        assert timeout == 1
        if blocked_stage == "connect":
            await asyncio.Event().wait()
        return Connection()

    app = api.create_app(
        _config(db_dsn_env="AVATAR_TEST_DSN", total_timeout_seconds=0.03),
        environ=os.environ,
        conn_factory=connect,
    )
    body = _payload(context_source="db")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await asyncio.wait_for(
            client.post("/evaluate", json=body), timeout=0.5
        )
    assert response.status_code == 200
    assert response.json()["error_code"] == "TimeoutError"
    assert close_started.is_set() is (blocked_stage == "close")
    assert terminated.is_set() is (blocked_stage == "close")


@pytest.mark.asyncio
async def test_cancelled_database_read_preserves_cancellation_when_close_hangs():
    api = _api_module()
    read_started = asyncio.Event()
    close_started = asyncio.Event()
    terminated = asyncio.Event()
    release_close = asyncio.Event()

    class Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return False

    class Connection:
        def transaction(self, *, readonly):
            assert readonly
            return Transaction()

        async def fetchval(self, _query):
            read_started.set()
            await asyncio.Event().wait()

        async def close(self):
            close_started.set()
            try:
                await release_close.wait()
            except asyncio.CancelledError:
                await release_close.wait()

        def terminate(self):
            terminated.set()
            release_close.set()

    async def connect(_dsn, *, timeout):
        assert timeout == 1
        return Connection()

    body = _payload(context_source="db")
    config = api.PreviewConfig.model_validate(
        _config(db_dsn_env="AVATAR_TEST_DSN", total_timeout_seconds=5)
    )
    task = asyncio.create_task(
        api.run_evaluation(
            body,
            config,
            api_key="test-key",
            resolved_db_dsn="postgresql://selected/test",
            conn_factory=connect,
            evaluator=lambda *_args: None,
        )
    )
    await read_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.5)
    assert close_started.is_set()
    assert terminated.is_set()


@pytest.mark.asyncio
async def test_unselected_database_source_fails_without_fallback_to_fixture(
    monkeypatch,
):
    api = _api_module()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    calls = 0

    async def evaluator(*_args):
        nonlocal calls
        calls += 1
        return {"label": "neutral"}

    app = api.create_app(_config(), environ=os.environ, evaluator=evaluator)
    body = _payload(context_source="db")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/evaluate", json=body)
    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["context_source"] == "db"
    assert calls == 0


@pytest.mark.asyncio
async def test_unknown_query_never_starts_a_new_evaluation(monkeypatch):
    api = _api_module()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    calls = 0

    async def evaluator(*_args):
        nonlocal calls
        calls += 1
        return {"label": "neutral"}

    app = api.create_app(_config(), environ=os.environ, evaluator=evaluator)
    identity = {
        "session_id": str(uuid.uuid4()),
        "input_turn_id": str(uuid.uuid4()),
        "input_order": 0,
        "stimulus_revision": 0,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(api.evaluation_path(identity))
    assert response.status_code == 404
    assert response.json()["status"] == "unknown"
    assert calls == 0


@pytest.mark.asyncio
async def test_no_cumulative_evaluation_limit_is_added(monkeypatch):
    api = _api_module()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    calls = 0

    async def evaluator(*_args):
        nonlocal calls
        calls += 1
        return {"label": "neutral"}

    app = api.create_app(_config(), environ=os.environ, evaluator=evaluator)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = [await client.post("/evaluate", json=_payload()) for _ in range(5)]
    assert all(response.status_code == 200 for response in responses)
    assert calls == 5


def test_missing_configuration_can_leave_api_disabled_for_offline_preview():
    api = _api_module()
    app = api.create_app(None, environ={})
    assert app.state.preview_config is None


def test_explicit_profile_validation_rejects_nonfinite_timeouts_and_extra_fields(
    tmp_path,
):
    api = _api_module()
    valid = _config()
    valid["timeout_seconds"] = float("inf")
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(valid), encoding="utf-8")
    with pytest.raises(ValueError):
        api.load_config(path)

    valid = _config(unapproved_fallback="OPENAI_API_KEY")
    path.write_text(json.dumps(valid), encoding="utf-8")
    with pytest.raises(ValueError):
        api.load_config(path)
