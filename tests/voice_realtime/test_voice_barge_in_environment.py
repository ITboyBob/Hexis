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
    build_talk_ui_environment,
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
_MIGRATION_SOURCE = environment_module.m6_migration_source_manifest()
MIGRATION_VERIFICATION = {
    **_MIGRATION_SOURCE, "status": "verified_read_only", "ledger_table": "public.schema_migrations",
    "ledger_count": len(_MIGRATION_SOURCE["committed_files"]),
    "ledger_latest_version": max(Path(relative).stem for relative in _MIGRATION_SOURCE["committed_files"]),
}


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


def test_api_environment_allows_runtime_and_database_credentials_only():
    required = {
        "PATH": "/usr/bin", "HOME": "/unit/home", "TMPDIR": "/unit/tmp",
        "LANG": "en_US.UTF-8", "TZ": "Asia/Shanghai", "VIRTUAL_ENV": "/unit/venv",
        "PYTHONPATH": "/unit/python", "PYTHONUNBUFFERED": "1",
        "XDG_CACHE_HOME": "/unit/cache", "PYTHONPYCACHEPREFIX": "/unit/pycache",
        "POSTGRES_USER": "unit-role", "POSTGRES_PASSWORD": "unit-db-secret",
    }
    excluded = {
        "OPENAI_API_KEY": "unit-openai", "DASHSCOPE_API_KEY": "unit-dashscope",
        "AWS_ACCESS_KEY_ID": "unit-aws", "GOOGLE_APPLICATION_CREDENTIALS": "/unit/cloud.json",
        "ARBITRARY_PROVIDER_SECRET": "unit-custom", "HEXIS_API_KEY": "unit-auth",
        "DATABASE_URL": "postgresql://ambient", "HEXIS_DATABASE_URL": "postgresql://ambient",
        "HEXIS_REALTIME_CONFIG": "/unit/ambient-config.json",
        "HEXIS_VOICE_BARGE_IN_ENV": "/unit/stale-e1.json",
    }
    assert environment_module.build_api_environment({**required, **excluded}) == required


def test_api_environment_includes_only_an_explicitly_selected_provider_variable():
    parent = {
        "PATH": "/usr/bin", "POSTGRES_USER": "m6-role", "POSTGRES_PASSWORD": "unit-db",
        "DASHSCOPE_API_KEY": "selected-provider-secret", "OPENAI_API_KEY": "other-provider-secret",
    }
    safe = environment_module.build_api_environment(parent)
    assert "DASHSCOPE_API_KEY" not in safe and "OPENAI_API_KEY" not in safe
    selected = environment_module.build_api_environment(parent, provider_key_name="DASHSCOPE_API_KEY")
    assert selected["DASHSCOPE_API_KEY"] == "selected-provider-secret"
    assert "OPENAI_API_KEY" not in selected
    with pytest.raises(ValueError, match="not present"):
        environment_module.build_api_environment(parent, provider_key_name="MISSING_PROVIDER_KEY")


def test_m6_provider_key_selection_must_match_c5_name_and_be_present():
    profile = environment_module.load_profile(PROFILE_PATH)
    assert environment_module.validate_selected_provider_key_env(
        profile, "DASHSCOPE_API_KEY", {"DASHSCOPE_API_KEY": "unit-provider-secret"}
    ) == "DASHSCOPE_API_KEY"
    with pytest.raises(ValueError, match="exactly match C5"):
        environment_module.validate_selected_provider_key_env(
            profile, "OPENAI_API_KEY", {"OPENAI_API_KEY": "unit-other-provider"}
        )
    with pytest.raises(ValueError, match="not present"):
        environment_module.validate_selected_provider_key_env(profile, "DASHSCOPE_API_KEY", {})


