import errno
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.voice_barge_in_environment import (
    _database_copy_script,
    _target_psql_command,
    build_compose_command,
    build_environment_record,
    build_pytest_environment,
    build_worker_environment,
    build_parser,
    derive_project_name,
    validate_source_container_metadata,
    validate_environment_id,
    validate_profile,
    runtime_dsn_from_environment_record,
)
import apps.hexis_api as hexis_api
import apps.voice_barge_in_environment as environment_module


# Keep this pure unit module isolated from tests/conftest.py's project-wide DB
# autouse fixtures. These overrides prove that this module opens no DB.
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


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = REPOSITORY_ROOT / "config/voice-barge-in/run-profile.json"


def test_fingerprints_respects_selected_paths_defaults_and_missing_files(monkeypatch, tmp_path):
    (tmp_path / "present.txt").write_bytes(b"fingerprint-data")
    expected_digest = hashlib.sha256(b"fingerprint-data").hexdigest()
    monkeypatch.setattr(environment_module, "FINGERPRINT_PATHS", ("present.txt", "missing.txt"))
    assert environment_module._fingerprints(tmp_path) == {"present.txt": expected_digest, "missing.txt": "missing"}
    assert environment_module._fingerprints(tmp_path, ["present.txt"]) == {"present.txt": expected_digest}
    assert environment_module._fingerprints(tmp_path, []) == {}


