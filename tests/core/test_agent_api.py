import os
from pathlib import Path
from unittest.mock import patch

import pytest

from core import agent_api
from tests.utils import _db_dsn

pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.core]


async def test_db_dsn_from_env(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "example.com")
    monkeypatch.setenv("POSTGRES_PORT", "5555")
    monkeypatch.setenv("POSTGRES_DB", "hexis_tmp")
    monkeypatch.setenv("POSTGRES_USER", "user1")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    dsn = agent_api.db_dsn_from_env()
    assert dsn == "postgresql://user1:secret@example.com:5555/hexis_tmp"


async def test_bob107_runner_dsn_bypasses_ambient_instance_registry(monkeypatch):
    source_root = str(Path(agent_api.__file__).resolve().parents[1])
    for name, value in {
        "HEXIS_BOB107_RUNNER": "1",
        "HEXIS_BOB107_SOURCE_ROOT": source_root,
        "HEXIS_BOB107_EXPECTED_DATABASE": "bob107_runtime",
        "HEXIS_BOB107_EXPECTED_PORT": "43861",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "43861",
        "POSTGRES_DB": "bob107_runtime",
        "POSTGRES_USER": "hexis_user",
        "POSTGRES_PASSWORD": "runtime-only",
    }.items():
        monkeypatch.setenv(name, value)

    with patch("core.instance.InstanceRegistry", side_effect=AssertionError("ambient registry read")):
        assert agent_api.db_dsn_from_env() == (
            "postgresql://hexis_user:runtime-only@127.0.0.1:43861/bob107_runtime"
        )


@pytest.mark.parametrize(
    "name,value",
    [
        ("POSTGRES_HOST", "localhost"),
        ("POSTGRES_DB", "hexis_memory"),
        ("POSTGRES_PORT", "43815"),
        ("HEXIS_BOB107_SOURCE_ROOT", "/Users/itboybob/Hexis"),
    ],
)
async def test_bob107_runner_dsn_rejects_nonisolated_identity(monkeypatch, name, value):
    source_root = str(Path(agent_api.__file__).resolve().parents[1])
    for key, item in {
        "HEXIS_BOB107_RUNNER": "1",
        "HEXIS_BOB107_SOURCE_ROOT": source_root,
        "HEXIS_BOB107_EXPECTED_DATABASE": "bob107_runtime",
        "HEXIS_BOB107_EXPECTED_PORT": "43861",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "43861",
        "POSTGRES_DB": "bob107_runtime",
        "POSTGRES_USER": "hexis_user",
        "POSTGRES_PASSWORD": "runtime-only",
    }.items():
        monkeypatch.setenv(key, item)
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match="isolated BOB-107 database identity"):
        agent_api.db_dsn_from_env()


async def test_connect_with_retry_times_out():
    bad_dsn = "postgresql://nope:nope@localhost:1/postgres"
    with pytest.raises(TimeoutError):
        await agent_api._connect_with_retry(bad_dsn, wait_seconds=1)  # noqa: SLF001


async def test_get_init_defaults(db_pool):
    defaults = await agent_api.get_init_defaults(_db_dsn())
    assert defaults["heartbeat_interval_minutes"] > 0
    assert defaults["max_energy"] > 0
    assert defaults["base_regeneration"] >= 0
    assert defaults["max_active_goals"] > 0
    assert defaults["maintenance_interval_seconds"] > 0


async def test_apply_agent_config_and_readback(db_pool):
    dsn = _db_dsn()
    await agent_api.apply_agent_config(
        dsn=dsn,
        heartbeat_interval_minutes=12,
        maintenance_interval_seconds=45,
        max_energy=9.5,
        base_regeneration=3.5,
        max_active_goals=4,
        objectives=["ship tests"],
        guardrails=["no sharing secrets"],
        initial_message="hello",
        tools=["recall", "create_goal"],
        llm_heartbeat={"provider": "openai", "model": "gpt-4o", "endpoint": "", "api_key_env": ""},
        llm_chat={"provider": "openai", "model": "gpt-4o-mini", "endpoint": "", "api_key_env": ""},
        contact_channels=["email"],
        contact_destinations={"email": "dev@example.com"},
        enable_autonomy=False,
        enable_maintenance=False,
        mark_configured=True,
    )

    assert await agent_api.get_config(dsn, "agent.objectives") == ["ship tests"]
    assert await agent_api.get_config(dsn, "agent.guardrails") == ["no sharing secrets"]
    assert await agent_api.get_config(dsn, "agent.initial_message") == "hello"
    tools = await agent_api.get_config(dsn, "agent.tools")
    assert isinstance(tools, list) and tools
    llm_chat = await agent_api.get_llm_config(dsn, "llm.chat")
    assert llm_chat.get("model") == "gpt-4o-mini"

    async with db_pool.acquire() as conn:
        # Phase 7 (ReduceScopeCreep): use unified config
        interval = await conn.fetchval(
            "SELECT get_config_float('heartbeat.heartbeat_interval_minutes')"
        )
        assert float(interval) == 12.0


async def test_get_agent_status_requires_consent_log(db_pool):
    dsn = _db_dsn()
    async with db_pool.acquire() as conn:
        await conn.execute("SELECT set_config('agent.is_configured', 'true'::jsonb)")
        await conn.execute("SELECT set_config('agent.consent_status', '\"consent\"'::jsonb)")
        await conn.execute("DELETE FROM config WHERE key = 'agent.consent_log_id'")

    status = await agent_api.get_agent_status(dsn)
    assert status["configured"] is False


async def test_set_agent_configured_toggles(db_pool):
    dsn = _db_dsn()
    await agent_api.set_agent_configured(dsn, configured=False)
    assert await agent_api.get_config(dsn, "agent.is_configured") is None
    await agent_api.set_agent_configured(dsn, configured=True)
    assert await agent_api.get_config(dsn, "agent.is_configured") is True