def test_talk_ui_environment_binds_clone_and_api_without_ambient_provider_credentials():
    selected = {
        "PATH": "/usr/bin", "HOME": "/unit/home", "XDG_CACHE_HOME": "/unit/cache",
        "POSTGRES_USER": "m6-role", "POSTGRES_PASSWORD": "db:p/a%ss",
        "OPENAI_API_KEY": "must-not-pass", "CUSTOM_CLOUD_TOKEN": "must-not-pass-either",
        "HEXIS_DATABASE_URL": "postgresql://ambient", "HEXIS_API_URL": "http://ambient:1",
    }
    record = {
        "env_id": "vb-talk", "owned_resources": {"talk_database": "hexis_memory", "talk_host_port": 43852},
        "db_identity": {"talk": {"database": "hexis_memory", "server_port": 5432, "host_port": 43852}},
    }
    environment = build_talk_ui_environment(
        selected,
        environment_id="vb-talk",
        environment_record_path=Path("/unit/cache/hexis/voice-barge-in/environments/vb-talk/environment.json"),
        environment_record=record,
        profile=environment_module.load_profile(PROFILE_PATH),
        port=3482,
        dist_dir=".next/voice-barge-in-talk-unit",
    )
    assert environment["HEXIS_VOICE_BARGE_IN_ENV"].endswith("vb-talk/environment.json")
    assert environment["HEXIS_UI_TEST_TARGET"] == "talk"
    assert environment["HEXIS_UI_DIST_DIR"] == ".next/voice-barge-in-talk-unit"
    assert environment["HEXIS_API_URL"] == "http://127.0.0.1:43817"
    assert environment["PORT"] == "3482"
    assert environment["DATABASE_URL"] == environment["HEXIS_DATABASE_URL"]
    assert "m6-role:db%3Ap%2Fa%25ss@127.0.0.1:43852/hexis_memory" in environment["HEXIS_DATABASE_URL"]
    assert "OPENAI_API_KEY" not in environment and "CUSTOM_CLOUD_TOKEN" not in environment
    assert environment["HEXIS_BROWSER_API_URL"] == environment_module.load_profile(PROFILE_PATH)["api_url"].rstrip("/")


def test_api_allowlist_does_not_change_compose_or_pytest_environment_inheritance():
    parent = {"POSTGRES_USER": "unit-role", "POSTGRES_PASSWORD": "unit-secret", "CUSTOM_CLOUD_TOKEN": "unit-token"}
    record = {"owned_resources": {
        "pytest_host_port": 43851, "talk_host_port": 43852, "database_image_digest": "unit-image@sha256:unit",
    }}
    compose_env = environment_module._compose_environment("vb-unit", record, base=parent)
    pytest_env = build_pytest_environment(parent, {"host_port": 43851})
    assert compose_env["CUSTOM_CLOUD_TOKEN"] == pytest_env["CUSTOM_CLOUD_TOKEN"] == "unit-token"
    assert parent == {"POSTGRES_USER": "unit-role", "POSTGRES_PASSWORD": "unit-secret", "CUSTOM_CLOUD_TOKEN": "unit-token"}


def test_selected_ui_origin_uses_c6_or_explicit_owned_port():
    profile = environment_module.load_profile(PROFILE_PATH)
    assert environment_module.selected_ui_origin(profile) == profile["ui_url"].rstrip("/")
    assert environment_module.selected_ui_origin(profile, "http://127.0.0.1:3482") == "http://127.0.0.1:3482"
    for origin in ("https://outside.example", "http://127.0.0.1:3482/chat", "http://user:pass@127.0.0.1:3482"):
        with pytest.raises(ValueError):
            environment_module.selected_ui_origin(profile, origin)


def test_selected_api_port_binds_endpoint_to_explicit_startup_source():
    profile = environment_module.load_profile(PROFILE_PATH)
    assert environment_module.selected_api_url(profile) == profile["api_url"].rstrip("/")
    assert environment_module.selected_api_url(profile, 443) == "http://127.0.0.1:443"
    record = {"browser_api_url": "http://127.0.0.1:43877", "api_url_source": "start_api_cli",
              "endpoints": {"api_host": "127.0.0.1", "api_port": 43877}}
    assert environment_module.validate_api_start_endpoint(profile, record) == record["browser_api_url"]
    for port in (0, 65536, True, "43877"):
        with pytest.raises(ValueError):
            environment_module.selected_api_url(profile, port)
    for field, value in (("api_url_source", "ambient_env"), ("api_url_source", "C6_api_url"),
                         ("browser_api_url", "http://outside.example:43877"),
                         ("endpoints", {"api_host": "0.0.0.0", "api_port": 43877}),
                         ("endpoints", {"api_host": "127.0.0.1", "api_port": 43878})):
        with pytest.raises(ValueError):
            environment_module.validate_api_start_endpoint(profile, {**record, field: value})
    args = build_parser().parse_args(["start-api", "--env-id", "vb-unit", "--output-start-record", "/unit/start.json", "--api-port", "43877"])
    assert args.api_port == 43877


