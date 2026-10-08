"""Pure tests for M6 environment preflight and runtime-record validation."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlparse
from uuid import UUID

import pytest

from apps import voice_barge_in_probe as probe
from apps.voice_barge_in_probe import (
    build_environment_preflight,
    build_talk_evidence_verification,
    validate_talk_records,
)
from apps.voice_barge_in_environment import load_profile


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


ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT.parent / "config/voice-barge-in/run-profile.json"
PROFILE = load_profile(PROFILE_PATH)
_MIGRATION_SOURCE = probe.environment.m6_migration_source_manifest()
MIGRATION_VERIFICATION = {
    **_MIGRATION_SOURCE, "status": "verified_read_only", "ledger_table": "public.schema_migrations",
    "ledger_count": len(_MIGRATION_SOURCE["committed_files"]),
    "ledger_latest_version": max(Path(relative).stem for relative in _MIGRATION_SOURCE["committed_files"]),
}


def environment_record(root: Path = ROOT.parent, *, profile: dict = PROFILE) -> dict:
    return {
        "schema_version": 1,
        "env_id": "vb-unit",
        "repository_root": str(root.resolve()),
        "head": "a" * 40,
        "content_fingerprints": {},
        "profile_fingerprint": hashlib.sha256(
            json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        "owned_resources": {
            "project_name": "hexis-vb-unit",
            "pytest_service": "pytest-db",
            "talk_service": "talk-db",
            "pytest_database": "postgres",
            "talk_database": "hexis_memory",
            "pytest_host_port": 43851,
            "talk_host_port": 43852,
            "volumes": ["hexis-vb-unit_pytest_data", "hexis-vb-unit_talk_data"],
        },
        "db_identity": {
            "pytest": {"database": "postgres", "role": "m6clone", "host_port": 43851, "server_port": 5432},
            "talk": {"database": "hexis_memory", "role": "m6clone", "host_port": 43852, "server_port": 5432},
        },
        "source_clone": {
            "status": "verified",
            "recovery_verification": "full_table_content_hash",
            "source_data_fingerprint": "source-hash",
            "target_data_fingerprint": "source-hash",
            "agent_status_matches": True,
            "source_agent_configured": True,
            "source_agent_terminated": False,
            "source_consent_valid": True,
            "source_agent_status_fingerprint": "a" * 64,
            "agent_status_fingerprint": "a" * 64,
            "target_migration_count": 266,
        },
        "status": "ready",
    }


def test_environment_preflight_validates_owned_clone_before_live_database_probes():
    record = environment_record()
    result = build_environment_preflight(PROFILE, record, repository_root=ROOT.parent)
    assert result["kind"] == "environment_preflight"
    assert result["env_id"] == "vb-unit"
    assert result["databases"] == {}
    assert result["owned_resources"]["pytest_database"] == "postgres"
    assert result["owned_resources"]["talk_database"] == "hexis_memory"
    assert result["owned_resources"]["project_name"] == "hexis-vb-unit"


@pytest.mark.parametrize("mutation, message", [
    (lambda record: record.update(schema_version=0), "schema_version"),
    (lambda record: record["owned_resources"].update(project_name="hexis-other"), "project identity"),
    (lambda record: record["owned_resources"].update(talk_database="postgres"), "talk database"),
    (lambda record: record["source_clone"].update(target_data_fingerprint="different"), "content fingerprint"),
    (lambda record: record["source_clone"].update(agent_status_matches=False), "agent/consent"),
])
def test_environment_preflight_rejects_stale_or_misbound_e1(mutation, message):
    record = environment_record()
    mutation(record)
    with pytest.raises(ValueError, match=message):
        build_environment_preflight(PROFILE, record, repository_root=ROOT.parent)


def test_environment_preflight_rejects_profile_drift():
    record = environment_record()
    drifted = {**PROFILE, "detection_config": "missing-vad.json"}
    with pytest.raises(ValueError, match="profile fingerprint"):
        build_environment_preflight(drifted, record, repository_root=ROOT.parent)


def test_environment_preflight_rejects_missing_config_with_matching_profile():
    missing = {**PROFILE, "detection_config": "missing-vad.json"}
    record = environment_record(profile=missing)
    with pytest.raises(ValueError, match="missing configuration"):
        build_environment_preflight(missing, record, repository_root=ROOT.parent)


@pytest.fixture
def environment_phase(tmp_path, monkeypatch):
    record_path = tmp_path / "environment.json"
    record_path.write_text(json.dumps(environment_record()), encoding="utf-8")
    output = tmp_path / "preflight.json"
    writer = Mock(wraps=probe.environment._atomic_json_write)
    commands = Mock(side_effect=AssertionError("unexpected service or provider command"))
    monkeypatch.setattr(probe.environment, "_atomic_json_write", writer)
    monkeypatch.setattr(probe.environment, "_run", commands)
    argv = [
        "--phase", "environment", "--profile", str(PROFILE_PATH),
        "--env-record", str(record_path), "--output-manifest", str(output),
    ]
    return argv, output, writer, commands


def test_environment_phase_writes_once_after_both_database_probes(environment_phase, monkeypatch, capsys):
    argv, output, writer, commands = environment_phase
    identities = environment_record()["db_identity"]
    database_probe = Mock(side_effect=lambda env_id, record, scope: dict(identities[scope]))
    monkeypatch.setattr(probe, "_probe_database_scope", database_probe)

    assert probe.main(argv) == 0
    assert [call.args[2] for call in database_probe.call_args_list] == ["pytest", "talk"]
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["databases"] == identities
    writer.assert_called_once()
    commands.assert_not_called()
    assert "passed" in capsys.readouterr().out


@pytest.mark.parametrize("failed_scope", ["pytest", "talk"])
def test_environment_phase_database_probe_failure_writes_no_manifest(
    environment_phase, monkeypatch, capsys, failed_scope,
):
    argv, output, writer, commands = environment_phase

    def fail_probe(env_id, record, scope):
        if scope == failed_scope:
            raise ValueError(f"live {scope} database identity does not match E1")
        return dict(record["db_identity"][scope])

    database_probe = Mock(side_effect=fail_probe)
    monkeypatch.setattr(probe, "_probe_database_scope", database_probe)

    assert probe.main(argv) == 1
    assert not output.exists()
    writer.assert_not_called()
    commands.assert_not_called()
    expected_scopes = ["pytest"] if failed_scope == "pytest" else ["pytest", "talk"]
    assert [call.args[2] for call in database_probe.call_args_list] == expected_scopes
    assert f"live {failed_scope} database identity" in capsys.readouterr().err


def test_manifest_never_carries_database_or_provider_secrets():
    result = build_environment_preflight(PROFILE, environment_record(), repository_root=ROOT.parent)
    rendered = json.dumps(result)
    for secret in ("POSTGRES_PASSWORD", "OPENAI_API_KEY", "must-not-appear"):
        assert secret not in rendered


@pytest.fixture
def talk_records(tmp_path, monkeypatch):
    """Mirror W1/U1/API producers; block every process/network entry point."""
    side_effects = Mock(side_effect=AssertionError("unexpected process/network/cloud side effect"))
    monkeypatch.setattr(probe.environment, "_run", side_effects)
    monkeypatch.setattr(probe.environment.subprocess, "Popen", side_effects)
    monkeypatch.setattr(probe.environment.subprocess, "run", side_effects)
    monkeypatch.setattr(probe.environment, "urlopen", side_effects)
    record = environment_record()
    start_path = tmp_path / "api-start.json"
    record["owned_resources"]["api_process"] = {
        "pid": 123, "status": "healthy", "start_id": "start-1",
        "api_url": PROFILE["api_url"], "start_record_path": str(start_path),
    }
    worker_fingerprints = probe.environment._worker_ui_fingerprints()
    image_id = "sha256:" + "b" * 64
    worker = {
        "schema_version": 1, "kind": "worker_ui_run", "status": "stopped",
        "mode": "worker", "target": "standalone", "env_id": "vb-unit",
        "source_root": str(ROOT.parent), "source_fingerprints": worker_fingerprints,
        "container": {"image_id": image_id},
        "runtime_http_status": 200, "ready_at_utc": "2026-10-08T00:00:00+00:00",
        "stopped_at_utc": "2026-10-08T00:01:00+00:00",
        "model_resource_fingerprint": worker_fingerprints[PROFILE["asset_manifest"]],
        "build_fingerprint": hashlib.sha256(json.dumps(
            {"source_fingerprints": worker_fingerprints, "image_id": image_id}, sort_keys=True,
        ).encode()).hexdigest(),
    }
    api_url = urlparse(PROFILE["api_url"])
    api = {
        "schema_version": 1, "status": "started", "environment_id": "vb-unit",
        "start_id": "start-1", "pid": 123, "start_time": "2026-10-08T00:02:00+00:00",
        "session_execution_policy": PROFILE["session_execution_policy"],
        "acceptance_session_id": "11111111-1111-4111-8111-111111111111",
        "migration_verification": MIGRATION_VERIFICATION,
        "browser_api_url": PROFILE["api_url"].rstrip("/"),
        "ui_origin": "http://127.0.0.1:3482", "ui_origin_source": "start_api_cli",
        "source": {"repository_root": str(ROOT.parent), "head": record["head"]},
        "source_fingerprints": probe._api_source_fingerprints(ROOT.parent),
        "actual_current_database": "hexis_memory",
        "actual_server_address_and_port": {"address": "172.22.0.2/32", "port": 5432},
        "endpoints": {"api_host": api_url.hostname, "api_port": api_url.port,
                      "health": "/health", "voice_realtime": "/api/voice/realtime"},
        "source_clone": {key: record["source_clone"][key] for key in (
            "source_data_fingerprint", "target_data_fingerprint", "agent_status_fingerprint",
        )},
        "config_fingerprints": {label: probe.environment._sha256(ROOT.parent / relative) for label, relative in {
            "C1": PROFILE["detection_config"], "C2": PROFILE["asset_manifest"],
            "C4": PROFILE["runtime_config"], "C5": PROFILE["realtime_config"],
            "C6": "config/voice-barge-in/run-profile.json", "C7": PROFILE["playback_coordination_config"],
            "C8": PROFILE["audio_case_spec"],
        }.items()},
    }
    ui_fingerprints = probe.environment._talk_ui_fingerprints()
    ui = {
        "schema_version": 1, "kind": "talk_ui_run", "status": "ready", "mode": "talk",
        "env_id": "vb-unit", "source_root": str(ROOT.parent),
        "api_start_id": api["start_id"], "api_start_record": str(start_path),
        "session_execution_policy": PROFILE["session_execution_policy"],
        "acceptance_session_id": api["acceptance_session_id"],
        "migration_verification": MIGRATION_VERIFICATION,
        "browser_api_url": PROFILE["api_url"].rstrip("/"),
        "ui_origin": "http://127.0.0.1:3482", "ui_url": "http://127.0.0.1:3482/chat",
        "source_fingerprints": ui_fingerprints,
        "runtime_identity": {
            "env_id": "vb-unit",
            "browser_api_url": PROFILE["api_url"].rstrip("/"),
            "ui_origin": "http://127.0.0.1:3482",
            "database": {"database": "hexis_memory", "server_port": 5432,
                         "server_address": "172.22.0.2", "url_port": 43852,
                         "url_source": "HEXIS_DATABASE_URL", "url_host": "127.0.0.1"},
            "api": {"status": "ok", "checks": {"db": True},
                    "session_execution_policy": PROFILE["session_execution_policy"],
                    "acceptance_session_id": api["acceptance_session_id"],
                    "acceptance_session_registered": True, "start_id": api["start_id"],
                    "migration_verification": MIGRATION_VERIFICATION,
                    "browser_api_url": PROFILE["api_url"].rstrip("/"),
                    "ui_origin": "http://127.0.0.1:3482",
                    "url_host": api_url.hostname, "url_port": api_url.port},
        },
        "build_fingerprint": hashlib.sha256(json.dumps(
            {"source_fingerprints": ui_fingerprints, "target": "talk-dev", "api_start_id": api["start_id"]},
            sort_keys=True,
        ).encode()).hexdigest(),
    }
    sample_path = tmp_path / "noise.wav"
    sample_path.write_bytes(b"frozen sample bytes")
    audio = {"schema_version": 1, "samples": [{
        "sample_id": "noise-01", "path": str(sample_path),
        "file_sha256": probe.environment._sha256(sample_path),
    }]}
    yield worker, ui, api, audio, record
    side_effects.assert_not_called()


def test_talk_preflight_accepts_stopped_worker_and_real_startup_record(talk_records):
    result = validate_talk_records(*talk_records, PROFILE)
    assert result["env_id"] == "vb-unit"
    assert result["database"] == "hexis_memory"
    assert result["api_start_id"] == "start-1"
    assert result["audio_sample_count"] == 1
    assert result["worker_build_fingerprint"] == talk_records[0]["build_fingerprint"]
    assert result["api_source_fingerprints"] == talk_records[2]["source_fingerprints"]


def test_api_start_producer_binds_source_bytes_to_talk_preflight(talk_records, tmp_path, monkeypatch):
    from apps import hexis_api

    monkeypatch.delenv("HEXIS_VOICE_BARGE_IN_PROVIDER_KEY_ENV", raising=False)
    monkeypatch.setenv("HEXIS_VOICE_BARGE_IN_LISTEN_HOST", urlparse(PROFILE["api_url"]).hostname)
    monkeypatch.setenv("HEXIS_VOICE_BARGE_IN_LISTEN_PORT", str(urlparse(PROFILE["api_url"]).port))
    monkeypatch.setenv("HEXIS_BROWSER_API_URL", PROFILE["api_url"].rstrip("/"))
    monkeypatch.setenv("HEXIS_UI_ORIGIN", "http://127.0.0.1:3482")
    monkeypatch.setenv("HEXIS_VOICE_BARGE_IN_UI_ORIGIN_SOURCE", "start_api_cli")
    record = hexis_api._write_voice_barge_in_api_start_record(
        tmp_path / "producer-api-start.json", talk_records[4],
        {"database": "hexis_memory", "server_address": "172.22.0.2/32", "server_port": 5432},
        migration_verification=MIGRATION_VERIFICATION,
    )
    assert record["source_fingerprints"] == probe._api_source_fingerprints(ROOT.parent)
    assert record["source_fingerprints"]["apps/hexis_api.py"] == probe.environment._sha256(ROOT.parent / "apps/hexis_api.py")
    api = talk_records[2]
    api.update(record)
    talk_records[4]["owned_resources"]["api_process"].update(pid=record["pid"], start_id=record["start_id"])
    talk_records[1]["api_start_id"] = record["start_id"]
    talk_records[1]["acceptance_session_id"] = record["acceptance_session_id"]
    talk_records[1]["runtime_identity"]["api"].update(
        start_id=record["start_id"], acceptance_session_id=record["acceptance_session_id"],
    )
    talk_records[1]["build_fingerprint"] = hashlib.sha256(json.dumps({
        "source_fingerprints": talk_records[1]["source_fingerprints"], "target": "talk-dev",
        "api_start_id": record["start_id"],
    }, sort_keys=True).encode()).hexdigest()
    assert validate_talk_records(*talk_records, PROFILE)["api_source_fingerprints"] == record["source_fingerprints"]


@pytest.mark.parametrize("relative", [
    "apps/hexis_api.py", "apps/voice_barge_in_probe.py", "apps/voice_barge_in_environment.py",
    "apps/voice_m4_probe.py", "apps/voice_realtime_harness.py",
    "services/voice_realtime_session.py", "services/voice_realtime_transport.py",
    "services/qwen_realtime_client.py", "services/qwen_realtime_output.py",
])
def test_talk_preflight_rejects_api_source_drift_with_same_head(talk_records, relative):
    talk_records[2]["source_fingerprints"][relative] = "b" * 64
    with pytest.raises(ValueError, match="API source fingerprints"):
        validate_talk_records(*talk_records, PROFILE)


@pytest.mark.parametrize("mutation", [
    lambda api: api.pop("source_fingerprints"),
    lambda api: api.update(source_fingerprints=None),
    lambda api: api["source_fingerprints"].pop("apps/hexis_api.py"),
    lambda api: api["source_fingerprints"].update({"services/voice_unrecorded.py": "b" * 64}),
])
def test_talk_preflight_rejects_missing_or_different_api_source_set(talk_records, mutation):
    mutation(talk_records[2])
    with pytest.raises(ValueError, match="API source fingerprints"):
        validate_talk_records(*talk_records, PROFILE)


def test_api_source_fingerprints_track_dirty_bytes_and_service_file_set(tmp_path):
    required = (
        "apps/hexis_api.py", "apps/voice_barge_in_probe.py", "apps/voice_barge_in_environment.py",
        "apps/voice_m4_probe.py", "apps/voice_realtime_harness.py",
        "services/m5_tool_bridge.py", "services/m5_tool_contracts.py", "services/contracts/m5-tools-v1.json",
        "core/agent_api.py", "core/migrations.py",
    )
    for relative in (*required, "services/voice_realtime_session.py"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"initial source")
    original = probe._api_source_fingerprints(tmp_path)
    (tmp_path / required[0]).write_bytes(b"uncommitted source")
    added = tmp_path / "services/qwen_realtime_new.py"
    added.write_bytes(b"new helper")
    current = probe._api_source_fingerprints(tmp_path)
    assert current[required[0]] != original[required[0]]
    assert current["services/qwen_realtime_new.py"] == hashlib.sha256(b"new helper").hexdigest()
    assert set(current) == set(original) | {"services/qwen_realtime_new.py"}


def test_api_source_fingerprints_require_fixed_entry_point_and_helpers(tmp_path):
    with pytest.raises(ValueError, match="selected source file"):
        probe._api_source_fingerprints(tmp_path)


def test_api_start_producer_rejects_a_different_checkout_before_writing(tmp_path):
    from apps import hexis_api

    output = tmp_path / "api-start.json"
    with pytest.raises(RuntimeError, match="source checkout"):
        hexis_api._write_voice_barge_in_api_start_record(output, environment_record(tmp_path), {})
    assert not output.exists()


@pytest.mark.parametrize("missing_index", range(4))
def test_talk_preflight_requires_all_records(talk_records, missing_index):
    records = list(talk_records)
    records[missing_index] = None
    with pytest.raises(ValueError, match="requires"):
        validate_talk_records(*records, PROFILE)


@pytest.mark.parametrize("index, path, value, message", [
    (0, ("kind",), "talk_ui_run", "Worker"),
    (0, ("mode",), "talk", "Worker"),
    (0, ("status",), "ready", "Worker"),
    (0, ("target",), "dev", "Worker"),
    (0, ("env_id",), "other", "environment identity"),
    (0, ("ready_at_utc",), None, "ready.*stopped"),
    (0, ("ready_at_utc",), "2026-10-08T00:00:00", "ready.*stopped"),
    (0, ("stopped_at_utc",), None, "ready.*stopped"),
    (0, ("stopped_at_utc",), "2026-10-07T00:00:00+00:00", "ready.*stopped"),
    (0, ("runtime_http_status",), 500, "Worker"),
    (0, ("build_fingerprint",), "b" * 64, "build fingerprint"),
    (0, ("source_fingerprints", PROFILE["detection_config"]), "b" * 64, "fingerprint"),
    (0, ("model_resource_fingerprint",), "b" * 64, "fingerprint"),
    (0, ("source_root",), "", "source checkout"),
    (0, ("source_fingerprints",), [], "fingerprints"),
    (0, ("container",), None, "build fingerprint"),
    (1, ("kind",), "worker_ui_run", "UI"),
    (1, ("status",), "starting", "UI"),
    (1, ("mode",), "worker", "UI"),
    (1, ("schema_version",), 0, "schema_version"),
    (1, ("env_id",), "other", "environment identity"),
    (1, ("api_start_id",), "stale", "start identity"),
    (1, ("api_start_record",), "/other/api-start.json", "start record"),
    (1, ("runtime_identity", "env_id"), "other", "runtime environment"),
    (1, ("runtime_identity", "database", "database"), "other", "database identity"),
    (1, ("runtime_identity", "database", "server_port"), 9999, "database identity"),
    (1, ("runtime_identity", "database", "url_port"), 43815, "database identity"),
    (1, ("runtime_identity", "database", "url_source"), "DATABASE_URL", "database identity"),
    (1, ("runtime_identity", "database", "url_host"), "localhost", "database identity"),
    (1, ("runtime_identity", "database", "server_address"), "172.23.0.2", "server address"),
    (1, ("runtime_identity", "api", "status"), "degraded", "API upstream"),
    (1, ("runtime_identity", "api", "checks", "db"), False, "API upstream"),
    (1, ("runtime_identity", "api", "url_host"), "localhost", "API upstream"),
    (1, ("runtime_identity", "api", "url_port"), 9999, "API upstream"),
    (1, ("runtime_identity",), None, "runtime identity"),
    (1, ("runtime_identity", "api", "checks"), None, "API upstream"),
    (1, ("build_fingerprint",), "b" * 64, "build fingerprint"),
    (1, ("source_fingerprints", PROFILE["detection_config"]), "b" * 64, "fingerprint"),
    (2, ("environment_id",), "other", "environment identity"),
    (2, ("status",), "starting", "startup/migration"),
    (2, ("start_id",), "stale", "start identity"),
    (2, ("pid",), 456, "process identity"),
    (2, ("actual_current_database",), "other", "database identity"),
    (2, ("actual_server_address_and_port", "port"), 9999, "database identity"),
    (2, ("actual_server_address_and_port", "address"), None, "server address"),
    (2, ("endpoints", "api_host"), "localhost", "endpoint"),
    (2, ("endpoints", "api_port"), 9999, "endpoint"),
    (2, ("endpoints", "health"), "/other", "endpoint"),
    (2, ("endpoints", "voice_realtime"), "/other", "endpoint"),
    (2, ("config_fingerprints",), None, "configuration fingerprints"),
    (2, ("config_fingerprints", "C1"), "b" * 64, "configuration fingerprint"),
    (2, ("config_fingerprints", "C6"), "b" * 64, "configuration fingerprint"),
    (2, ("source_clone", "agent_status_fingerprint"), "b" * 64, "clone identity"),
    (2, ("source_clone", "target_data_fingerprint"), "different", "clone identity"),
    (2, ("source", "repository_root"), "/other", "source checkout"),
    (2, ("source", "repository_root"), "", "source checkout"),
    (2, ("source", "head"), "b" * 40, "source checkout"),
    (3, ("samples",), [], "audio manifest"),
    (4, ("owned_resources", "api_process", "status"), "starting", "startup/migration"),
    (4, ("owned_resources", "api_process", "api_url"), "http://127.0.0.1:9999", "endpoint"),
    (4, ("source_clone", "source_agent_configured"), False, "agent identity or consent"),
    (4, ("source_clone", "source_consent_valid"), False, "agent identity or consent"),
    (4, ("source_clone", "source_agent_terminated"), True, "agent identity or consent"),
    (4, ("source_clone", "source_agent_status_fingerprint"), "b" * 64, "agent identity or consent"),
    (4, ("source_clone", "target_migration_count"), 0, "migration"),
    (4, ("source_clone", "target_migration_count"), -1, "migration"),
    (4, ("owned_resources", "api_process"), None, "API process"),
])
def test_talk_preflight_rejects_contract_mismatches(talk_records, index, path, value, message):
    item = talk_records[index]
    for key in path[:-1]:
        item = item[key]
    item[path[-1]] = value
    with pytest.raises(ValueError, match=message):
        validate_talk_records(*talk_records, PROFILE)


@pytest.mark.parametrize("label", ["C1", "C2", "C4", "C5", "C6", "C7", "C8"])
def test_talk_preflight_rejects_each_api_configuration_drift(talk_records, label):
    talk_records[2]["config_fingerprints"][label] = "b" * 64
    with pytest.raises(ValueError, match=f"configuration fingerprint {label}"):
        validate_talk_records(*talk_records, PROFILE)


@pytest.mark.parametrize("value", ["", "audio-hash", "a" * 63, "g" * 64, None])
def test_talk_preflight_rejects_invalid_audio_hash(talk_records, value):
    talk_records[3]["samples"][0]["file_sha256"] = value
    with pytest.raises(ValueError, match="audio manifest"):
        validate_talk_records(*talk_records, PROFILE)


def test_talk_preflight_rejects_changed_audio_bytes(talk_records):
    Path(talk_records[3]["samples"][0]["path"]).write_bytes(b"changed bytes")
    with pytest.raises(ValueError, match="audio sample hash"):
        validate_talk_records(*talk_records, PROFILE)


def test_talk_preflight_rejects_legacy_audio_sha256_field(talk_records):
    sample = talk_records[3]["samples"][0]
    sample["sha256"] = sample.pop("file_sha256")
    with pytest.raises(ValueError, match="file_sha256"):
        validate_talk_records(*talk_records, PROFILE)


def test_talk_preflight_rejects_missing_audio_path(talk_records):
    talk_records[3]["samples"][0]["path"] = "/missing/sample.wav"
    with pytest.raises(ValueError, match="sample path"):
        validate_talk_records(*talk_records, PROFILE)


def test_talk_phase_writes_once_after_validation(talk_records, tmp_path, monkeypatch):
    paths = []
    for index, record in enumerate(talk_records):
        path = Path(talk_records[4]["owned_resources"]["api_process"]["start_record_path"]) if index == 2 else tmp_path / f"record-{index}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        paths.append(path)
    output = tmp_path / "talk-preflight.json"
    writer = Mock(wraps=probe.environment._atomic_json_write)
    monkeypatch.setattr(probe.environment, "_atomic_json_write", writer)
    argv = ["--phase", "talk", "--profile", str(PROFILE_PATH), "--env-record", str(paths[4]),
            "--worker-run-record", str(paths[0]), "--ui-run-record", str(paths[1]),
            "--api-start-record", str(paths[2]), "--audio-manifest", str(paths[3]),
            "--evidence-dir", str(tmp_path), "--output-manifest", str(output)]
    assert probe.main(argv) == 0
    writer.assert_called_once()
    manifest = json.loads(output.read_text())
    assert manifest["kind"] == "c3"
    assert manifest["api_start_id"] == "start-1"
    assert set(manifest["configuration_fingerprints"]) == {"C1", "C2", "C4", "C5", "C6", "C7", "C8"}
    for label, path in zip(("E1", "W1", "U1", "API", "D1"), (paths[4], *paths[:4])):
        reference = manifest["records"][label]
        snapshot = Path(reference["path"])
        assert snapshot != path.resolve()
        assert snapshot.is_relative_to(tmp_path)
        assert reference["source_path"] == str(path.resolve())
        assert snapshot.read_bytes() == path.read_bytes()
        assert reference["sha256"] == probe.environment._sha256(snapshot)
    frozen_bytes = output.read_bytes()
    writer.reset_mock()
    assert probe.main(argv) == 1
    writer.assert_not_called()
    assert output.read_bytes() == frozen_bytes

    failure_output = tmp_path / "rejected-preflight.json"
    argv[-1] = str(failure_output)
    talk_records[1]["api_start_id"] = "stale"
    paths[1].write_text(json.dumps(talk_records[1]), encoding="utf-8")
    writer.reset_mock()
    assert probe.main(argv) == 1
    writer.assert_not_called()
    assert not failure_output.exists()


@pytest.mark.parametrize("stopped_label", ["E1", "U1"])
def test_talk_phase_frozen_inputs_survive_owned_record_stop(
    talk_records, tmp_path, monkeypatch, stopped_label,
):
    cache_root = tmp_path / "hexis"
    run_root = cache_root / "voice-barge-in"
    evidence_dir = run_root / "evidence" / "snapshot-run"
    input_paths = {}
    for label, document in zip(("W1", "U1", "API", "D1", "E1"), talk_records):
        source_path = (
            Path(talk_records[4]["owned_resources"]["api_process"]["start_record_path"])
            if label == "API" else run_root / "records" / f"{label}.json"
        )
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(json.dumps(document), encoding="utf-8")
        input_paths[label] = source_path
    output = run_root / "runs" / "c3.json"
    assert probe.main([
        "--phase", "talk", "--profile", str(PROFILE_PATH),
        "--env-record", str(input_paths["E1"]),
        "--worker-run-record", str(input_paths["W1"]),
        "--ui-run-record", str(input_paths["U1"]),
        "--api-start-record", str(input_paths["API"]),
        "--audio-manifest", str(input_paths["D1"]),
        "--evidence-dir", str(evidence_dir), "--output-manifest", str(output),
    ]) == 0
    c3 = json.loads(output.read_text())
    frozen = {label: Path(reference["path"]).read_bytes() for label, reference in c3["records"].items()}
    stopped = json.loads(input_paths[stopped_label].read_text())
    target = stopped["owned_resources"]["api_process"] if stopped_label == "E1" else stopped
    target.update(status="stopped", stopped_at_utc="2026-10-08T01:00:00+00:00")
    input_paths[stopped_label].write_text(json.dumps(stopped), encoding="utf-8")
    for label, reference in c3["records"].items():
        snapshot = Path(reference["path"])
        assert snapshot.read_bytes() == frozen[label]
        assert probe.environment._sha256(snapshot) == reference["sha256"]

    scenario = evidence_dir / "scenario.json"
    scenario.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(probe.environment, "cache_root_default", lambda: cache_root)
    verification_calls = []

    async def fake_verify(**kwargs):
        verification_calls.append(kwargs)
        assert kwargs["environment_record_path"] == Path(c3["records"]["E1"]["path"])
        _, record = probe.environment.load_owned_environment_record(kwargs["environment_record_path"])
        assert record["owned_resources"]["api_process"]["status"] == "healthy"
        return {"schema_version": 1, "kind": "talk_persistence_verification", "status": "verified", "env_id": "vb-unit"}

    monkeypatch.setattr(probe, "verify_talk_evidence", fake_verify)
    args = SimpleNamespace(
        output_manifest=evidence_dir / "verification.json", c3_record=output,
        scenario_record=scenario, env_record=input_paths["E1"], profile=PROFILE_PATH,
    )
    assert probe.run_talk_evidence_verification(args)["status"] == "verified"
    assert len(verification_calls) == 1

    # A mutation of the frozen copy must still fail before any database call.
    Path(c3["records"][stopped_label]["path"]).write_text(json.dumps(stopped), encoding="utf-8")
    args.output_manifest = evidence_dir / "rejected-verification.json"
    with pytest.raises(ValueError, match=f"C3 {stopped_label} record.*changed"):
        probe.run_talk_evidence_verification(args)
    assert len(verification_calls) == 1
    assert not args.output_manifest.exists()


def _persistence_inputs(tmp_path):
    from uuid import uuid4

    cache = tmp_path / "hexis" / "voice-barge-in"
    evidence_dir = cache / "evidence" / "c3-run"
    evidence_dir.mkdir(parents=True)
    session_id, prompt_id, interruption_id, capture_id, command_id = (str(uuid4()) for _ in range(5))
    media_session_id = "media-evidence"
    cloud_session_id = "cloud-evidence"
    reopened_media_session_id = "media-evidence-reopened"
    reopened_cloud_session_id = "cloud-evidence-reopened"
    capability = {"supported": True, "configuration_id": "c7-evidence", "maximum_receipt_prefix_items": 256}
    sample_rows = []
    sample_hashes = {}
    for sample_id, filename, content in (
        ("ordinary-tuning-01", "ordinary.wav", b"ordinary synthetic pcm"),
        ("pure-noise-tuning-01", "noise.wav", b"noise synthetic pcm"),
        ("short-stop-acceptance-01", "interrupt.wav", b"interrupt synthetic pcm"),
    ):
        sample_path = cache / "audio" / filename
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        sample_path.write_bytes(content)
        sample_hash = hashlib.sha256(sample_path.read_bytes()).hexdigest()
        sample_hashes[sample_id] = sample_hash
        sample_rows.append({"sample_id": sample_id, "path": str(sample_path), "file_sha256": sample_hash})
    audio_manifest = {"schema_version": 1, "samples": sample_rows}
    d1_path = cache / "audio" / "manifest.json"
    d1_path.write_text(json.dumps(audio_manifest), encoding="utf-8")
    records = {}
    for name in ("E1", "W1", "U1", "API"):
        record_path = cache / "records" / f"{name}.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        value = {"kind": name}
        if name in {"API", "U1"}:
            value.update({"session_execution_policy": PROFILE["session_execution_policy"], "acceptance_session_id": session_id, "migration_verification": MIGRATION_VERIFICATION})
        if name == "E1":
            value.update({
                "env_id": "vb-evidence",
                "db_identity": {"talk": {"database": "hexis_memory", "server_port": 5432}},
                "source_clone": {"target_migration_count": 266},
            })
        record_path.write_text(json.dumps(value), encoding="utf-8")
        records[name] = {"path": str(record_path), "sha256": hashlib.sha256(record_path.read_bytes()).hexdigest()}
    records["D1"] = {"path": str(d1_path), "sha256": hashlib.sha256(d1_path.read_bytes()).hexdigest()}
    c3 = {
        "schema_version": 1, "kind": "c3", "env_id": "vb-evidence",
        "database": "hexis_memory", "api_start_id": "api-start-evidence",
        "acceptance_session_id": session_id, "session_execution_policy": PROFILE["session_execution_policy"],
        "migration_verification": MIGRATION_VERIFICATION,
        "evidence_dir": str(evidence_dir), "records": records,
    }
    c3_path = cache / "runs" / "c3.json"
    c3_path.parent.mkdir(parents=True)
    c3_path.write_text(json.dumps(c3), encoding="utf-8")
    receipt_prefix = [
        {"action": "open", "session_id": session_id, "media_session_id": media_session_id,
         "response_id": "response-target", "playback_id": "playback-target", "producer_seq": 1,
         "output_epoch": 0, "start_ms": 0},
        {"action": "audible", "session_id": session_id, "media_session_id": media_session_id,
         "response_id": "response-target", "playback_id": "playback-target", "producer_seq": 2,
         "output_epoch": 0, "audible_ms": 10},
        {"action": "interrupt", "session_id": session_id, "media_session_id": media_session_id,
         "response_id": "response-target", "playback_id": "playback-target", "producer_seq": 3,
         "output_epoch": 0, "played_ms": 20, "reason": "speech_start"},
    ]
    observation = {
        "schema_version": 3, "kind": "m6_talk_barge_in_observation",
        "c3_path": str(c3_path), "c3_sha256": hashlib.sha256(c3_path.read_bytes()).hexdigest(),
        "env_id": "vb-evidence", "api_start_id": "api-start-evidence",
        "acceptance_session_id": session_id, "session_execution_policy": PROFILE["session_execution_policy"],
        "migration_verification": MIGRATION_VERIFICATION,
        "session_id": session_id,
        "media_session_id": media_session_id, "cloud_session_id": cloud_session_id,
        "capture_instance_id": capture_id,
        "session_response": {"session_id": session_id, "media_session_id": media_session_id,
                              "barge_in_capability": dict(capability)},
        "ready": {"type": "ready", "session_id": session_id, "media_session_id": media_session_id,
                  "cloud_session_id": cloud_session_id, "barge_in_capability": dict(capability)},
        "capture_open": {"type": "capture_open", "session_id": session_id,
                         "media_session_id": media_session_id, "capture_instance_id": capture_id},
        "prompt_input_turn_id": prompt_id, "prompt_stimulus_revision": 0,
        "interruption_input_turn_id": interruption_id, "interruption_stimulus_revision": 0,
        "target_response_id": "response-target", "target_playback_id": "playback-target", "target_output_epoch": 0,
        "target_input_turn_id": prompt_id,
        "target_identity": {"session_id": session_id, "media_session_id": media_session_id,
                            "response_id": "response-target", "playback_id": "playback-target", "output_epoch": 0,
                            "input_turn_id": prompt_id, "stimulus_revision": 0},
        "target_model_call_started": {"type": "event", "event": {
            "event_type": "voice_model_call_started",
            "payload": {"identity": {"session_id": session_id, "input_turn_id": prompt_id, "stimulus_revision": 0},
                        "response_id": "response-target", "playback_id": "playback-target", "output_epoch": 0}}},
        "next_model_call_started": {"type": "event", "event": {
            "event_type": "voice_model_call_started",
            "payload": {"identity": {"session_id": session_id, "input_turn_id": interruption_id, "stimulus_revision": 0},
                        "response_id": "response-next", "playback_id": "playback-next", "output_epoch": 1}}},
        "prompt_input_committed": {"type": "event", "event": {
            "event_type": "voice_input_committed",
            "payload": {"identity": {"session_id": session_id, "input_turn_id": prompt_id,
                                      "stimulus_revision": 0}}}},
        "interruption_input_committed": {"type": "event", "event": {
            "event_type": "voice_input_committed",
            "payload": {"identity": {"session_id": session_id, "input_turn_id": interruption_id,
                                      "stimulus_revision": 0}}}},
        "target_was_audible": True,
        "command": {"type": "barge_in_confirmed", "command_id": command_id,
                    "session_id": session_id, "media_session_id": media_session_id,
                    "capture_instance_id": capture_id, "response_id": "response-target",
                    "playback_id": "playback-target", "output_epoch": 0,
                    "local_playback_state": "started", "receipt_prefix": receipt_prefix},
        "command_result": {"type": "barge_in_result", "command_id": command_id,
                           "session_id": session_id, "media_session_id": media_session_id,
                           "capture_instance_id": capture_id, "response_id": "response-target",
                           "playback_id": "playback-target", "output_epoch": 0, "status": "accepted"},
        "no_barge_commands_during_noise": True,
        "committed_input_count": 2,
        "next_response_id": "response-next", "next_playback_id": "playback-next", "next_output_epoch": 1,
        "next_identity": {"session_id": session_id, "media_session_id": media_session_id,
                          "response_id": "response-next", "playback_id": "playback-next",
                          "output_epoch": 1, "input_turn_id": interruption_id, "stimulus_revision": 0},
        "next_response_end_receipt_persisted": True,
        "next_response_terminal_http_status": 200,
        "next_response_terminal_receipt": {
            "action": "end", "scope": "complete", "response_id": "response-next",
            "playback_id": "playback-next", "output_epoch": 1,
        },
        "media_reopen_sessions": [{
            "session_response": {"session_id": session_id, "media_session_id": reopened_media_session_id,
                                  "barge_in_capability": dict(capability)},
            "ready": {"type": "ready", "session_id": session_id,
                      "media_session_id": reopened_media_session_id, "cloud_session_id": reopened_cloud_session_id,
                      "barge_in_capability": dict(capability)},
            "capture_open": {"type": "capture_open", "session_id": session_id,
                             "media_session_id": reopened_media_session_id, "capture_instance_id": str(uuid4())},
            "stop_completed": True,
        }],
        "sample_ids": ["ordinary-tuning-01", "pure-noise-tuning-01", "short-stop-acceptance-01"],
        "prompt_sha256": sample_hashes["ordinary-tuning-01"],
        "noise_sha256": sample_hashes["pure-noise-tuning-01"],
        "interruption_sha256": sample_hashes["short-stop-acceptance-01"],
    }
    observation_path = evidence_dir / "talk-observation.json"
    observation_path.write_text(json.dumps(observation), encoding="utf-8")
    database_readback = {
        "database": "hexis_memory", "server_port": 5432, "migration_count": 266,
        "active_realtime_session_count": 0, "matched_realtime_session_count": 1,
        "matched_realtime_session_status": "closed", "matched_realtime_session_closed_at": "2026-10-08T00:00:00Z",
        "prompt_turn": {
            "completion_count": 1, "playback_scope": "partial", "turn_status": "completed",
            "user_message_count": 1, "assistant_message_count": 0,
            "input_committed_event_count": 1, "turn_completed_event_count": 1,
            "completion_metadata": {"final_response_id": "response-target", "final_playback_id": "playback-target", "final_output_epoch": 0},
        },
        "interruption_turn": {
            "completion_count": 1, "playback_scope": "complete", "turn_status": "completed",
            "user_message_count": 1, "assistant_message_count": 1,
            "input_committed_event_count": 1, "turn_completed_event_count": 1,
            "completion_metadata": {"final_response_id": "response-next", "final_playback_id": "playback-next", "final_output_epoch": 1},
        },
        "target_playback_interrupted_count": 1,
        "target_playback_audible_count": 1,
        "next_playback_audible_count": 1,
        "next_playback_end_count": 1,
        "confirmed_receipt_prefix": [
            {"action": item["action"], "producer_seq": item["producer_seq"], "persisted_once": True}
            for item in receipt_prefix
        ],
        "media_reopen_session_facts": [{
            "row_count": 1, "status": "closed", "closed_at": "2026-10-08T00:00:00Z",
        }],
    }
    return c3, c3_path, observation, database_readback


def test_post_talk_evidence_requires_unique_persistence_and_matching_playback(tmp_path):
    c3, c3_path, observation, database_readback = _persistence_inputs(tmp_path)
    result = build_talk_evidence_verification(c3, c3_path, observation, database_readback)
    assert result["kind"] == "talk_persistence_verification"
    assert result["status"] == "verified"
    assert result["prompt_turn"]["playback_scope"] == "partial"
    assert result["interruption_turn"]["playback_scope"] == "complete"
    assert result["barge_in_result"] == "accepted"
    assert result["barge_in_evidence_scope"] == "active_generation_cancel_command"
    assert result["provider_cancel_terminal_verified"] is False
    assert result["media_reopen_sessions_verified_count"] == 1


def test_playback_only_verifies_generation_completed_before_exact_interrupt(tmp_path):
    c3, c3_path, observation, database_readback = _persistence_inputs(tmp_path)
    observation["command_result"]["status"] = "playback_only"
    database_readback["target_generation_before_interrupt"] = {
        "completed_model_end_count": 1,
        "interrupt_count": 1,
        "model_ended_at": "2026-10-08T00:00:01+00:00",
        "interrupted_at": "2026-10-08T00:00:02+00:00",
    }
    result = build_talk_evidence_verification(c3, c3_path, observation, database_readback)
    assert result["barge_in_result"] == "playback_only"
    assert result["barge_in_evidence_scope"] == "local_playback_interrupt_after_generation"
    assert result["provider_cancel_terminal_verified"] is False
    assert result["target_generation_before_interrupt"] == database_readback["target_generation_before_interrupt"]


@pytest.mark.parametrize("facts", [
    None,
    {"completed_model_end_count": 0, "interrupt_count": 1, "model_ended_at": None,
     "interrupted_at": "2026-10-08T00:00:02+00:00"},
    {"completed_model_end_count": 2, "interrupt_count": 1,
     "model_ended_at": "2026-10-08T00:00:01+00:00", "interrupted_at": "2026-10-08T00:00:02+00:00"},
    {"completed_model_end_count": 1, "interrupt_count": 0,
     "model_ended_at": "2026-10-08T00:00:01+00:00", "interrupted_at": None},
    {"completed_model_end_count": 1, "interrupt_count": 1,
     "model_ended_at": "2026-10-08T00:00:03+00:00", "interrupted_at": "2026-10-08T00:00:02+00:00"},
    {"completed_model_end_count": 1, "interrupt_count": 1,
     "model_ended_at": "2026-10-08T00:00:02+00:00", "interrupted_at": "2026-10-08T00:00:02+00:00"},
    {"completed_model_end_count": 1, "interrupt_count": 1,
     "model_ended_at": "invalid", "interrupted_at": "2026-10-08T00:00:02+00:00"},
    {"completed_model_end_count": 1, "interrupt_count": 1,
     "model_ended_at": "2026-10-08T00:00:01", "interrupted_at": "2026-10-08T00:00:02+00:00"},
])
def test_playback_only_rejects_missing_or_unproven_generation_order(facts, tmp_path):
    c3, c3_path, observation, database_readback = _persistence_inputs(tmp_path)
    observation["command_result"]["status"] = "playback_only"
    database_readback["target_generation_before_interrupt"] = facts
    with pytest.raises(ValueError, match="playback_only requires"):
        build_talk_evidence_verification(c3, c3_path, observation, database_readback)


@pytest.mark.parametrize("barge_status", ["accepted", "playback_only"])
@pytest.mark.parametrize("receipt_turn, database_turn_id, expected_match", [
    ({}, None, True),
    ({"turn_id": None}, None, True),
    ({}, UUID("00000000-0000-0000-0000-000000000001"), False),
    ({"turn_id": None}, UUID("00000000-0000-0000-0000-000000000001"), False),
    ({"turn_id": "00000000-0000-0000-0000-000000000001"}, None, False),
    ({"turn_id": "00000000-0000-0000-0000-000000000001"}, UUID("00000000-0000-0000-0000-000000000001"), True),
    ({"turn_id": "00000000-0000-0000-0000-000000000001"}, UUID("00000000-0000-0000-0000-000000000002"), False),
])
def test_post_talk_database_readback_uses_only_readonly_queries(
    monkeypatch, tmp_path, receipt_turn, database_turn_id, expected_match, barge_status,
):
    import asyncpg

    c3, _c3_path, observation, _database_readback = _persistence_inputs(tmp_path)
    observation["command_result"]["status"] = barge_status
    for receipt in observation["command"]["receipt_prefix"]:
        receipt.update(receipt_turn)
    queries = []
    readonly_transactions = []

    class ReadOnlyTransaction:
        def __init__(self, *, readonly):
            readonly_transactions.append(readonly)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class FakeConnection:
        closed = False

        def transaction(self, *, readonly=False):
            return ReadOnlyTransaction(readonly=readonly)

        async def fetchrow(self, query, *args):
            queries.append(query)
            if "current_database()" in query:
                return {"database": "hexis_memory", "server_port": 5432}
            if "voice_realtime_sessions" in query:
                return {"row_count": 1, "status": "closed", "closed_at": "2026-10-08T00:00:00Z"}
            if "voice_turn_completions" in query:
                prompt = str(args[1]) == observation["prompt_input_turn_id"]
                return {
                    "completion_count": 1,
                    "playback_scope": "partial" if prompt else "complete",
                    "turn_status": "completed",
                    "completion_metadata": [json.dumps({
                        "final_response_id": "response-target" if prompt else "response-next",
                        "final_playback_id": "playback-target" if prompt else "playback-next",
                        "final_output_epoch": 0 if prompt else 1,
                    })],
                }
            if "voice_input_committed" in query:
                return {"input_committed_event_count": 1, "turn_completed_event_count": 1}
            if "completed_model_end_count" in query:
                assert args == (UUID(observation["session_id"]), observation["media_session_id"],
                                observation["target_response_id"], observation["target_output_epoch"],
                                observation["target_playback_id"], observation["prompt_input_turn_id"],
                                observation["prompt_stimulus_revision"])
                assert "payload->>'reason'='completed'" in query
                assert "session_id=$1 AND media_session_id=$2 AND response_id=$3" in query
                assert "output_epoch=$4 AND payload->>'playback_id'=$5" in query
                assert "input_turn_id=$6" in query and "stimulus_revision=$7" in query
                from datetime import datetime, timezone
                return {"completed_model_end_count": 1, "interrupt_count": 1,
                        "model_ended_at": datetime(2026, 10, 8, 0, 0, 1, tzinfo=timezone.utc),
                        "interrupted_at": datetime(2026, 10, 8, 0, 0, 2, tzinfo=timezone.utc)}
            raise AssertionError(f"unexpected SQL query: {query}")

        async def fetch(self, query, *args):
            queries.append(query)
            if "FROM behavior_events" in query and "producer_seq" in query:
                return [{"payload": json.dumps({"start_ms": 0, "audible_ms": 10, "played_ms": 20, "reason": "speech_start"}), "turn_id": database_turn_id}]
            prompt = str(args[1][0]).find(observation["prompt_input_turn_id"]) >= 0
            if prompt:
                return [{"role": "user", "count": 1}]
            return [{"role": "user", "count": 1}, {"role": "assistant", "count": 1}]

        async def fetchval(self, query, *args):
            queries.append(query)
            if "schema_migrations" in query:
                return 266
            if "status='active'" in query:
                return 0
            if args and args[2] in {"voice_first_audible", "voice_playback_interrupted", "voice_playback_ended"}:
                return 1
            raise AssertionError(f"unexpected SQL query: {query}")

        async def close(self):
            self.closed = True

    connection = FakeConnection()
    async def fake_connect(dsn, **kwargs):
        assert dsn == "postgresql://ephemeral"
        assert kwargs["timeout"] == 10
        return connection

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    monkeypatch.setattr(
        probe.environment, "runtime_dsn_from_environment_record",
        lambda *_args: ("postgresql://ephemeral", {"env_id": c3["env_id"]}),
    )
    actual = asyncio.run(probe._read_talk_database_evidence(
        Path("/cache/e1.json"), c3, observation, {"POSTGRES_USER": "role"}
    ))
    assert actual["database"] == "hexis_memory"
    assert actual["prompt_turn"]["playback_scope"] == "partial"
    assert actual["interruption_turn"]["playback_scope"] == "complete"
    assert actual["target_playback_interrupted_count"] == 1
    if barge_status == "playback_only":
        assert actual["target_generation_before_interrupt"]["model_ended_at"] == "2026-10-08T00:00:01+00:00"
        assert actual["target_generation_before_interrupt"]["completed_model_end_count"] == 1
    assert all(fact["persisted_once"] is expected_match for fact in actual["confirmed_receipt_prefix"])
    assert readonly_transactions == [True]
    assert connection.closed is True
    assert queries and all(query.lstrip().upper().startswith("SELECT") for query in queries)


@pytest.mark.parametrize("mutation, expected_error", [
    (lambda observation, db: db["interruption_turn"].update(completion_count=2), "exactly once"),
    (lambda observation, db: db.update(target_playback_interrupted_count=0), "target playback"),
    (lambda observation, db: db.update(active_realtime_session_count=1), "selected cloud session"),
    (lambda observation, db: observation["command_result"].update(status="unknown"), "accepted or playback_only"),
    (lambda observation, db: observation["command_result"].update(status="stale_target"), "accepted or playback_only"),
    (lambda observation, db: observation.update(no_barge_commands_during_noise=False), "noise interruption"),
    (lambda observation, db: observation["command_result"].update(capture_instance_id="another-capture"), "exact confirmed command identity"),
    (lambda observation, db: db["confirmed_receipt_prefix"][1].update(persisted_once=False), "prefix is not persisted exactly once"),
    (lambda observation, db: db.update(matched_realtime_session_count=0), "selected cloud session"),
    (lambda observation, db: db["prompt_turn"]["completion_metadata"].update(final_response_id="another-response"), "completion metadata"),
    (lambda observation, db: observation["ready"]["barge_in_capability"].update(configuration_id="drifted"), "REST session and server ready"),
    (lambda observation, db: db["media_reopen_session_facts"][0].update(status="active"), "reopened media sessions"),
    (lambda observation, db: observation["media_reopen_sessions"][0].update(stop_completed=False), "media reopen 0"),
])
def test_post_talk_evidence_rejects_ambiguous_or_incomplete_history(mutation, expected_error, tmp_path):
    c3, c3_path, observation, database_readback = _persistence_inputs(tmp_path)
    mutation(observation, database_readback)
    with pytest.raises(ValueError, match=expected_error):
        build_talk_evidence_verification(c3, c3_path, observation, database_readback)


def test_verify_cli_binds_c3_scenario_and_output_without_external_calls(talk_records, tmp_path, monkeypatch):
    cache_home = tmp_path / "cache-home"
    cache_root = cache_home / "hexis"
    run_root = cache_root / "voice-barge-in"
    evidence_dir = run_root / "evidence" / "verify-run"
    evidence_dir.mkdir(parents=True)
    worker, ui, api, audio, e1 = talk_records
    api_path = run_root / "api-runs" / "api-start.json"
    api_path.parent.mkdir(parents=True)
    api_path.write_text(json.dumps(api), encoding="utf-8")
    e1["owned_resources"]["api_process"]["start_record_path"] = str(api_path)
    ui["api_start_record"] = str(api_path)
    record_values = {"E1": e1, "W1": worker, "U1": ui, "API": api, "D1": audio}
    record_paths = {}
    for label, record in record_values.items():
        if label == "API":
            record_path = api_path
        else:
            record_path = run_root / "records" / f"{label}.json"
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_text(json.dumps(record), encoding="utf-8")
        record_paths[label] = {"path": str(record_path), "sha256": hashlib.sha256(record_path.read_bytes()).hexdigest()}
    c3 = {
        "schema_version": 1,
        "kind": "c3",
        "env_id": e1["env_id"],
        "database": e1["owned_resources"]["talk_database"],
        "api_url": PROFILE["api_url"],
        "api_start_id": api["start_id"],
        "session_execution_policy": PROFILE["session_execution_policy"],
        "acceptance_session_id": api["acceptance_session_id"],
        "migration_verification": MIGRATION_VERIFICATION,
        "worker_build_fingerprint": worker["build_fingerprint"],
        "ui_build_fingerprint": ui["build_fingerprint"],
        "configuration_fingerprints": validate_talk_records(worker, ui, api, audio, e1, PROFILE)["configuration_fingerprints"],
        "evidence_dir": str(evidence_dir),
        "records": record_paths,
    }
    c3_path = run_root / "runs" / "c3.json"
    c3_path.parent.mkdir(parents=True)
    c3_path.write_text(json.dumps(c3), encoding="utf-8")
    observation = {
        "schema_version": 1, "kind": "m6_talk_barge_in_observation",
        "c3_path": str(c3_path), "c3_sha256": hashlib.sha256(c3_path.read_bytes()).hexdigest(),
    }
    scenario_path = evidence_dir / "scenario.json"
    scenario_path.write_text(json.dumps(observation), encoding="utf-8")
    monkeypatch.setattr(probe.environment, "cache_root_default", lambda: cache_root)

    async def fake_verify(**_kwargs):
        return {"schema_version": 1, "kind": "talk_persistence_verification", "status": "verified", "env_id": e1["env_id"]}

    monkeypatch.setattr(probe, "verify_talk_evidence", fake_verify)
    output = evidence_dir / "verification.json"
    args = SimpleNamespace(
        output_manifest=output,
        c3_record=c3_path,
        scenario_record=scenario_path,
        env_record=Path(record_paths["E1"]["path"]),
        profile=PROFILE_PATH,
    )
    result = probe.run_talk_evidence_verification(args)
    assert result["status"] == "verified"
    assert json.loads(output.read_text())["scenario_record"] == str(scenario_path)
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        probe.run_talk_evidence_verification(args)
