"""Contract and lifecycle tests for the isolated Avatar preview HTTP API."""

import asyncio
import importlib
import importlib.util
import json
import os
import time
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
async def test_success_returns_service_owned_visual_acceptance(monkeypatch):
    api = _api_module()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")

    async def evaluate_once(*_args):
        return {
            "label": "happy",
            "wordset_version": "v1",
            "logical_call_count": 1,
            "transport_attempts": None,
        }

    app = api.create_app(
        _config(timeout_seconds=0.01), environ=os.environ, evaluator=evaluate_once
    )
    body = _payload()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = (await client.post("/evaluate", json=body)).json()
        await asyncio.sleep(0.02)
        query = (await client.get(api.evaluation_path(body["identity"]))).json()
    assert result["visual_window"] == {
        "status": "accepted",
        "owner": "service",
        "decision_seq": 1,
    }
    assert query == result


@pytest.mark.asyncio
async def test_model_timeout_closes_visual_window_but_context_timeout_does_not(
    monkeypatch,
):
    api = _api_module()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")

    async def hang(*_args):
        await asyncio.Event().wait()

    app = api.create_app(
        _config(timeout_seconds=0.01), environ=os.environ, evaluator=hang
    )
    body = _payload()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        model = (await client.post("/evaluate", json=body)).json()
    assert model["error_code"] == "ModelEvaluationTimeout"
    assert model["visual_window"] == {
        "status": "closed",
        "owner": "service",
        "decision_seq": 1,
        "reason": "model_timeout",
    }
    assert model["transport_status"] == "unknown"

    monkeypatch.setenv("AVATAR_TEST_DSN", "postgresql://selected/test")

    async def context_timeout(_dsn, *, timeout):
        assert timeout == 1
        await asyncio.sleep(0.01)
        raise asyncio.TimeoutError

    context_app = api.create_app(
        _config(db_dsn_env="AVATAR_TEST_DSN", timeout_seconds=1),
        environ=os.environ,
        conn_factory=context_timeout,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=context_app), base_url="http://test"
    ) as client:
        context = (
            await client.post("/evaluate", json=_payload(context_source="db"))
        ).json()
    assert context["stage"] == "context_connect"
    assert "visual_window" not in context


@pytest.mark.asyncio
async def test_late_cancel_ignored_value_is_queryable_diagnostic_not_accepted(
    monkeypatch,
):
    api = _api_module()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")

    async def late_value(*_args):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.03)
            return {"label": "happy", "raw_business_value": "happy"}

    app = api.create_app(
        _config(timeout_seconds=0.01), environ=os.environ, evaluator=late_value
    )
    body = _payload()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        post = (await client.post("/evaluate", json=body)).json()
        await asyncio.sleep(0.04)
        query = (await client.get(api.evaluation_path(body["identity"]))).json()
    assert post["status"] == "error"
    assert query["status"] == "error"
    assert query["visual_window"]["status"] == "closed"
    assert query["late_diagnostics"] == [
        {
            "identity": body["identity"],
            "status": "valid",
            "label": "happy",
            "raw_business_value": "happy",
            "accepted": False,
        }
    ]


@pytest.mark.asyncio
async def test_invalid_model_business_value_is_shown_as_rejected_error(monkeypatch):
    api = _api_module()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")

    async def completion(*, temperature, **_kwargs):
        assert temperature is not None
        return {"content": "happy\n"}

    monkeypatch.setattr(api.avatar_emotion.llm, "chat_completion", completion)
    app = api.create_app(_config(), environ=os.environ)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = (await client.post("/evaluate", json=_payload())).json()
    assert result["status"] == "error"
    assert result["error_code"] == "InvalidEmotionLabel"
    assert result["raw_business_value"] == "happy\n"
    assert result["rejection_reason"] == "not_exact_v1_emotion_label"
    assert "标签协议" in result["message"]
    assert "visual_window" not in result