@pytest.mark.parametrize("api_port", [43817, 43877])
def test_talk_ui_start_requires_api_identity_and_records_only_runtime_identity(monkeypatch, tmp_path, api_port):
    selected_api = f"http://127.0.0.1:{api_port}"
    cache_root = tmp_path / "cache"
    env_id = "vb-talk"
    e1_path = cache_root / "voice-barge-in" / "environments" / env_id / "environment.json"
    api_record_path = cache_root / "voice-barge-in" / "api-runs" / "api-start.json"
    api_record_path.parent.mkdir(parents=True)
    api_record_path.write_text(json.dumps({
        "environment_id": env_id,
        "start_id": "selected-start",
        "session_execution_policy": environment_module.M6_SESSION_EXECUTION_POLICY,
        "acceptance_session_id": "11111111-1111-4111-8111-111111111111",
        "migration_verification": MIGRATION_VERIFICATION,
        "browser_api_url": selected_api,
        "api_url_source": "start_api_cli",
        "ui_origin": "http://127.0.0.1:3482", "ui_origin_source": "start_api_cli",
        "source": {"repository_root": str(REPOSITORY_ROOT)},
        "endpoints": {"api_host": "127.0.0.1", "api_port": api_port},
    }), encoding="utf-8")
    record = {
        "env_id": env_id,
        "status": "ready",
        "repository_root": str(REPOSITORY_ROOT),
        "owned_resources": {
            "talk_database": "hexis_memory", "talk_host_port": 43852,
            "api_process": {
                "status": "healthy", "api_url": selected_api,
                "start_id": "selected-start", "start_record_path": str(api_record_path),
            },
        },
        "db_identity": {"talk": {"database": "hexis_memory", "host_port": 43852, "server_port": 5432}},
        "source_clone": {"agent_status_matches": True},
    }
    monkeypatch.setattr(environment_module, "_read_environment_record", lambda *args: (e1_path, record))
    monkeypatch.setattr(environment_module, "_assert_port_available", lambda *args: None)
    monkeypatch.setattr(environment_module, "_talk_ui_fingerprints", lambda: {"hexis-ui/next.config.ts": "source-hash"})
    monkeypatch.setattr(environment_module, "_run", lambda *args, **kwargs: SimpleNamespace(stdout="unit-head"))
    monkeypatch.setattr(environment_module, "_wait_for_worker_page", lambda *args, **kwargs: 200)
    monkeypatch.setattr(environment_module, "_register_selected_acceptance_session", lambda *args: {
        "session_id": "11111111-1111-4111-8111-111111111111",
        "api_start_id": "selected-start", "session_execution_policy": environment_module.M6_SESSION_EXECUTION_POLICY,
    })
    identity = {
        "env_id": env_id,
        "browser_api_url": selected_api,
        "ui_origin": "http://127.0.0.1:3482",
        "database": {
            "database": "hexis_memory", "server_port": 5432, "url_port": 43852,
            "url_source": "HEXIS_DATABASE_URL", "url_host": "127.0.0.1",
        },
        "api": {"status": "ok", "checks": {"db": True}, "url_host": "127.0.0.1", "url_port": api_port,
                "session_execution_policy": environment_module.M6_SESSION_EXECUTION_POLICY,
                "acceptance_session_id": "11111111-1111-4111-8111-111111111111",
                "acceptance_session_registered": True, "start_id": "selected-start",
                "migration_verification": MIGRATION_VERIFICATION},
    }
    identity["api"]["browser_api_url"] = identity["browser_api_url"]
    identity["api"]["ui_origin"] = identity["ui_origin"]
    monkeypatch.setattr(environment_module, "_read_talk_runtime_identity", lambda *args: identity)
    class HealthResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps({"status": "ok", "checks": {"db": True}, "voice_barge_in": {
                "environment_id": env_id, "database": "hexis_memory", "start_id": "selected-start",
                "session_execution_policy": environment_module.M6_SESSION_EXECUTION_POLICY,
                "acceptance_session_id": "11111111-1111-4111-8111-111111111111",
                "acceptance_session_registered": True,
                "migration_verification": MIGRATION_VERIFICATION,
                "browser_api_url": selected_api,
                "ui_origin": "http://127.0.0.1:3482",
            }}).encode()

    monkeypatch.setattr(environment_module, "urlopen", lambda *args, **kwargs: HealthResponse())
    launches = []

    def launch(command, **kwargs):
        launches.append((command, kwargs["env"]))
        return SimpleNamespace(pid=54321, poll=lambda: None)

    monkeypatch.setattr(environment_module.subprocess, "Popen", launch)
    output_record = cache_root / "voice-barge-in" / "ui-runs" / "u1-talk.json"
    args = SimpleNamespace(
        env_id=env_id, cache_root=cache_root, profile=PROFILE_PATH, port=3482,
        mode="talk", target="dev", output_record=output_record,
    )
    parent = {
        "PATH": "/usr/bin", "HOME": "/unit/home", "POSTGRES_USER": "m6-role",
        "POSTGRES_PASSWORD": "m6-db-secret", "OPENAI_API_KEY": "ambient-cloud-secret",
        "CUSTOM_CLOUD_TOKEN": "ambient-custom-secret",
    }
    monkeypatch.setattr(environment_module.os, "environ", parent)

    environment_module._start_talk_ui(args)

    run_record = json.loads(output_record.read_text(encoding="utf-8"))
    serialized = json.dumps(run_record)
    assert run_record["status"] == "ready"
    assert run_record["runtime_identity"] == identity
    assert run_record["api_start_id"] == "selected-start"
    assert run_record["ui_url"] == "http://127.0.0.1:3482/chat"
    assert "m6-db-secret" not in serialized and "ambient-cloud-secret" not in serialized
    assert "OPENAI_API_KEY" not in launches[0][1] and "CUSTOM_CLOUD_TOKEN" not in launches[0][1]
    assert launches[0][1]["HEXIS_UI_TEST_TARGET"] == "talk"
    assert launches[0][1]["HEXIS_API_URL"] == selected_api