def test_worker_ui_fingerprints_uses_only_its_selected_source_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(environment_module, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(environment_module.subprocess, "run", lambda *args, **kwargs: pytest.fail("real subprocess invoked"))
    monkeypatch.setattr(environment_module.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("real process invoked"))
    page = "hexis-ui/app/voice-vad-test/page.tsx"
    page_path = tmp_path / page
    page_path.parent.mkdir(parents=True)
    page_path.write_bytes(b"unit-worker-page")
    fingerprints = environment_module._worker_ui_fingerprints()
    assert fingerprints[page] == hashlib.sha256(b"unit-worker-page").hexdigest()
    assert fingerprints["hexis-ui/lib/voice-realtime/vad-assets-manifest.json"] == "missing"
    assert "config/voice-barge-in/run-profile.json" not in fingerprints


def test_worker_container_name_uses_the_complete_run_identity_without_suffix_collisions():
    environment_id = "vb-20261008-a"
    first_run = "w1-s2-20261008-a-standalone-worker"
    second_run = "w1-s2-20261008-b-standalone-worker"
    assert first_run[-8:] == second_run[-8:]
    first_name = environment_module._worker_ui_container_name(environment_id, first_run)
    second_name = environment_module._worker_ui_container_name(environment_id, second_run)
    assert first_name != second_name
    assert first_name == environment_module._worker_ui_container_name(environment_id, first_run)
    for name in (first_name, second_name):
        assert name.startswith("hexis-vb-20261008-a-worker-")
        assert environment_module.re.fullmatch(r"[a-z0-9][a-z0-9_.-]+", name)
        assert environment_module.re.fullmatch(r"[a-f0-9]{12}", name.rsplit("-", 1)[1])


@pytest.mark.parametrize("run_id", ["w1-first-dev-worker", "w1-second-dev-worker"])
def test_worker_dev_start_selects_and_records_its_own_build_directory(monkeypatch, tmp_path, run_id):
    monkeypatch.setattr(environment_module, "_assert_port_available", lambda *args: None)
    monkeypatch.setattr(environment_module, "_worker_ui_fingerprints", lambda: {"unit-source": "unit-hash"})
    monkeypatch.setattr(environment_module, "_run", lambda command, **kwargs: SimpleNamespace(stdout="unit-head"))
    monkeypatch.setattr(environment_module, "_wait_for_worker_page", lambda *args, **kwargs: 200)
    monkeypatch.setattr(environment_module.subprocess, "run", lambda *args, **kwargs: pytest.fail("real subprocess invoked"))
    launches = []

    def launch(command, **kwargs):
        launches.append((command, kwargs))
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(environment_module.subprocess, "Popen", launch)
    output_record = tmp_path / "voice-barge-in/ui-runs" / f"{run_id}.json"
    args = SimpleNamespace(
        env_id="vb-unit", mode="worker", target="dev", port=3481,
        profile=PROFILE_PATH, cache_root=tmp_path, output_record=output_record,
    )
    environment_module._start_worker_ui(args)
    record = json.loads(output_record.read_text(encoding="utf-8"))
    expected = f".next/voice-barge-in-{hashlib.sha256(run_id.encode()).hexdigest()[:12]}"
    assert launches[0][1]["env"]["HEXIS_UI_DIST_DIR"] == expected
    assert launches[0][1]["start_new_session"] is True
    assert record["dev_dist_dir"] == expected
    assert record["process"]["process_group_id"] == 12345
    assert record["status"] == "ready"


@pytest.mark.parametrize("active_listener", [False, True])
def test_port_probe_reuses_closing_connections_but_rejects_active_listeners(monkeypatch, active_listener):
    class PortProbe:
        reuse_address = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def setsockopt(self, level, option, value):
            assert (level, option, value) == (
                environment_module.socket.SOL_SOCKET, environment_module.socket.SO_REUSEADDR, 1,
            )
            self.reuse_address = True

        def bind(self, address):
            assert address == ("127.0.0.1", 3478)
            if active_listener or not self.reuse_address:
                raise OSError(errno.EADDRINUSE, "Address already in use")

    probe = PortProbe()
    monkeypatch.setattr(environment_module.socket, "socket", lambda: probe)
    if active_listener:
        with pytest.raises(RuntimeError, match="port 3478 is already occupied"):
            environment_module._assert_port_available(3478)
    else:
        environment_module._assert_port_available(3478)
    assert probe.reuse_address is True


def test_environment_id_rejects_path_components_and_project_is_deterministic():
    assert validate_environment_id("vb-20261008-a") == "vb-20261008-a"
    assert derive_project_name("vb-20261008-a") == "hexis-vb-20261008-a"
    for invalid in ("../outside", "UPPER", "with/slash", ""):
        with pytest.raises(ValueError):
            validate_environment_id(invalid)


def test_only_explicit_v2_profile_and_browser_mapping_are_accepted():
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    assert validate_profile(profile)["schema_version"] == 2
    legacy = {**profile, "schema_version": 1}
    with pytest.raises(ValueError, match="schema_version 2"):
        validate_profile(legacy)
    wrong_mapping = json.loads(json.dumps(profile))
    wrong_mapping["browser"]["sprint_usage"]["S5"] = ["inspection"]
    with pytest.raises(ValueError, match="sprint_usage"):
        validate_profile(wrong_mapping)


def test_environment_record_contains_only_non_secret_identity_fields():
    record = build_environment_record(
        environment_id="vb-20261008-a",
        repository_root=REPOSITORY_ROOT,
        head="abc123",
        profile=validate_profile(json.loads(PROFILE_PATH.read_text(encoding="utf-8"))),
        owned_resources={"project_name": "hexis-vb-20261008-a", "volumes": ["hexis-vb-db"]},
        environment={"POSTGRES_PASSWORD": "must-not-appear", "POSTGRES_USER": "test-user"},
    )
    serialized = json.dumps(record)
    assert "must-not-appear" not in serialized
    assert "test-user" not in serialized
    assert record["env_id"] == "vb-20261008-a"
    assert record["status"] == "prepared"
    assert record["profile_fingerprint"]


def test_compose_and_pytest_commands_are_bound_to_the_selected_environment():
    compose = build_compose_command("vb-20261008-a", "up", "-d", "--wait")
    assert compose[:4] == ["docker", "compose", "--project-name", "hexis-vb-20261008-a"]
    assert "down" not in compose
    env = build_pytest_environment(
        {"POSTGRES_USER": "test-user", "POSTGRES_PASSWORD": "test-secret"},
        {"host_port": 43851, "database": "postgres"},
    )
    assert env["POSTGRES_HOST"] == "127.0.0.1"
    assert env["POSTGRES_PORT"] == "43851"
    assert env["POSTGRES_DB"] == "postgres"
    assert env["POSTGRES_ADMIN_DB"] == "postgres"
    assert env["POSTGRES_PASSWORD"] == "test-secret"


def test_source_container_must_match_the_explicit_live_instance_and_primary_database():
    source = {
        "Id": "source-container-id",
        "Image": "sha256:source-image-id",
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "Config": {
            "Labels": {"com.docker.compose.project": "hexis", "com.docker.compose.service": "db"},
            "Env": ["POSTGRES_DB=hexis_memory", "POSTGRES_USER=source-role", "POSTGRES_PASSWORD=source-secret"],
        },
        "Mounts": [{"Name": "hexis_postgres_data", "Destination": "/var/lib/postgresql/data"}],
    }
    summary = validate_source_container_metadata(
        source,
        expected_container_id="source-container-id",
        expected_database="hexis_memory",
        expected_image_id="sha256:source-image-id",
    )
    assert summary["database"] == "hexis_memory"
    assert summary["container_id"] == "source-container-id"
    assert summary["data_volume"] == "hexis_postgres_data"
    assert "source-secret" not in json.dumps(summary)


def test_source_container_drift_is_rejected_before_any_clone_operation():
    source = {
        "Id": "different-container",
        "Image": "sha256:source-image-id",
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "Config": {
            "Labels": {"com.docker.compose.project": "hexis", "com.docker.compose.service": "db"},
            "Env": ["POSTGRES_DB=hexis_memory", "POSTGRES_USER=source-role"],
        },
        "Mounts": [{"Name": "hexis_postgres_data", "Destination": "/var/lib/postgresql/data"}],
    }
    with pytest.raises(ValueError, match="container ID"):
        validate_source_container_metadata(
            source,
            expected_container_id="source-container-id",
            expected_database="hexis_memory",
            expected_image_id="sha256:source-image-id",
        )


def test_runtime_dsn_uses_only_the_explicit_ready_clone_record(tmp_path):
    cache_root = tmp_path / "cache" / "hexis"
    record_path = cache_root / "voice-barge-in/environments/vb-clone/environment.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps({
        "schema_version": 1,
        "env_id": "vb-clone",
        "status": "ready",
        "repository_root": str(REPOSITORY_ROOT),
        "head": "abc123",
        "credential_env_names": ["POSTGRES_USER", "POSTGRES_PASSWORD"],
        "owned_resources": {"project_name": "hexis-vb-clone", "talk_host_port": 43852, "talk_database": "hexis_memory"},
        "db_identity": {"talk": {"database": "hexis_memory", "server_port": 5432}},
    }), encoding="utf-8")
    dsn, record = runtime_dsn_from_environment_record(
        record_path,
        {"POSTGRES_USER": "m6-clone", "POSTGRES_PASSWORD": "p@ss/word"},
    )
    assert dsn == "postgresql://m6-clone:p%40ss%2Fword@127.0.0.1:43852/hexis_memory"
    assert record["env_id"] == "vb-clone"
    assert "p@ss/word" not in json.dumps(record)