@pytest.mark.asyncio
async def test_late_invalid_model_business_value_is_diagnostic_only(monkeypatch):
    api = _api_module()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")

    async def completion(*, temperature, **_kwargs):
        assert temperature is not None
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.03)
            return {"content": "\nhappy"}

    monkeypatch.setattr(api.avatar_emotion.llm, "chat_completion", completion)
    app = api.create_app(_config(timeout_seconds=0.01), environ=os.environ)
    body = _payload()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = (await client.post("/evaluate", json=body)).json()
        await asyncio.sleep(0.04)
        query = (await client.get(api.evaluation_path(body["identity"]))).json()
    assert result["visual_window"]["status"] == "closed"
    assert query["visual_window"] == result["visual_window"]
    assert query["late_diagnostics"] == [
        {
            "identity": body["identity"],
            "status": "error",
            "error_type": "InvalidEmotionLabel",
            "raw_business_value": "\nhappy",
            "accepted": False,
            "rejection_reason": "not_exact_v1_emotion_label",
        }
    ]


@pytest.mark.asyncio
async def test_synchronous_event_loop_block_cannot_cross_model_deadline():
    api = _api_module()

    async def blocking(*_args):
        time.sleep(0.03)
        return {"label": "happy", "wordset_version": "v1"}

    result = await api.run_evaluation(
        _payload(),
        api.PreviewConfig.model_validate(_config(timeout_seconds=0.01)),
        api_key="test-key",
        resolved_db_dsn=None,
        conn_factory=api._connect,
        evaluator=blocking,
    )
    assert result["status"] == "error"
    assert result["visual_window"]["reason"] == "model_timeout"


@pytest.mark.asyncio
async def test_late_connection_cleanup_logs_failure_type_without_secret(caplog):
    api = _api_module()
    close_finished = asyncio.Event()

    class BrokenConnection:
        def terminate(self):
            raise RuntimeError("db-password-must-not-be-logged")

        async def close(self):
            try:
                raise ValueError("dsn-must-not-be-logged")
            finally:
                close_finished.set()

    api._late_connection_cleanup(BrokenConnection())
    await close_finished.wait()
    await asyncio.sleep(0)
    assert (
        "Avatar preview connection termination failed error_type=RuntimeError"
        in caplog.text
    )
    assert (
        "Avatar preview background cleanup failed error_type=ValueError" in caplog.text
    )
    assert "db-password-must-not-be-logged" not in caplog.text
    assert "dsn-must-not-be-logged" not in caplog.text


@pytest.mark.asyncio
async def test_cancelled_database_read_is_not_replaced_by_immediate_close_failure(
    caplog,
):
    api = _api_module()
    read_started = asyncio.Event()

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
            raise ValueError("private cleanup detail")

        def terminate(self):
            raise RuntimeError("private termination detail")

    async def connect(_dsn, *, timeout):
        assert timeout == 1
        return Connection()

    task = asyncio.create_task(
        api.run_evaluation(
            _payload(context_source="db"),
            api.PreviewConfig.model_validate(_config(db_dsn_env="AVATAR_TEST_DSN")),
            api_key="test-key",
            resolved_db_dsn="postgresql://selected/test",
            conn_factory=connect,
            evaluator=api.avatar_emotion.evaluate,
        )
    )
    await read_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.5)
    assert "DB cleanup failed stage=context_close error_type=ValueError" in caplog.text
    assert "private cleanup detail" not in caplog.text
    assert "connection termination failed error_type=RuntimeError" in caplog.text
    assert "private termination detail" not in caplog.text