def test_talk_ui_stop_retires_only_a_matching_owned_record(monkeypatch, tmp_path):
    cache_root = tmp_path / "cache"
    run_path = cache_root / "voice-barge-in" / "ui-runs" / "u1-stopped-talk.json"
    run_path.parent.mkdir(parents=True)
    run_path.write_text(json.dumps({
        "kind": "talk_ui_run", "mode": "talk", "env_id": "vb-talk", "run_id": "u1-stopped-talk",
        "status": "ready", "ui_port": 3482, "process": {"pid": 1234, "process_group_id": 1234},
    }), encoding="utf-8")
    monkeypatch.setattr(environment_module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=""))
    args = SimpleNamespace(env_id="vb-talk", cache_root=cache_root, run_record=run_path)
    environment_module._stop_talk_ui(args)
    assert json.loads(run_path.read_text(encoding="utf-8"))["status"] == "stopped"


@pytest.mark.parametrize("missing_name", ["POSTGRES_USER", "POSTGRES_PASSWORD"])
def test_api_launch_rejects_missing_database_credentials_before_creating_a_process(monkeypatch, tmp_path, missing_name):
    parent = {"POSTGRES_USER": "unit-role", "POSTGRES_PASSWORD": "unit-db-secret"}
    del parent[missing_name]
    monkeypatch.setattr(environment_module.os, "environ", parent)
    record = {
        "status": "ready", "source_clone": {"agent_status_matches": True},
        "owned_resources": {
            "pytest_host_port": 43851, "talk_host_port": 43852,
            "database_image_digest": "unit-image@sha256:unit", "talk_database": "hexis_memory",
        },
    }
    e1_path = tmp_path / "voice-barge-in/environments/vb-api/environment.json"
    start_path = tmp_path / "voice-barge-in/api-runs/start.json"
    args = SimpleNamespace(env_id="vb-api", cache_root=tmp_path, profile=PROFILE_PATH, output_start_record=start_path)
    monkeypatch.setattr(environment_module, "_read_environment_record", lambda *args: (e1_path, record))
    monkeypatch.setattr(environment_module, "_assert_port_available", lambda *args: None)
    monkeypatch.setattr(environment_module, "_atomic_json_write", lambda *args: pytest.fail("E1 mutated"))
    monkeypatch.setattr(environment_module.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("API process started"))
    with pytest.raises(ValueError, match=missing_name):
        environment_module._start_api(args)
    assert not start_path.with_suffix(".log").exists()
    assert "api_process" not in record["owned_resources"]