def test_m6_runtime_rejects_m5_selection_and_never_falls_back_to_ambient_registry(monkeypatch, tmp_path):
    record_path = tmp_path / "cache/hexis/voice-barge-in/environments/vb-clone/environment.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps({
        "schema_version": 1,
        "env_id": "vb-clone",
        "status": "ready",
        "credential_env_names": ["POSTGRES_USER", "POSTGRES_PASSWORD"],
        "owned_resources": {"project_name": "hexis-vb-clone", "talk_host_port": 43852},
        "db_identity": {"talk": {"database": "hexis_memory", "server_port": 5432}},
    }), encoding="utf-8")
    monkeypatch.setenv("HEXIS_VOICE_BARGE_IN_ENV", str(record_path))
    monkeypatch.setenv("HEXIS_M5_ACCEPTANCE_RUN", "m5-run.json")
    with pytest.raises(RuntimeError, match="cannot be active together"):
        hexis_api._dsn()
    monkeypatch.delenv("HEXIS_M5_ACCEPTANCE_RUN")
    monkeypatch.setenv("POSTGRES_USER", "m6-clone")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret-value")
    monkeypatch.setattr("core.agent_api.db_dsn_from_env", lambda: pytest.fail("ambient registry fallback used"))
    assert hexis_api._dsn() == "postgresql://m6-clone:secret-value@127.0.0.1:43852/hexis_memory"


def test_m6_api_mode_never_starts_cloned_m5_background_work():
    assert hexis_api._should_start_m5_background_workers(False) is True
    assert hexis_api._should_start_m5_background_workers(True) is False


def test_full_table_comparison_uses_repeatable_read_and_quotes_identifiers():
    script = _database_copy_script([("public", "config"), ('odd"schema', "table'name")])
    assert script.startswith("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;")
    assert 'FROM "public"."config" AS row_value' in script
    assert 'FROM "odd""schema"."table\'name" AS row_value' in script
    assert script.rstrip().endswith("COMMIT;")