@pytest.mark.asyncio
async def test_read_failure_stage_survives_close_failure_with_cleanup_diagnostic(
    monkeypatch,
):
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
            return "invalid context json"

        async def close(self):
            raise ValueError("private cleanup detail")

    async def connect(_dsn, *, timeout):
        assert timeout == 1
        return Connection()

    app = api.create_app(
        _config(db_dsn_env="AVATAR_TEST_DSN"), environ=os.environ, conn_factory=connect
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = (
            await client.post("/evaluate", json=_payload(context_source="db"))
        ).json()
    assert result["stage"] == "context_read"
    assert result["error_code"] == "JSONDecodeError"
    assert result["cleanup_diagnostics"] == [
        {"stage": "context_close", "error_type": "ValueError"}
    ]


@pytest.mark.asyncio
async def test_context_close_timeout_before_total_deadline_does_not_close_visual_window(
    monkeypatch,
):
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
            return json.dumps(
                {"identity": [], "worldview": [], "goals": [], "relationships": []}
            )

        async def close(self):
            raise asyncio.TimeoutError

    async def connect(_dsn, *, timeout):
        assert timeout == 1
        return Connection()

    app = api.create_app(
        _config(db_dsn_env="AVATAR_TEST_DSN"), environ=os.environ, conn_factory=connect
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = (
            await client.post("/evaluate", json=_payload(context_source="db"))
        ).json()
    assert result["error_code"] == "ContextStageTimeout"
    assert result["stage"] == "context_close"
    assert "visual_window" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeout_seconds", "total_timeout_seconds", "expected_reason"),
    [(0.01, 0.2, "model_timeout"), (0.2, 0.01, "total_deadline")],
)
async def test_deadline_wins_when_evaluator_ignores_cancellation(
    timeout_seconds, total_timeout_seconds, expected_reason
):
    api = _api_module()

    async def ignore_cancel(*_args):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)
            return {"label": "happy", "wordset_version": "v1"}

    started = asyncio.get_running_loop().time()
    result = await api.run_evaluation(
        _payload(),
        api.PreviewConfig.model_validate(
            _config(
                timeout_seconds=timeout_seconds,
                total_timeout_seconds=total_timeout_seconds,
            )
        ),
        api_key="test-key",
        resolved_db_dsn=None,
        conn_factory=api._connect,
        evaluator=ignore_cancel,
    )
    assert result["status"] == "error"
    assert result["visual_window"]["status"] == "closed"
    assert result["visual_window"]["reason"] == expected_reason
    assert asyncio.get_running_loop().time() - started < 0.15


@pytest.mark.asyncio
async def test_early_sdk_timeout_is_an_error_without_closing_visual_window():
    api = _api_module()

    async def early_timeout(*_args):
        raise asyncio.TimeoutError("provider timeout")

    result = await api.run_evaluation(
        _payload(),
        api.PreviewConfig.model_validate(_config()),
        api_key="test-key",
        resolved_db_dsn=None,
        conn_factory=api._connect,
        evaluator=early_timeout,
    )
    assert result["status"] == "error"
    assert result["error_code"] == "TimeoutError"
    assert "visual_window" not in result


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
        assert pending.json()["identity"] == body["identity"]
        assert pending.json()["visual_owner"] == "service"
        assert pending.json()["visual_window"] == {
            "status": "open",
            "owner": "service",
            "decision_seq": 0,
        }
        second = asyncio.create_task(client.post("/evaluate", json=body))
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        responses = await asyncio.gather(first, second)
        completed_query = await client.get(api.evaluation_path(body["identity"]))
    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json()["label"] for response in responses] == [
        "curious",
        "curious",
    ]
    assert calls == 1
    assert completed_query.json() == responses[0].json()
    assert completed_query.json()["visual_window"] == {
        "status": "accepted",
        "owner": "service",
        "decision_seq": 1,
    }


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
    assert result["stage"] == "context_read"
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
    raw_database_context = {
        key: value for key, value in context.items() if key != "relationships"
    }
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
            return json.dumps(
                {**raw_database_context, "dopamine_state": {"secret": True}}
            )

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
    assert response.json()["error_code"] == "TotalDeadlineExceeded"
    assert response.json()["stage"] == "context_read"
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
    assert response.json()["error_code"] == "TotalDeadlineExceeded"
    assert response.json()["stage"] == (
        "context_connect" if blocked_stage == "connect" else "context_close"
    )
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