@pytest.mark.parametrize("exits_before_health", [False, True])
@pytest.mark.parametrize("api_port", [None, 43877])
def test_api_launch_strips_ambient_credentials_and_uses_selected_e1(monkeypatch, tmp_path, exits_before_health, api_port):
    selected_api = environment_module.selected_api_url(environment_module.load_profile(PROFILE_PATH), api_port)
    monkeypatch.setattr(environment_module.os, "environ", {
        "PATH": "/usr/bin", "XDG_CACHE_HOME": "/unit/cache",
        "POSTGRES_USER": "unit-role", "POSTGRES_PASSWORD": "unit-db-secret",
        "OPENAI_API_KEY": "unit-cloud", "CUSTOM_CLOUD_TOKEN": "unit-custom",
        "HEXIS_VOICE_BARGE_IN_ENV": "/unit/stale-e1.json",
        "HEXIS_VOICE_BARGE_IN_API_START_RECORD": "/unit/stale-start.json",
        "HEXIS_VOICE_BARGE_IN_LISTEN_PORT": "9999",
    })
    record = {
        "status": "ready", "source_clone": {"agent_status_matches": True},
        "owned_resources": {
            "pytest_host_port": 43851, "talk_host_port": 43852,
            "database_image_digest": "unit-image@sha256:unit", "talk_database": "hexis_memory",
        },
    }
    e1_path = tmp_path / "voice-barge-in/environments/vb-api/environment.json"
    start_path = tmp_path / "voice-barge-in/api-runs/start.json"
    args = SimpleNamespace(env_id="vb-api", cache_root=tmp_path, profile=PROFILE_PATH, output_start_record=start_path, api_port=api_port)
    monkeypatch.setattr(environment_module, "_read_environment_record", lambda *args: (e1_path, record))
    monkeypatch.setattr(environment_module, "_assert_port_available", lambda *args: None)
    monkeypatch.setattr(environment_module, "_atomic_json_write", lambda *args: None)
    monkeypatch.setattr(environment_module.subprocess, "run", lambda *args, **kwargs: pytest.fail("real subprocess invoked"))
    launches = []

    def launch(command, **kwargs):
        launches.append((command, kwargs["env"]))
        start_path.write_text(json.dumps({"environment_id": "vb-api", "start_id": "unit-start", "session_execution_policy": environment_module.M6_SESSION_EXECUTION_POLICY, "migration_verification": MIGRATION_VERIFICATION, "browser_api_url": kwargs["env"]["HEXIS_BROWSER_API_URL"], "api_url_source": kwargs["env"]["HEXIS_VOICE_BARGE_IN_API_URL_SOURCE"], "endpoints": {"api_host": "127.0.0.1", "api_port": int(kwargs["env"]["HEXIS_VOICE_BARGE_IN_LISTEN_PORT"])}, "ui_origin": kwargs["env"]["HEXIS_UI_ORIGIN"]}), encoding="utf-8")
        return SimpleNamespace(pid=12345, poll=lambda: 1 if exits_before_health else None)

    class HealthResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps({"status": "ok", "voice_barge_in": {
                "environment_id": "vb-api", "database": "hexis_memory", "start_id": "unit-start",
                "session_execution_policy": environment_module.M6_SESSION_EXECUTION_POLICY,
                "migration_verification": MIGRATION_VERIFICATION,
                "browser_api_url": selected_api,
                "ui_origin": environment_module.selected_ui_origin(environment_module.load_profile(PROFILE_PATH)),
            }}).encode()

    monkeypatch.setattr(environment_module.subprocess, "Popen", launch)
    requests = []
    def request(url, **kwargs):
        requests.append(url)
        return HealthResponse()
    monkeypatch.setattr(environment_module, "urlopen", request)
    if exits_before_health:
        with pytest.raises(RuntimeError, match="exited before health"):
            environment_module._start_api(args)
        assert record["owned_resources"]["api_process"]["status"] == "exited_before_health"
    else:
        environment_module._start_api(args)
        assert record["owned_resources"]["api_process"]["status"] == "healthy"
        assert requests == [selected_api + "/health"]
    child_env = launches[0][1]
    assert "OPENAI_API_KEY" not in child_env and "CUSTOM_CLOUD_TOKEN" not in child_env
    assert child_env["POSTGRES_USER"] == "unit-role"
    assert child_env["POSTGRES_PASSWORD"] == "unit-db-secret"
    assert child_env["PATH"] == "/usr/bin" and child_env["XDG_CACHE_HOME"] == "/unit/cache"
    assert child_env["HEXIS_VOICE_BARGE_IN_ENV"] == str(e1_path)
    assert child_env["HEXIS_BROWSER_API_URL"] == selected_api
    assert child_env["HEXIS_UI_ORIGIN"] == environment_module.selected_ui_origin(environment_module.load_profile(PROFILE_PATH))
    assert child_env["HEXIS_VOICE_BARGE_IN_API_START_RECORD"] == str(start_path)
    assert child_env["HEXIS_VOICE_BARGE_IN_LISTEN_HOST"] == "127.0.0.1"
    assert child_env["HEXIS_VOICE_BARGE_IN_LISTEN_PORT"] == launches[0][0][-1]
    assert child_env["VOICE_BARGE_IN_ENV_ID"] == "vb-api"
    assert child_env["VOICE_BARGE_IN_TALK_DB_PORT"] == "43852"