def test_target_psql_uses_machine_parseable_output():
    command = _target_psql_command("vb-test", "hexis_memory", "m6_clone", "-c", "SELECT 1")
    assert "-A" in command
    assert "-t" in command
    assert command[command.index("-F") + 1] == "\t"
    assert "--no-TTY" in command and "--interactive" in command


def test_api_start_record_path_must_be_explicit_new_cache_output(monkeypatch, tmp_path):
    cache_root = tmp_path / "hexis"
    env_record = cache_root / "voice-barge-in/environments/vb-api/environment.json"
    env_record.parent.mkdir(parents=True)
    env_record.write_text("{}", encoding="utf-8")
    output = cache_root / "voice-barge-in/api-runs/api-a.json"
    monkeypatch.setenv("HEXIS_VOICE_BARGE_IN_ENV", str(env_record))
    monkeypatch.setenv("HEXIS_VOICE_BARGE_IN_API_START_RECORD", str(output))
    assert hexis_api._voice_barge_in_api_start_record_path(str(env_record)) == output
    monkeypatch.setenv("HEXIS_VOICE_BARGE_IN_API_START_RECORD", str(tmp_path / "outside.json"))
    with pytest.raises(RuntimeError, match="api-runs"):
        hexis_api._voice_barge_in_api_start_record_path(str(env_record))
    output.parent.mkdir(parents=True)
    output.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HEXIS_VOICE_BARGE_IN_API_START_RECORD", str(output))
    with pytest.raises(RuntimeError, match="already exists"):
        hexis_api._voice_barge_in_api_start_record_path(str(env_record))


def test_api_start_record_contains_only_non_secret_clone_and_config_fingerprints(tmp_path):
    record_path = tmp_path / "api-runs/api-a.json"
    environment_record = {
        "env_id": "vb-api",
        "head": "abc123",
        "repository_root": str(REPOSITORY_ROOT),
        "source_clone": {
            "container_id": "source-container-id",
            "database": "hexis_memory",
            "source_image_digest": "ghcr.io/example@sha256:abcdef",
            "dump_sha256": "deadbeef",
            "target_data_fingerprint": "safe-hash",
            "credential": "must-not-copy",
        },
    }
    record = hexis_api._write_voice_barge_in_api_start_record(
        record_path,
        environment_record,
        {"database": "hexis_memory", "server_address": "127.0.0.1", "server_port": 5432},
    )
    serialized = json.dumps(record)
    assert record["environment_id"] == "vb-api"
    assert record["actual_current_database"] == "hexis_memory"
    assert set(record["config_fingerprints"]) == {"C1", "C2", "C4", "C5", "C6", "C7", "C8"}
    assert record["source_clone"]["target_data_fingerprint"] == "safe-hash"
    assert "must-not-copy" not in serialized
    assert "postgresql://" not in serialized


def test_worker_ui_environment_strips_database_api_and_credential_values():
    worker_env = build_worker_environment({
        "PATH": "/usr/bin",
        "POSTGRES_PASSWORD": "db-secret",
        "POSTGRES_USER": "private-role",
        "HEXIS_DATABASE_URL": "postgresql://private",
        "DATABASE_URL": "postgresql://ambient",
        "HEXIS_API_URL": "http://ambient-api",
        "OPENAI_API_KEY": "cloud-secret",
        "SAFE_UI_SETTING": "visible",
        "LC_ALL": "zh_CN.UTF-8",
    }, port=3477)
    assert worker_env["PATH"] == "/usr/bin"
    assert worker_env["LC_ALL"] == "zh_CN.UTF-8"
    assert worker_env["HEXIS_VAD_TEST_HARNESS"] == "1"
    assert worker_env["PORT"] == "3477"
    assert not {"POSTGRES_PASSWORD", "POSTGRES_USER", "HEXIS_DATABASE_URL", "DATABASE_URL", "HEXIS_API_URL", "OPENAI_API_KEY", "SAFE_UI_SETTING"} & set(worker_env)


