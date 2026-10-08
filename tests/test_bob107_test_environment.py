"""Pure configuration checks for the opt-in BOB-107 database modes."""

from __future__ import annotations

import os

import pytest

from tests.conftest import _clone_test_db_config, _verify_clone_database_server


BASE_ENV = {
    "POSTGRES_HOST": "127.0.0.1",
    "POSTGRES_PORT": "43861",
    "POSTGRES_DB": "bob107_runtime",
    "POSTGRES_USER": "hexis_user",
    "POSTGRES_PASSWORD": "test-only",
    "POSTGRES_ADMIN_DB": "postgres",
    "HEXIS_TEST_DB_TEMPLATE": "bob107_source_template",
    "HEXIS_TEST_DB_EXPECTED_HOST": "127.0.0.1",
    "HEXIS_TEST_DB_EXPECTED_PORT": "43861",
    "HEXIS_TEST_DB_EXPECTED_SERVER_VERSION": "PostgreSQL 16.9",
    "HEXIS_TEST_DB_EXPECTED_SYSTEM_IDENTIFIER": "7419852360123456789",
    "HEXIS_TEST_DB_EXPECTED_TARGET_DATABASE": "bob107_runtime",
}


def test_greenfield_remains_default_without_template_opt_in():
    assert _clone_test_db_config("greenfield", {}) is None


def test_clone_mode_requires_complete_explicit_isolation_identity():
    config = _clone_test_db_config("clone", BASE_ENV)

    assert config["template_database"] == "bob107_source_template"
    assert config["target_database"] == "bob107_runtime"
    assert config["host"] == "127.0.0.1"
    assert config["port"] == 43861
    assert config["system_identifier"] == "7419852360123456789"


@pytest.mark.parametrize("mode", ["clone", "survivor"])
def test_clone_modes_fail_closed_without_template(mode):
    env = {key: value for key, value in BASE_ENV.items() if key != "HEXIS_TEST_DB_TEMPLATE"}

    with pytest.raises(ValueError, match="HEXIS_TEST_DB_TEMPLATE"):
        _clone_test_db_config(mode, env)


def test_template_mode_rejects_user_database_host():
    env = {**BASE_ENV, "POSTGRES_HOST": "hexis_brain"}

    with pytest.raises(ValueError, match="loopback"):
        _clone_test_db_config("clone", env)


def test_template_mode_rejects_expected_port_mismatch():
    env = {**BASE_ENV, "POSTGRES_PORT": "43815"}

    with pytest.raises(ValueError, match="port"):
        _clone_test_db_config("clone", env)


def test_template_mode_rejects_target_equal_to_template():
    env = {**BASE_ENV, "POSTGRES_DB": "bob107_source_template", "HEXIS_TEST_DB_EXPECTED_TARGET_DATABASE": "bob107_source_template"}

    with pytest.raises(ValueError, match="must differ"):
        _clone_test_db_config("clone", env)


def test_template_mode_requires_server_identity():
    env = {key: value for key, value in BASE_ENV.items() if key != "HEXIS_TEST_DB_EXPECTED_SYSTEM_IDENTIFIER"}

    with pytest.raises(ValueError, match="SYSTEM_IDENTIFIER"):
        _clone_test_db_config("clone", env)


def test_unknown_db_mode_is_rejected():
    with pytest.raises(ValueError, match="db mode"):
        _clone_test_db_config("automatic", BASE_ENV)


@pytest.mark.asyncio
@pytest.mark.parametrize("server_port", [5432, 43861])
async def test_clone_server_checks_container_port_separately_from_host_port(server_port):
    config = _clone_test_db_config("clone", BASE_ENV)

    class AdminConnection:
        async def fetchrow(self, query):
            return ("postgres", str(server_port), config["server_version"], config["system_identifier"])

        async def fetch(self, query, databases):
            return [{"datname": database, "datallowconn": True} for database in databases]

        async def fetchval(self, query, database):
            return 0

    if server_port == 5432:
        await _verify_clone_database_server(AdminConnection(), config)
    else:
        with pytest.raises(RuntimeError, match="container port"):
            await _verify_clone_database_server(AdminConnection(), config)


@pytest.mark.asyncio(loop_scope="session")
async def test_clone_fixture_uses_isolated_database_with_source_identity(db_pool, request):
    if request.config.getoption("--db-mode") != "clone":
        pytest.skip("requires explicit clone database mode")

    async with db_pool.acquire() as connection:
        database_name = await connection.fetchval("SELECT current_database()")
        system_identifier = await connection.fetchval(
            "SELECT system_identifier::text FROM pg_control_system()"
        )
        chat_message_count = await connection.fetchval("SELECT count(*) FROM chat_messages")

    assert database_name.startswith("tmp_test_")
    assert database_name != "bob107_source_template"
    assert system_identifier == os.environ["HEXIS_TEST_DB_EXPECTED_SYSTEM_IDENTIFIER"]
    assert chat_message_count > 0