@pytest.mark.parametrize("m6_selected", [False, True])
def test_api_main_loads_dotenv_only_outside_m6(monkeypatch, tmp_path, m6_selected):
    import dotenv
    import uvicorn

    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("UNIT_PROVIDER_CREDENTIAL=unit-cloud-secret\n", encoding="utf-8")
    monkeypatch.setattr(environment_module.os, "environ", {})
    if m6_selected:
        monkeypatch.setenv("HEXIS_VOICE_BARGE_IN_ENV", "/unit/selected-e1.json")
    real_load_dotenv = dotenv.load_dotenv
    loads = []

    def load_selected_dotenv():
        loads.append(dotenv_path)
        return real_load_dotenv(dotenv_path)

    monkeypatch.setattr(dotenv, "load_dotenv", load_selected_dotenv)
    runs = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: runs.append((args, kwargs)))
    hexis_api.main(["--host", "127.0.0.1", "--port", "43853"])
    assert len(runs) == 1 and runs[0][1]["port"] == 43853
    if m6_selected:
        assert loads == []
        assert "UNIT_PROVIDER_CREDENTIAL" not in environment_module.os.environ
    else:
        assert loads == [dotenv_path]
        assert environment_module.os.environ["UNIT_PROVIDER_CREDENTIAL"] == "unit-cloud-secret"


@pytest.mark.parametrize("api_port", [43817, 43877])
def test_api_start_record_lists_selected_provider_variable_name_without_credential_value(monkeypatch, tmp_path, api_port):
    monkeypatch.setenv("POSTGRES_USER", "m6-role")
    monkeypatch.setenv("POSTGRES_PASSWORD", "m6-db-secret")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "must-not-enter-record")
    monkeypatch.setenv("HEXIS_VOICE_BARGE_IN_PROVIDER_KEY_ENV", "DASHSCOPE_API_KEY")
    monkeypatch.setenv("HEXIS_VOICE_BARGE_IN_LISTEN_HOST", "127.0.0.1")
    monkeypatch.setenv("HEXIS_VOICE_BARGE_IN_LISTEN_PORT", str(api_port))
    monkeypatch.setenv("HEXIS_BROWSER_API_URL", f"http://127.0.0.1:{api_port}")
    monkeypatch.setenv("HEXIS_VOICE_BARGE_IN_API_URL_SOURCE", "start_api_cli")
    record = {
        "env_id": "vb-api-record",
        "repository_root": str(REPOSITORY_ROOT),
        "head": "unit-head",
        "source_clone": {"container_id": "clone-id", "database": "hexis_memory"},
    }
    output = tmp_path / "api-start.json"
    result = hexis_api._write_voice_barge_in_api_start_record(
        output,
        record,
        {"database": "hexis_memory", "server_address": "127.0.0.1", "server_port": 5432},
    )
    serialized = output.read_text(encoding="utf-8")
    assert result["credential_env_names"] == ["POSTGRES_USER", "POSTGRES_PASSWORD", "DASHSCOPE_API_KEY"]
    assert "must-not-enter-record" not in serialized
    assert "m6-db-secret" not in serialized
    assert result["api_url_source"] == "start_api_cli"
    assert result["endpoints"]["api_port"] == api_port
    assert result["browser_api_url"] == f"http://127.0.0.1:{api_port}"


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