def test_worker_ui_cli_requires_explicit_mode_target_and_run_record():
    parsed = build_parser().parse_args([
        "start-ui",
        "--env-id", "vb-s2",
        "--mode", "worker",
        "--target", "standalone",
        "--output-record", "/cache/voice-barge-in/ui-runs/w1-s2-worker.json",
        "--cache-root", "/cache",
    ])
    assert parsed.command == "start-ui"
    assert parsed.mode == "worker"
    assert parsed.target == "standalone"
    assert parsed.output_record.name == "w1-s2-worker.json"


@pytest.mark.parametrize("target_migration_count", [268, 269])
def test_restored_metadata_uses_tab_rows_and_rejects_migration_drift(monkeypatch, target_migration_count):
    monkeypatch.setattr(environment_module, "_database_table_row_counts", lambda *args, **kwargs: {"public.memories": 3})
    monkeypatch.setattr(environment_module, "_database_table_names", lambda *args: [("public", "memories")])
    monkeypatch.setattr(environment_module, "_stream_fingerprint", lambda *args, **kwargs: ("a" * 64, 1024))
    monkeypatch.setattr(environment_module.subprocess, "run", lambda *args, **kwargs: pytest.fail("real subprocess invoked"))
    monkeypatch.setattr(environment_module.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("real process invoked"))

    def query_source(source, username, database, sql):
        if "schema_migrations" in sql:
            return "268"
        if "pg_extension" in sql:
            return "plpgsql=1.0,vector=0.8.2"
        if "get_agent_status" in sql:
            return "{}"
        pytest.fail(f"unexpected source query: {sql}")

    def run_target(command, **kwargs):
        assert "-A" in command and "-t" in command
        assert command[command.index("-F") + 1] == "\t"
        sql = command[command.index("-c") + 1]
        if "current_database()" in sql:
            output = f"hexis_memory\t16.11\t4096\t{target_migration_count}\n"
        elif "pg_extension" in sql:
            output = "plpgsql=1.0,vector=0.8.2\n"
        elif "get_agent_status" in sql:
            output = "{}\n"
        else:
            pytest.fail(f"unexpected target query: {sql}")
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(environment_module, "_query_source", query_source)
    monkeypatch.setattr(environment_module, "_run", run_target)
    arguments = {
        "source": {"Id": "unit-source"},
        "source_user": "source-role",
        "database": "hexis_memory",
        "environment_id": "vb-unit",
        "target_user": "target-role",
        "target_environment": {},
    }
    if target_migration_count != 268:
        with pytest.raises(RuntimeError, match="migration history differs"):
            environment_module._verify_restored_database(**arguments)
    else:
        identity = environment_module._verify_restored_database(**arguments)
        assert identity["database"] == "hexis_memory"
        assert identity["migration_count"] == 268
        assert identity["size_bytes"] == 4096
        assert identity["agent_status_matches"] is True
        assert identity["source_data_fingerprint"] == identity["target_data_fingerprint"] == "a" * 64
        assert identity["data_fingerprint_bytes"] == 1024


