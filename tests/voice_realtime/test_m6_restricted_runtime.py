"""M6 受限运行时纯回归：所有数据库、模型与业务执行边界均为替身。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from apps import hexis_api as api
from apps import voice_barge_in_environment as environment
from apps import voice_realtime_harness as harness
from services.m5_tool_bridge import BoundToolCall, execute_bound_tool, execute_restricted_bound_tool
from services.voice_emotion_gate import VoiceEmotionCoordinator
from services.voice_emotion_window import EmotionWindowHost


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


class Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


POLICY = environment.M6_SESSION_EXECUTION_POLICY
ARGUMENTS = {
    "submit": {"task_description": "验收测试任务"},
    "pend_message": {"message": "验收追加消息"},
    "remember": {"content": "验收记忆"},
    "add_evidence": {"memory_id": str(uuid4()), "stance": "supports", "source": {"label": "验收来源"}},
    "voice_recall": {"query": "验收查询"},
}


def bound_call(name, arguments):
    return BoundToolCall(
        actual_call_id="supplier-call", command_id=uuid4(), name=name,
        raw_arguments=json.dumps(arguments), origin_session_id=uuid4(),
        input_turn_id=str(uuid4()), input_order=1, stimulus_revision=1,
        response_id="local-response", supplier_response_id="supplier-response",
        request_id="request-1", tool_surface="m5",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(ARGUMENTS))
async def test_restricted_tools_return_schema_valid_denial_without_business_handlers(name):
    from services.m5_tool_contracts import build_receipt

    session = SimpleNamespace(validate_voice_tool_call=Mock())
    call = bound_call(name, ARGUMENTS[name])
    handler = AsyncMock(side_effect=AssertionError("业务 handler 不得执行"))
    if name == "voice_recall":
        result = await execute_restricted_bound_tool(session, call)
    else:
        result = await execute_bound_tool(session, call, handlers={name: handler}, deny_business_effects=True)
    receipt = json.loads(result)
    assert receipt == build_receipt(
        tool_name=name, call_id="supplier-call", operation_id=None,
        ok=False, state="failed", data={}, error=receipt["error"],
    )
    assert receipt["error"]["code"] == "PERMISSION_DENIED"
    assert receipt["error"]["retryable"] is False
    session.validate_voice_tool_call.assert_called_once_with(call)
    handler.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(ARGUMENTS))
async def test_restricted_tool_schema_still_rejects_invalid_arguments(name):
    session = SimpleNamespace(validate_voice_tool_call=Mock())
    receipt = json.loads(await execute_restricted_bound_tool(session, bound_call(name, {"unexpected": "value"})))
    assert receipt["error"]["code"] == "INVALID_ARGUMENTS"
    assert receipt["operation_id"] is None and receipt["ok"] is False


@pytest.mark.asyncio
async def test_restricted_tool_cannot_bypass_exact_input_validation():
    session = SimpleNamespace(validate_voice_tool_call=Mock(side_effect=ValueError("stale input")))
    with pytest.raises(ValueError, match="stale input"):
        await execute_restricted_bound_tool(session, bound_call("submit", ARGUMENTS["submit"]))


@pytest.mark.asyncio
async def test_normal_m5_handler_still_executes():
    session = SimpleNamespace(validate_voice_tool_call=Mock())
    handler = AsyncMock(return_value={"ok": False, "state": "failed", "operation_id": None, "error": {
        "code": "PERMISSION_DENIED", "message": "normal handler decision", "retryable": False,
    }})
    call = bound_call("submit", ARGUMENTS["submit"])
    receipt = json.loads(await execute_bound_tool(session, call, handlers={"submit": handler}))
    handler.assert_awaited_once_with(call, ARGUMENTS["submit"])
    assert receipt["error"]["message"] == "normal handler decision"


@pytest.mark.asyncio
async def test_restricted_hydration_and_emotion_use_only_persona_time_and_registered_session(monkeypatch):
    from services import voice_context_hydration as hydration
    from services.voice_instructions import load_m5_instructions

    queries = []
    session_id = uuid4()

    class Connection:
        async def fetchval(self, sql, *args):
            queries.append((sql, args))
            if sql == "SELECT get_temporal_context()":
                return {"now": "2026-10-08T00:00:00Z", "timezone": "UTC"}
            if sql == "SELECT get_agent_profile_context()":
                return {"persona": {"name": "真实身份"}}
            if sql == "SELECT render_active_persona($1::jsonb)":
                return "真实数据库 persona"
            pytest.fail(f"非许可数据库查询：{sql}")

        async def fetch(self, sql, *args):
            queries.append((sql, args))
            assert sql == "SELECT role, content FROM chat_messages WHERE session_id=$1::uuid ORDER BY created_at DESC, id DESC LIMIT 8"
            assert args == (session_id,)
            return [{"role": "user", "content": "本次会话真实历史"}]

    pool = SimpleNamespace(acquire=lambda: Context(Connection()))
    forbidden_memory = Mock(side_effect=AssertionError("CognitiveMemory/embedding 不得构造"))
    forbidden_emotion = AsyncMock(side_effect=AssertionError("非 C5 emotion 不得调用"))
    monkeypatch.setattr(hydration, "CognitiveMemory", forbidden_memory)
    monkeypatch.setattr(harness, "evaluate_emotion", forbidden_emotion)
    monkeypatch.setattr(harness, "_emotion_runtime_config", Mock(side_effect=AssertionError("不得读取 emotion 凭据")))
    bundle = load_m5_instructions()
    config = {"memory_limit": 10, "emotion": {"provider": "other", "model": "other", "endpoint": "https://forbidden", "api_key_env": "FORBIDDEN", "timeout_ms": 1000}}
    coordinator, summary = harness._build_runtime(config, pool=pool, session_id=session_id, stable_instructions=bundle.stable, session_execution_policy=POLICY)
    gate = await coordinator.run({"session_id": str(session_id), "input_turn_id": str(uuid4()), "input_order": 1, "stimulus_revision": 1}, "真实验收输入")
    assert gate.released and gate.context_ready
    assert gate.evaluation == {
        "status": "disabled_by_policy", "logical_call_count": 0, "transport_attempts": 0,
    }
    assert gate.visual_window is None
    assert gate.late_diagnostics == ()
    assert coordinator.last_evaluation_task is None
    assert gate.hydration.memory_count == 0
    assert "not performed" in gate.hydration.memory_context
    assert "本次会话真实历史" in gate.hydration.continuity_context
    assert summary["session_execution_policy"] == POLICY
    assert summary["emotion_execution"] == "disabled_by_policy"
    forbidden_memory.assert_not_called()
    forbidden_emotion.assert_not_called()
    assert len(queries) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("hydration_outcome", ["ready", "none", "error"])
async def test_disabled_emotion_skips_evaluation_and_visuals_but_preserves_hydration(hydration_outcome):
    identity = {"session_id": str(uuid4()), "input_turn_id": str(uuid4()), "input_order": 1, "stimulus_revision": 1}
    hydration = SimpleNamespace(per_response_instructions="真实水合结果")
    hydrator = AsyncMock(
        return_value=hydration if hydration_outcome == "ready" else None,
        side_effect=RuntimeError("水合失败") if hydration_outcome == "error" else None,
    )
    context_reader = AsyncMock(side_effect=AssertionError("disabled 禁止读取 emotion context"))
    evaluator = AsyncMock(side_effect=AssertionError("disabled 禁止调用 emotion evaluator"))
    sink = AsyncMock(side_effect=AssertionError("disabled 禁止发布视觉窗口或 label"))
    host = EmotionWindowHost(schedule=Mock(side_effect=AssertionError("disabled 禁止安排视觉窗口 timer")))
    coordinator = VoiceEmotionCoordinator(
        context_reader=context_reader, evaluator=evaluator, hydrator=hydrator,
        emotion_timeout_ms=1000, evaluation_enabled=False,
        window_host=host, on_visual_window=sink,
    )

    gate = await coordinator.run(identity, "本次输入")

    assert gate.released is True
    assert gate.context_ready is (hydration_outcome == "ready")
    assert gate.hydration is (hydration if hydration_outcome == "ready" else None)
    assert gate.evaluation == {"status": "disabled_by_policy", "logical_call_count": 0, "transport_attempts": 0}
    assert gate.visual_window is None and host.current(identity) is None
    assert gate.late_diagnostics == () and coordinator.last_evaluation_task is None
    assert gate.metrics["eval_ms"] == 0
    assert gate.metrics["release_reason"] == "disabled_by_policy"
    assert ("hydration_error_type" in gate.metrics) is (hydration_outcome == "error")
    if hydration_outcome == "error":
        assert gate.metrics["hydration_error_type"] == "RuntimeError"
    hydrator.assert_awaited_once_with(identity, "本次输入")
    context_reader.assert_not_called()
    evaluator.assert_not_called()
    sink.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_emotion_cancellation_cancels_hydration_without_visual_diagnostics():
    identity = {"session_id": str(uuid4()), "input_turn_id": str(uuid4()), "input_order": 1, "stimulus_revision": 1}
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hydrator(_identity, _message):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    forbidden = AsyncMock(side_effect=AssertionError("disabled 禁止 emotion 调用"))
    coordinator = VoiceEmotionCoordinator(
        context_reader=forbidden, evaluator=forbidden, hydrator=hydrator,
        emotion_timeout_ms=1000, evaluation_enabled=False, on_visual_window=forbidden,
    )
    task = asyncio.create_task(coordinator.run(identity, "本次输入"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled.is_set()
    assert coordinator.window_host.current(identity) is None
    assert coordinator.window_host.late_diagnostics == ()
    assert coordinator.last_evaluation_task is None
    forbidden.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("already_exists", [False, True])
async def test_registration_creates_only_owned_uuid_or_refuses_existing_session(monkeypatch, already_exists):
    from fastapi import HTTPException

    session_id = uuid4()
    queries = []
    class Connection:
        transaction = lambda self: Context(None)

        async def fetchval(self, sql, *args):
            queries.append((sql, args))
            assert args[0] == session_id
            if "EXISTS" in sql:
                return already_exists
            assert "INSERT INTO chat_sessions" in sql and "ON CONFLICT(id) DO NOTHING" in sql
            return session_id

    monkeypatch.setattr(api, "_pool", SimpleNamespace(acquire=lambda: Context(Connection())))
    monkeypatch.setattr(api, "_selected_m6_session_policy", lambda: POLICY)
    monkeypatch.setattr(api, "_voice_barge_in_start_record", {"acceptance_session_id": str(session_id), "start_id": "start-1"})
    monkeypatch.setattr(api, "_m6_registered_chat_sessions", set())
    if already_exists:
        with pytest.raises(HTTPException) as error:
            await api.register_m6_acceptance_chat_session()
        assert error.value.status_code == 409 and len(queries) == 1
        assert not api._m6_registered_chat_sessions
    else:
        first, second = await asyncio.gather(api.register_m6_acceptance_chat_session(), api.register_m6_acceptance_chat_session())
        assert first == second and first["session_id"] == str(session_id)
        assert first["session_execution_policy"] == POLICY
        assert api._m6_registered_chat_sessions == {session_id}
        assert len(queries) == 2
    assert not any("m5_" in sql or "http" in sql or "embed" in sql for sql, _ in queries)


@pytest.mark.asyncio
async def test_m6_lifespan_never_queries_or_starts_copied_task_workers(monkeypatch):
    from core import agent_api, usage

    queries = []
    source = {"mode": "committed_applied_only", "committed_files": {"db/migrations/0267_m5_selections.sql": "a" * 64}, "excluded_files": ["db/migrations/0269_agent_chat_tool_projection.sql"]}
    monkeypatch.setattr(environment, "m6_migration_source_manifest", lambda: source)
    class Connection:
        def transaction(self, **kwargs):
            assert kwargs == {"readonly": True}
            return Context(None)

        async def fetchval(self, sql):
            queries.append(sql)
            assert "to_regclass" in sql and "schema_migrations" in sql
            return "public.schema_migrations"

        async def fetch(self, sql):
            queries.append(sql)
            assert sql == "SELECT version, checksum FROM public.schema_migrations"
            return [{"version": "0267_m5_selections", "checksum": "a" * 64}]

        async def fetchrow(self, sql):
            queries.append(sql)
            assert "current_database()" in sql
            return {"database": "hexis_memory", "server_address": "127.0.0.1", "server_port": 5432}

    pool = SimpleNamespace(acquire=lambda: Context(Connection()), close=AsyncMock())
    monkeypatch.setenv("HEXIS_VOICE_BARGE_IN_ENV", "selected-e1")
    monkeypatch.delenv("HEXIS_M5_ACCEPTANCE_RUN", raising=False)
    monkeypatch.setattr(api, "_dsn", lambda: "fake-local-db")
    monkeypatch.setattr(api, "_voice_barge_in_api_start_record_path", lambda _: None)
    monkeypatch.setattr(api, "_write_voice_barge_in_api_start_record", lambda *_args, **kwargs: {"session_execution_policy": POLICY, "migration_verification": kwargs["migration_verification"]})
    monkeypatch.setattr(api.asyncpg, "create_pool", AsyncMock(return_value=pool))
    migrate = AsyncMock(side_effect=AssertionError("M6 不得执行迁移"))
    build_change = AsyncMock(side_effect=AssertionError("M6 不得写全局构建版本"))
    monkeypatch.setattr(agent_api, "apply_migrations", migrate)
    monkeypatch.setattr(agent_api, "record_build_change", build_change)
    monkeypatch.setattr(usage, "set_usage_pool", lambda _: None)
    for field in ("_m5_task_worker", "_m5_lightweight_executor", "_voice_realtime_registry", "_voice_playback_watchdog"):
        monkeypatch.setattr(api, field, None)
    monkeypatch.setattr(api, "_voice_barge_in_start_record", None)
    async with api.lifespan(api.app):
        assert api._m5_task_worker is None and api._m5_lightweight_executor is None
    assert len(queries) == 3
    assert not any("m5_tasks" in sql or "m5_actions" in sql or "UPDATE" in sql for sql in queries)
    migrate.assert_not_called()
    build_change.assert_not_called()


@pytest.mark.parametrize("field", list(POLICY))
def test_policy_rejects_missing_or_widened_permissions(field):
    changed = dict(POLICY)
    changed[field] = "enabled"
    with pytest.raises(ValueError, match="without widened permissions"):
        environment.session_execution_policy({"session_execution_policy": changed})


def test_migration_source_ignores_uncommitted_sql_without_reading_it(monkeypatch, tmp_path):
    trusted = tmp_path / "db/migrations/0267_trusted.sql"
    trusted.parent.mkdir(parents=True)
    trusted.write_text("SELECT 1;")
    foreign = trusted.parent / "0269_foreign.sql"
    foreign.write_text("EXTERNAL UNAUTHORIZED SQL")
    monkeypatch.setattr(environment.subprocess, "run", lambda command, **kwargs: SimpleNamespace(stdout="db/migrations/0267_trusted.sql\n"))
    original = type(trusted).read_text
    reads = []
    def read_trusted(path, *args, **kwargs):
        reads.append(path)
        assert path != foreign, "不得读取外来迁移 SQL"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(type(trusted), "read_text", read_trusted)
    manifest = environment.m6_migration_source_manifest(tmp_path)
    assert list(manifest["committed_files"]) == ["db/migrations/0267_trusted.sql"]
    assert manifest["excluded_files"] == ["db/migrations/0269_foreign.sql"]
    assert reads == [trusted]


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["matches", "pending", "checksum", "unknown_applied", "no_ledger"])
async def test_migration_ledger_verification_is_select_only_and_refuses_unapproved_state(condition):
    source = {"mode": "committed_applied_only", "committed_files": {"db/migrations/0267_trusted.sql": "a" * 64}, "excluded_files": ["db/migrations/0269_foreign.sql"]}
    statements = []
    class Connection:
        async def fetchval(self, sql):
            statements.append(sql)
            return None if condition == "no_ledger" else "public.schema_migrations"

        async def fetch(self, sql):
            statements.append(sql)
            rows = [{"version": "0267_trusted", "checksum": "b" * 64 if condition == "checksum" else "a" * 64}]
            if condition == "pending": return []
            if condition == "unknown_applied": rows.append({"version": "0269_foreign", "checksum": "c" * 64})
            return rows
    if condition == "matches":
        result = await api._verify_m6_migration_ledger(Connection(), source)
        assert result["status"] == "verified_read_only"
        assert result["excluded_files"] == source["excluded_files"]
        assert result["ledger_latest_version"] == "0267_trusted"
    else:
        with pytest.raises(RuntimeError):
            await api._verify_m6_migration_ledger(Connection(), source)
    assert all(sql.startswith("SELECT") for sql in statements)


@pytest.fixture
def media_boundary(monkeypatch):
    from services.voice_realtime_session import VoiceRealtimeSessionRegistry
    from services.voice_realtime_transport import BargeInCapability

    session = SimpleNamespace(
        session_id=uuid4(), media_session_id="media-owned", cloud_session_id="cloud-owned",
        stable_instructions="stable", close=AsyncMock(), _background_tasks=set(),
        barge_in_capability=BargeInCapability(supported=True, configuration_id="fixture", maximum_receipt_prefix_items=256),
    )
    registry = VoiceRealtimeSessionRegistry()
    registry.register(session.session_id, session)
    rows = {(session.session_id, session.cloud_session_id): "active", (uuid4(), "unrelated-cloud"): "active"}
    statements = []
    class Connection:
        async def execute(self, sql, *args):
            statements.append((sql, args))
            assert "WHERE session_id=$1::uuid AND qwen_session_id=$2 AND status='active'" in sql
            rows[(args[0], args[1])] = args[2]
    monkeypatch.setattr(api, "_pool", SimpleNamespace(acquire=lambda: Context(Connection())))
    monkeypatch.setattr(api, "_voice_realtime_registry", registry)
    monkeypatch.setenv("HEXIS_UI_ORIGIN", "http://127.0.0.1:3482")
    monkeypatch.delenv("HEXIS_VOICE_BARGE_IN_ENV", raising=False)
    return session, registry, rows, statements


@pytest.mark.asyncio
async def test_foreign_origin_rejection_cannot_close_the_owned_media_session(media_boundary):
    session, registry, rows, statements = media_boundary
    websocket = SimpleNamespace(headers={"origin": "http://unselected.example"}, accept=AsyncMock(), close=AsyncMock())
    await api.voice_realtime_media(websocket, session.media_session_id)
    websocket.accept.assert_not_called()
    websocket.close.assert_awaited_once_with(code=1008, reason="realtime session or origin is invalid")
    session.close.assert_not_called()
    assert registry.get(session.session_id) is session
    assert rows[(session.session_id, session.cloud_session_id)] == "active" and not statements


@pytest.mark.asyncio
@pytest.mark.parametrize("ready_failure", [False, True])
async def test_owned_origin_sends_ready_or_cleans_exact_session_on_ready_failure(media_boundary, ready_failure):
    from starlette.websockets import WebSocketDisconnect

    session, registry, rows, statements = media_boundary
    websocket = SimpleNamespace(
        headers={"origin": "http://127.0.0.1:3482"}, accept=AsyncMock(), close=AsyncMock(),
        send_json=AsyncMock(side_effect=RuntimeError("controlled ready failure") if ready_failure else None),
        receive_text=AsyncMock(side_effect=WebSocketDisconnect()),
    )
    if ready_failure:
        with pytest.raises(RuntimeError, match="controlled ready failure"):
            await api.voice_realtime_media(websocket, session.media_session_id)
    else:
        await api.voice_realtime_media(websocket, session.media_session_id)
        assert websocket.send_json.call_args_list[0].args[0]["type"] == "ready"
    websocket.accept.assert_awaited_once()
    session.close.assert_awaited_once_with("error" if ready_failure else "disconnect")
    assert rows[(session.session_id, session.cloud_session_id)] == ("failed" if ready_failure else "closed")
    assert not registry.active_sessions()
    assert len(statements) == 1
    assert list(rows.values()).count("active") == 1


@pytest.mark.asyncio
async def test_m6_explicit_stop_only_closes_this_api_start_registered_acceptance_session(media_boundary, monkeypatch):
    session, registry, rows, statements = media_boundary
    unrelated = SimpleNamespace(session_id=uuid4(), media_session_id="other-media", cloud_session_id="other-cloud", close=AsyncMock(), _background_tasks=set())
    registry.register(unrelated.session_id, unrelated)
    monkeypatch.setenv("HEXIS_VOICE_BARGE_IN_ENV", "selected-e1")
    monkeypatch.setattr(api, "_voice_barge_in_start_record", {"acceptance_session_id": str(session.session_id)})
    await api._close_registered_voice_sessions()
    session.close.assert_awaited_once_with("manual")
    unrelated.close.assert_not_called()
    assert registry.get(unrelated.session_id) is unrelated
    assert rows[(session.session_id, session.cloud_session_id)] == "closed"
    assert len(statements) == 1