@pytest.fixture
def rebind_case(monkeypatch, tmp_path):
    profile = environment_module.load_profile(PROFILE_PATH)
    old_fingerprint, clone_fingerprint, agent_fingerprint = "a" * 64, "b" * 64, "c" * 64
    record = {
        "schema_version": 1, "env_id": "vb-rebind", "status": "ready",
        "repository_root": str(REPOSITORY_ROOT), "head": "unit-head",
        "profile_fingerprint": old_fingerprint, "credential_env_names": ["POSTGRES_USER", "POSTGRES_PASSWORD"],
        "owned_resources": {
            "project_name": "hexis-vb-rebind", "pytest_service": "pytest-db", "talk_service": "talk-db",
            "pytest_host_port": 43851, "talk_host_port": 43852,
            "pytest_database": "postgres", "talk_database": "hexis_memory",
            "volumes": ["hexis-vb-rebind_pytest_data", "hexis-vb-rebind_talk_data"],
        },
        "db_identity": {
            "pytest": {"database": "postgres", "host_port": 43851, "server_port": 5432, "role": "clone-role"},
            "talk": {"database": "hexis_memory", "host_port": 43852, "server_port": 5432, "role": "clone-role"},
        },
        "source_clone": {
            "status": "verified", "recovery_verification": "full_table_content_hash",
            "source_data_fingerprint": clone_fingerprint, "target_data_fingerprint": clone_fingerprint,
            "agent_status_fingerprint": agent_fingerprint, "source_agent_status_fingerprint": agent_fingerprint,
            "agent_status_matches": True, "source_agent_configured": True,
            "source_agent_terminated": False, "source_consent_valid": True,
        },
    }
    args = SimpleNamespace(
        env_id="vb-rebind", cache_root=tmp_path, profile=PROFILE_PATH,
        expected_profile_fingerprint=old_fingerprint, expected_clone_fingerprint=clone_fingerprint,
    )
    record_path = tmp_path / "voice-barge-in/environments/vb-rebind/environment.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps(record))
    def readonly_docker(command, **_kwargs):
        assert command[:2] == ["docker", "ps"]
        return SimpleNamespace(stdout="")
    monkeypatch.setattr(environment_module, "_run", readonly_docker)
    monkeypatch.setattr(environment_module.subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected subprocess/DB command"))
    def not_alive(*_args):
        raise ProcessLookupError()
    monkeypatch.setattr(environment_module.os, "killpg", not_alive)
    return args, record_path, record, profile


def test_rebind_profile_changes_only_binding_and_preserves_history_then_is_idempotent(rebind_case):
    args, record_path, original, profile = rebind_case
    environment_module._rebind_profile(args)
    first_bytes = record_path.read_bytes()
    rebound = json.loads(first_bytes)
    expected = hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert rebound["profile_fingerprint"] == expected
    entry, = rebound["profile_binding_history"]
    assert entry["old_profile_fingerprint"] == original["profile_fingerprint"]
    assert entry["new_profile_fingerprint"] == expected
    assert entry["clone_data_fingerprint"] == original["source_clone"]["source_data_fingerprint"]
    assert {key: value for key, value in rebound.items() if key not in {"profile_fingerprint", "profile_binding_history"}} == {
        key: value for key, value in original.items() if key != "profile_fingerprint"
    }
    environment_module._rebind_profile(args)
    assert record_path.read_bytes() == first_bytes


@pytest.mark.parametrize("activity", ["api_status", "api_live", "talk_live", "worker_record", "worker_container"])
def test_rebind_profile_refuses_owned_runtime_activity_without_mutating_e1(rebind_case, monkeypatch, activity):
    args, record_path, record, _ = rebind_case
    if activity.startswith("api"):
        record["owned_resources"]["api_process"] = {"status": "healthy" if activity == "api_status" else "stopped", "pid": 123, "process_group_id": 123}
    if activity in {"talk_live", "worker_record"}:
        ui_dir = args.cache_root / "voice-barge-in/ui-runs"
        ui_dir.mkdir(parents=True)
        (ui_dir / "run.json").write_text(json.dumps({
            "env_id": args.env_id, "kind": "talk_ui_run" if activity == "talk_live" else "worker_ui_run",
            "status": "stopped" if activity == "talk_live" else "ready", "process": {"pid": 123, "process_group_id": 123},
        }))
    if activity in {"api_live", "talk_live"}:
        monkeypatch.setattr(environment_module.os, "killpg", lambda *_args: None)
    if activity == "worker_container":
        monkeypatch.setattr(environment_module, "_run", lambda *_args: SimpleNamespace(stdout="owned-container"))
    record_path.write_text(json.dumps(record))
    before = record_path.read_bytes()
    with pytest.raises(RuntimeError, match="stopped|still live|still running"):
        environment_module._rebind_profile(args)
    assert record_path.read_bytes() == before


@pytest.mark.parametrize("mutation", ["not_ready", "project", "clone_hash", "agent_hash", "consent", "db_identity", "old_binding", "expected_clone", "repository", "malformed_clone"])
def test_rebind_profile_rejects_forged_or_mismatched_e1(rebind_case, mutation):
    args, record_path, record, _ = rebind_case
    if mutation == "not_ready": record["status"] = "partially_verified"
    if mutation == "project": record["owned_resources"]["project_name"] = "other-project"
    if mutation == "clone_hash": record["source_clone"]["target_data_fingerprint"] = "d" * 64
    if mutation == "agent_hash": record["source_clone"]["agent_status_fingerprint"] = "d" * 64
    if mutation == "consent": record["source_clone"]["source_consent_valid"] = False
    if mutation == "db_identity": record["db_identity"]["talk"]["host_port"] = 43815
    if mutation == "old_binding": record["profile_fingerprint"] = "d" * 64
    if mutation == "expected_clone": args.expected_clone_fingerprint = "d" * 64
    if mutation == "repository": record["repository_root"] = "/other/checkout"
    if mutation == "malformed_clone": record["source_clone"] = []
    record_path.write_text(json.dumps(record))
    before = record_path.read_bytes()
    with pytest.raises(ValueError):
        environment_module._rebind_profile(args)
    assert record_path.read_bytes() == before


def test_rebind_profile_cli_requires_explicit_old_and_clone_fingerprints():
    args = build_parser().parse_args([
        "rebind-profile", "--env-id", "vb-rebind", "--expected-profile-fingerprint", "a" * 64,
        "--expected-clone-fingerprint", "b" * 64,
    ])
    assert args.handler is environment_module._rebind_profile


@pytest.mark.parametrize("mutation", ["agent", "new_binding", "timestamp"])
def test_rebind_profile_refuses_modified_binding_history(rebind_case, mutation):
    args, record_path, _, _ = rebind_case
    environment_module._rebind_profile(args)
    record = json.loads(record_path.read_bytes())
    entry = record["profile_binding_history"][-1]
    if mutation == "agent": entry["agent_status_fingerprint"] = "d" * 64
    if mutation == "new_binding": entry["new_profile_fingerprint"] = "d" * 64
    if mutation == "timestamp": entry["rebound_at_utc"] = "invalid"
    record_path.write_text(json.dumps(record))
    before = record_path.read_bytes()
    with pytest.raises(ValueError):
        environment_module._rebind_profile(args)
    assert record_path.read_bytes() == before


@pytest.mark.parametrize("container_exists", [False, True])
def test_rebind_failed_standalone_record_requires_absent_container_for_exact_env_and_run(rebind_case, monkeypatch, container_exists):
    args, record_path, _, _ = rebind_case
    ui_dir = args.cache_root / "voice-barge-in/ui-runs"
    ui_dir.mkdir(parents=True)
    run_id = "failed-standalone-worker"
    ui_path = ui_dir / "failed-worker.json"
    ui_path.write_text(json.dumps({
        "env_id": args.env_id, "kind": "worker_ui_run", "target": "standalone", "run_id": run_id,
        "status": "start_failed_resource_left_for_explicit_recovery", "container": None,
    }))
    ui_before, e1_before = ui_path.read_bytes(), record_path.read_bytes()
    queries = []
    def docker_readonly(command):
        queries.append(command)
        assert command[:2] == ["docker", "ps"]
        assert f"label=com.hexis.voice-barge-in.env-id={args.env_id}" in command
        if "-a" in command:
            assert f"label=com.hexis.voice-barge-in.run-id={run_id}" in command
            return SimpleNamespace(stdout="stopped-or-live-container" if container_exists else "")
        return SimpleNamespace(stdout="")
    monkeypatch.setattr(environment_module, "_run", docker_readonly)
    if container_exists:
        with pytest.raises(RuntimeError, match="matching owned container"):
            environment_module._rebind_profile(args)
        assert record_path.read_bytes() == e1_before
    else:
        environment_module._rebind_profile(args)
        assert record_path.read_bytes() != e1_before
    assert ui_path.read_bytes() == ui_before
    assert "-a" in queries[0]


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


@pytest.mark.parametrize("verification_fails", [False, True])
def test_clone_summary_is_persisted_before_verification_and_preserves_provenance(monkeypatch, tmp_path, verification_fails):
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

    def verify_clone(**kwargs):
        verification_snapshots.append(json.loads(record_path.read_text(encoding="utf-8")))
        if verification_fails:
            raise RuntimeError("controlled verification failure")
        return {"database": "hexis_memory", "size_bytes": 4096, "migration_count": 268,
                "extensions": ["plpgsql=1.0"], "table_count": 3,
                "row_count_fingerprint": "c" * 64, "source_row_count_fingerprint": "c" * 64,
                "source_data_fingerprint": "b" * 64, "target_data_fingerprint": "b" * 64,
                "data_fingerprint_bytes": 2345, "agent_status_fingerprint": "unit-agent",
                "agent_status_matches": True}

    monkeypatch.setattr(environment_module, "_verify_restored_database", verify_clone)
    if verification_fails:
        with pytest.raises(RuntimeError, match="controlled verification failure"):
            environment_module._prepare_db(args)
    else:
        environment_module._prepare_db(args)
    snapshot = verification_snapshots[0]
    final_record = json.loads(record_path.read_text(encoding="utf-8"))
    for record in (snapshot, final_record):
        for key, value in clone_result.items():
            assert record["source_clone"][key] == value
        assert "unit-secret" not in json.dumps(record)
    assert snapshot["source_clone"]["status"] == "restored"
    assert snapshot["status"] == "preparing"
    if verification_fails:
        assert final_record["source_clone"]["status"] == "restored"
        assert final_record["status"] == "failed"
        assert "talk" not in final_record["db_identity"]
        assert "recovery_verification" not in final_record["source_clone"]
        assert "source_data_fingerprint" not in final_record["source_clone"]
    else:
        assert final_record["status"] == "prepared"
        clone = final_record["source_clone"]
        assert clone["status"] == "verified"
        assert clone["recovery_verification"] == "full_table_content_hash"
        assert clone["source_data_fingerprint"] == clone["target_data_fingerprint"] == "b" * 64
        assert clone["data_fingerprint_bytes"] == 2345


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