def test_clone_summary_is_persisted_before_verification_and_survives_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("POSTGRES_USER", "target-role")
    monkeypatch.setenv("POSTGRES_PASSWORD", "unit-secret")
    monkeypatch.setattr(environment_module.subprocess, "run", lambda *args, **kwargs: pytest.fail("real subprocess invoked"))
    monkeypatch.setattr(environment_module.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("real process invoked"))
    monkeypatch.setattr(environment_module, "_inspect_source_container", lambda *args: {
        "Id": "unit-source", "Config": {"Env": ["POSTGRES_USER=source-role"]},
    })
    monkeypatch.setattr(environment_module, "validate_source_container_metadata", lambda *args, **kwargs: {"database": "hexis_memory"})
    monkeypatch.setattr(environment_module, "_image_digest_reference", lambda *args: "unit-image@sha256:unit")
    monkeypatch.setattr(environment_module, "_source_database_metadata", lambda *args: {
        "server_version": "16.11", "size_bytes": 4096, "migration_count": 268,
        "extensions": ["plpgsql=1.0"], "agent_status_fingerprint": "unit-agent",
        "agent_configured": True, "agent_terminated": False, "consent_valid": True,
    })
    monkeypatch.setattr(environment_module, "_assert_port_available", lambda *args: None)

    def run(command, **kwargs):
        output = ""
        if command[0] == "git":
            output = "unit-head"
        elif command[1:3] == ["image", "inspect"]:
            output = "sha256:unit-image"
        elif command[1:3] == ["compose", "ls"]:
            output = "[]"
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(environment_module, "_run", run)
    clone_result = {"dump_sha256": "a" * 64, "dump_bytes": 1024, "restore_status": "restored"}
    monkeypatch.setattr(environment_module, "_clone_database_stream", lambda **kwargs: clone_result)
    args = SimpleNamespace(
        env_id="vb-unit", project_name="hexis-vb-unit", profile=PROFILE_PATH,
        source_container="unit-source", source_container_id="unit-source",
        source_database="hexis_memory", source_image_id="sha256:unit-image",
        pytest_db_port=43851, talk_db_port=43852, cache_root=tmp_path,
    )
    record_path = environment_module._environment_record_path(tmp_path, args.env_id)
    verification_snapshots = []

    def fail_verification(**kwargs):
        verification_snapshots.append(json.loads(record_path.read_text(encoding="utf-8")))
        raise RuntimeError("controlled verification failure")

    monkeypatch.setattr(environment_module, "_verify_restored_database", fail_verification)
    with pytest.raises(RuntimeError, match="controlled verification failure"):
        environment_module._prepare_db(args)
    snapshot = verification_snapshots[0]
    failed_record = json.loads(record_path.read_text(encoding="utf-8"))
    for record in (snapshot, failed_record):
        assert record["source_clone"]["status"] == "restored"
        for key, value in clone_result.items():
            assert record["source_clone"][key] == value
        assert "unit-secret" not in json.dumps(record)
    assert snapshot["status"] == "preparing"
    assert failed_record["status"] == "failed"
    assert "talk" not in failed_record["db_identity"]


@pytest.mark.parametrize("scope,missing_extension", [("talk", False), ("pytest", False), ("talk", True)])
def test_check_db_reads_configured_port_over_unix_socket(monkeypatch, tmp_path, scope, missing_extension):
    monkeypatch.setenv("POSTGRES_USER", "target-role")
    monkeypatch.setenv("POSTGRES_PASSWORD", "unit-secret")
    monkeypatch.setattr(environment_module.subprocess, "run", lambda *args, **kwargs: pytest.fail("real subprocess invoked"))
    monkeypatch.setattr(environment_module.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("real process invoked"))
    initial_record = {
        "env_id": "vb-unit", "status": "prepared", "db_identity": {},
        "owned_resources": {
            "project_name": "hexis-vb-unit", "pytest_host_port": 43851, "talk_host_port": 43852,
            "database_image_digest": "unit-image@sha256:unit",
            "pytest_database": "postgres", "talk_database": "hexis_memory",
        },
    }
    record_path = environment_module._environment_record_path(tmp_path, "vb-unit")
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps(initial_record), encoding="utf-8")
    database = initial_record["owned_resources"][f"{scope}_database"]
    extensions = environment_module.REQUIRED_DATABASE_EXTENSIONS - {"pgtap"}
    if missing_extension:
        extensions -= {"age"}

    def run(command, **kwargs):
        assert f"{scope}-db" in command
        sql = command[command.index("-c") + 1]
        if "current_database()" in sql:
            if "inet_server_port()" in sql:
                port = ""
            elif "current_setting('port')" in sql:
                port = "5544"
            else:
                pytest.fail(f"unexpected server port query: {sql}")
            output = f"{database}\t{port}\ttarget-role\n"
        elif "FROM pg_extension" in sql:
            output = ",".join(f"{extension}=unit-version" for extension in sorted(extensions))
        elif "pg_available_extensions" in sql or "FROM pg_roles" in sql:
            output = "true\n"
        else:
            pytest.fail(f"unexpected identity query: {sql}")
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(environment_module, "_run", run)
    args = SimpleNamespace(env_id="vb-unit", cache_root=tmp_path, profile=PROFILE_PATH, scope=scope)
    if missing_extension:
        with pytest.raises(RuntimeError, match="missing required extensions: age"):
            environment_module._check_db(args)
        assert json.loads(record_path.read_text(encoding="utf-8")) == initial_record
    else:
        environment_module._check_db(args)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        assert record["status"] == "partially_verified"
        assert record["db_identity"][scope]["server_port"] == 5544
        assert record["db_identity"][scope]["host_port"] == initial_record["owned_resources"][f"{scope}_host_port"]
        assert record["db_identity"][scope]["database"] == database
