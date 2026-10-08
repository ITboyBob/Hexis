"""Preflight the explicitly selected M6 voice barge-in environment."""

from __future__ import annotations

import asyncio
import argparse
import hashlib
import ipaddress
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

from apps import voice_barge_in_environment as environment


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _api_source_fingerprints(repository_root: Path) -> dict[str, str]:
    """Bind the API entry point and selected Talk helpers, including dirty files."""
    root = repository_root.resolve()
    files = {root / relative for relative in (
        "apps/hexis_api.py", "apps/voice_barge_in_probe.py", "apps/voice_barge_in_environment.py",
        "apps/voice_m4_probe.py", "apps/voice_realtime_harness.py",
        "services/m5_tool_bridge.py", "services/m5_tool_contracts.py", "services/contracts/m5-tools-v1.json",
        "core/agent_api.py", "core/migrations.py",
    )}
    for pattern in ("services/voice*.py", "services/qwen_realtime*.py"):
        files.update(root.glob(pattern))
    fingerprints = {}
    for path in sorted(files):
        relative = str(path.relative_to(root))
        if not path.is_file():
            raise ValueError(f"API source fingerprints require the selected source file: {relative}")
        fingerprints[relative] = environment._sha256(path)
    return fingerprints


def _profile_fingerprint(profile: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _read_json(path: Path | str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _database_json_mapping(value: Any, label: str) -> dict[str, Any]:
    """Decode asyncpg's default JSON/JSONB text values before validating shape."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} contains invalid JSON") from exc
    return _mapping(value, label)


def _freeze_talk_input_records(
    paths: Sequence[Path],
    documents: Sequence[dict[str, Any]],
    evidence_dir: Path,
) -> dict[str, dict[str, str]]:
    """Preserve validated bytes independently of the owned services' stop records."""
    labels = ("E1", "W1", "U1", "API", "D1")
    inputs = []
    for label, path, document in zip(labels, paths, documents):
        payload = path.read_bytes()
        if json.loads(payload) != document:
            raise ValueError(f"{label} record changed while creating C3; repeat preflight")
        inputs.append((label, path.resolve(), payload))
    # Keep E1's owned layout so the existing read-only DSN loader can consume the
    # frozen record without opening the mutable service-lifecycle source again.
    snapshot_root = evidence_dir.resolve() / "inputs" / uuid.uuid4().hex / "voice-barge-in"
    snapshot_root.mkdir(parents=True, mode=0o700)
    references = {}
    for label, source_path, payload in inputs:
        snapshot = (
            snapshot_root / "environments" / documents[0]["env_id"] / "environment.json"
            if label == "E1" else snapshot_root / f"{label}.json"
        )
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(snapshot, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
        references[label] = {
            "path": str(snapshot), "sha256": hashlib.sha256(payload).hexdigest(),
            "source_path": str(source_path),
        }
    return references


def build_environment_preflight(
    profile: dict[str, Any],
    record: dict[str, Any],
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, Any]:
    """Validate frozen C6/E1 inputs; live database probes populate databases later."""
    try:
        environment.validate_profile(profile)
    except ValueError as exc:
        raise ValueError(f"invalid run profile: {exc}") from exc

    env_id = record.get("env_id")
    try:
        environment.validate_environment_id(env_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid E1 environment identity: {exc}") from exc
    root = repository_root.resolve()
    if record.get("schema_version") != 1:
        raise ValueError("E1 schema_version must be 1")
    if Path(str(record.get("repository_root", ""))).resolve() != root:
        raise ValueError("E1 repository root does not match this checkout")
    if record.get("status") != "ready":
        raise ValueError("E1 must have both isolated database scopes verified")
    if record.get("profile_fingerprint") != _profile_fingerprint(profile):
        raise ValueError("E1 profile fingerprint does not match the selected C6 profile")

    owned = record.get("owned_resources")
    if not isinstance(owned, dict) or owned.get("project_name") != environment.derive_project_name(env_id):
        raise ValueError("E1 project identity does not match the selected environment")
    if owned.get("pytest_service") != profile["database"]["pytest_service"]:
        raise ValueError("E1 pytest service does not match the selected profile")
    if owned.get("talk_service") != profile["database"]["talk_service"]:
        raise ValueError("E1 talk service does not match the selected profile")
    if owned.get("pytest_database") != "postgres":
        raise ValueError("E1 pytest database must remain the isolated postgres database")
    if owned.get("talk_database") != "hexis_memory":
        raise ValueError("E1 talk database does not identify the full Hexis clone")
    if owned.get("pytest_host_port") == owned.get("talk_host_port"):
        raise ValueError("E1 pytest and talk database ports must be separate")
    volumes = owned.get("volumes")
    if not isinstance(volumes, list) or len(volumes) != 2 or len(set(volumes)) != 2:
        raise ValueError("E1 must own separate pytest and talk database volumes")

    identities = record.get("db_identity")
    for scope, database_key, port_key in (
        ("pytest", "pytest_database", "pytest_host_port"),
        ("talk", "talk_database", "talk_host_port"),
    ):
        identity = identities.get(scope) if isinstance(identities, dict) else None
        if not isinstance(identity, dict):
            raise ValueError(f"E1 is missing the {scope} database identity")
        if identity.get("database") != owned[database_key] or identity.get("host_port") != owned[port_key]:
            raise ValueError(f"E1 {scope} database identity does not match its owned resource")

    clone = record.get("source_clone")
    if not isinstance(clone, dict) or clone.get("status") != "verified":
        raise ValueError("E1 source database clone is not verified")
    if clone.get("recovery_verification") != "full_table_content_hash" or not clone.get("source_data_fingerprint"):
        raise ValueError("E1 clone lacks full source data verification")
    if clone.get("source_data_fingerprint") != clone.get("target_data_fingerprint"):
        raise ValueError("E1 source and target content fingerprints differ")
    if clone.get("agent_status_matches") is not True:
        raise ValueError("E1 cloned agent/consent identity does not match its source")

    configs: dict[str, str] = {}
    for field in (
        "realtime_config", "runtime_config", "detection_config", "asset_manifest",
        "playback_coordination_config", "audio_case_spec",
    ):
        relative = profile[field]
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f"missing configuration or resource file selected by C6: {relative}")
        configs[relative] = environment._sha256(path)

    return {
        "schema_version": 1,
        "kind": "environment_preflight",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "env_id": env_id,
        "repository_root": str(root),
        "profile_fingerprint": _profile_fingerprint(profile),
        "owned_resources": {
            "project_name": owned["project_name"],
            "pytest_database": owned["pytest_database"],
            "pytest_host_port": owned["pytest_host_port"],
            "talk_database": owned["talk_database"],
            "talk_host_port": owned["talk_host_port"],
            "volumes": list(volumes),
        },
        "databases": {},
        "configuration_fingerprints": configs,
        "source_clone_verified": True,
        "agent_identity_cloned": True,
    }


def _probe_database_scope(env_id: str, record: dict[str, Any], scope: str) -> dict[str, Any]:
    owned = record["owned_resources"]
    database_key = "pytest_database" if scope == "pytest" else "talk_database"
    service_key = "pytest_service" if scope == "pytest" else "talk_service"
    database = owned[database_key]
    identity = record["db_identity"][scope]
    compose_environment = environment._compose_environment(env_id, record)
    user = compose_environment["POSTGRES_USER"]
    base_command = environment.build_compose_command(
        env_id, "exec", "--no-TTY", owned[service_key], "psql", "-X", "-A", "-t", "-F", "\t", "-U", user, "-d", database, "-c"
    )
    actual = environment._run(
        [*base_command, "SELECT current_database(), current_setting('port')::int, current_user"],
        environment=compose_environment,
    ).stdout.strip().split("\t")
    if len(actual) != 3 or actual[0] != database or int(actual[1]) != identity.get("server_port") or actual[2] != identity.get("role"):
        raise ValueError(f"live {scope} database identity does not match E1")
    required = sorted(environment.REQUIRED_DATABASE_EXTENSIONS - {"pgtap"})
    quoted = ",".join("'" + item.replace("'", "''") + "'" for item in required)
    extensions = environment._run(
        [*base_command, f"SELECT COALESCE(string_agg(extname, ',' ORDER BY extname), '') FROM pg_extension WHERE extname IN ({quoted})"],
        environment=compose_environment,
    ).stdout.strip().split(",")
    active = sorted(item for item in extensions if item)
    if active != required:
        raise ValueError(f"live {scope} database is missing required extensions")
    migrations = 0
    if scope == "talk":
        result = environment._run(
            [*base_command, "SELECT count(*) FROM schema_migrations"], environment=compose_environment
        ).stdout.strip()
        migrations = int(result)
        expected = int(record.get("source_clone", {}).get("target_migration_count", 0))
        if migrations <= 0 or migrations != expected:
            raise ValueError("live talk database migration history does not match the verified clone")
    return {
        "database": database,
        "server_port": int(actual[1]),
        "role": actual[2],
        "host_port": identity["host_port"],
        "extensions": active,
        "migration_count": migrations,
    }


def validate_talk_records(
    worker: dict[str, Any] | None,
    ui: dict[str, Any] | None,
    api: dict[str, Any] | None,
    audio: dict[str, Any] | None,
    environment_record: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    named = (("worker", worker), ("UI", ui), ("API", api), ("audio manifest", audio))
    for label, value in named:
        if not isinstance(value, dict):
            raise ValueError(f"talk preflight requires the {label} record")
    assert worker is not None and ui is not None and api is not None and audio is not None
    if any(value.get("schema_version") != 1 for _, value in named):
        raise ValueError("Worker/UI/API/audio records must use schema_version 1")
    preflight = build_environment_preflight(profile, environment_record)
    if (worker.get("kind") != "worker_ui_run" or worker.get("mode") != "worker"
            or worker.get("target") != "standalone" or worker.get("status") != "stopped"
            or worker.get("runtime_http_status") != 200):
        raise ValueError("Worker build record must identify a stopped standalone Worker run after readiness")
    try:
        ready = datetime.fromisoformat(worker["ready_at_utc"])
        stopped = datetime.fromisoformat(worker["stopped_at_utc"])
        if ready.utcoffset() is None or stopped.utcoffset() is None or stopped < ready:
            raise ValueError("invalid Worker lifecycle order")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Worker must have verified ready and stopped timestamps in order") from exc
    if ui.get("kind") != "talk_ui_run" or ui.get("status") != "ready" or ui.get("mode") != "talk":
        raise ValueError("UI record must identify the ready real Talk build")
    env_id = environment_record.get("env_id")
    if any(value.get("env_id") != env_id for value in (worker, ui)) or api.get("environment_id") != env_id:
        raise ValueError("Worker/Talk UI/API environment identity does not match E1")
    owned = environment_record["owned_resources"]
    expected_database = owned["talk_database"]
    expected_api_url = profile["api_url"].rstrip("/")
    api_url = urlparse(expected_api_url)
    api_port = api_url.port or (443 if api_url.scheme == "https" else 80)
    process = _mapping(owned.get("api_process"), "E1 API process identity")
    clone = environment_record["source_clone"]
    # The API producer writes status=started only after its selected-run migration
    # check succeeds. E1 records healthy only after the matching start_id is read
    # back from /health; no invented migration_status field is involved.
    migration_count = clone.get("target_migration_count")
    if (api.get("status") != "started" or process.get("status") != "healthy"
            or type(migration_count) is not int or migration_count <= 0):
        raise ValueError("API startup/migration contract is not completed")
    if not api.get("start_id") or api["start_id"] != process.get("start_id") or ui.get("api_start_id") != api["start_id"]:
        raise ValueError("Talk UI/API start identity does not match the E1 owned process")
    if not api.get("pid") or api["pid"] != process.get("pid"):
        raise ValueError("API process identity does not match E1")
    start_path = process.get("start_record_path")
    if not isinstance(start_path, str) or not isinstance(ui.get("api_start_record"), str) or Path(ui["api_start_record"]).resolve() != Path(start_path).resolve():
        raise ValueError("Talk UI API start record does not match E1")
    endpoints = _mapping(api.get("endpoints"), "API endpoints")
    if (process.get("api_url", "").rstrip("/") != expected_api_url
            or endpoints.get("api_host") != api_url.hostname or endpoints.get("api_port") != api_port
            or endpoints.get("health") != "/health" or endpoints.get("voice_realtime") != "/api/voice/realtime"):
        raise ValueError("API endpoint does not match the selected profile")
    if api.get("browser_api_url") != expected_api_url or ui.get("browser_api_url") != expected_api_url:
        raise ValueError("API/Talk UI browser-reachable API URL does not match selected C6")
    selected_ui_url = urlparse(str(ui.get("ui_url", "")))
    ui_origin = environment.selected_ui_origin(profile, f"{selected_ui_url.scheme}://{selected_ui_url.netloc}")
    if (api.get("ui_origin") != ui_origin or ui.get("ui_origin") != ui_origin
            or api.get("ui_origin_source") not in {"C6_ui_url", "start_api_cli"}):
        raise ValueError("API/Talk UI origin does not match the actual owned UI URL")
    source = _mapping(api.get("source"), "API source checkout")
    root = Path(environment_record["repository_root"]).resolve()
    if (not isinstance(source.get("repository_root"), str) or not source["repository_root"]
            or Path(source["repository_root"]).resolve() != root
            or source.get("head") != environment_record.get("head")
            or any(not isinstance(value.get("source_root"), str) or not value["source_root"]
                   or Path(value["source_root"]).resolve() != root for value in (worker, ui))):
        raise ValueError("Worker/Talk UI/API source checkout does not match E1")
    api_source_fingerprints = _mapping(api.get("source_fingerprints"), "API source fingerprints")
    policy = environment.session_execution_policy(profile)
    if api.get("session_execution_policy") != policy:
        raise ValueError("API session execution policy does not match the selected restricted C6 policy")
    if ui.get("session_execution_policy") != policy:
        raise ValueError("Talk UI session execution policy does not match the selected restricted C6 policy")
    migration_verification = environment.validate_m6_migration_verification(api.get("migration_verification"), root)
    if ui.get("migration_verification") != migration_verification:
        raise ValueError("Talk UI migration provenance does not match selected API")
    acceptance_session_id = api.get("acceptance_session_id")
    try:
        uuid.UUID(acceptance_session_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("API has no valid owned acceptance session identity") from exc
    if ui.get("acceptance_session_id") != acceptance_session_id:
        raise ValueError("Talk UI acceptance session identity does not match selected API")
    if api_source_fingerprints != _api_source_fingerprints(root):
        raise ValueError("API source fingerprints do not match the current checkout; restart the selected API")
    source_agent_fingerprint = clone.get("source_agent_status_fingerprint")
    if (clone.get("source_agent_configured") is not True or clone.get("source_consent_valid") is not True
            or clone.get("source_agent_terminated") is not False or not source_agent_fingerprint
            or source_agent_fingerprint != clone.get("agent_status_fingerprint")):
        raise ValueError("cloned agent identity or consent is not valid")
    api_clone = _mapping(api.get("source_clone"), "API clone identity")
    for key in ("container_id", "database", "source_image_digest", "dump_sha256", "row_count_fingerprint",
                "source_data_fingerprint", "target_data_fingerprint", "agent_status_fingerprint"):
        if clone.get(key) is not None and api_clone.get(key) != clone[key]:
            raise ValueError("API clone identity does not match E1")
    server = _mapping(api.get("actual_server_address_and_port"), "API database identity")
    expected_server_port = environment_record["db_identity"]["talk"].get("server_port")
    if api.get("actual_current_database") != expected_database or server.get("port") != expected_server_port:
        raise ValueError("API database identity does not match E1")
    runtime = _mapping(ui.get("runtime_identity"), "Talk UI runtime identity")
    if runtime.get("browser_api_url") != expected_api_url:
        raise ValueError("Talk UI live browser API URL does not match selected C6")
    if runtime.get("ui_origin") != ui_origin:
        raise ValueError("Talk UI live origin does not match its selected URL")
    if runtime.get("env_id") != env_id:
        raise ValueError("Talk UI runtime environment does not match E1")
    database = _mapping(runtime.get("database"), "Talk UI database identity")
    if (database.get("database") != expected_database or database.get("server_port") != expected_server_port
            or database.get("url_port") != owned["talk_host_port"]
            or database.get("url_source") != profile["runtime_environment"]["ui_database_url_env"]
            or database.get("url_host") != "127.0.0.1"):
        raise ValueError("Talk UI database identity does not match E1")
    try:
        if ipaddress.ip_interface(server["address"]).ip != ipaddress.ip_interface(database["server_address"]).ip:
            raise ValueError("different servers")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Talk UI/API database server address does not match") from exc
    upstream = _mapping(runtime.get("api"), "Talk UI API upstream")
    if upstream.get("browser_api_url") != expected_api_url:
        raise ValueError("Talk UI live API browser URL does not match selected C6")
    if upstream.get("ui_origin") != ui_origin:
        raise ValueError("Talk UI live API origin does not match selected UI")
    if (upstream.get("session_execution_policy") != policy
            or upstream.get("acceptance_session_id") != acceptance_session_id
            or upstream.get("acceptance_session_registered") is not True
            or upstream.get("start_id") != api.get("start_id")):
        raise ValueError("Talk UI live API acceptance policy/session registration does not match C6/API")
    if upstream.get("migration_verification") != migration_verification:
        raise ValueError("Talk UI live migration provenance does not match the selected API")
    checks = _mapping(upstream.get("checks"), "Talk UI API upstream checks")
    if (upstream.get("status") != "ok" or checks.get("db") is not True
            or upstream.get("url_host") != api_url.hostname or upstream.get("url_port") != api_port):
        raise ValueError("Talk UI API upstream does not match the selected healthy API")
    configuration_paths = {
        "C1": profile["detection_config"], "C2": profile["asset_manifest"],
        "C4": profile["runtime_config"], "C5": profile["realtime_config"],
        "C6": "config/voice-barge-in/run-profile.json", "C7": profile["playback_coordination_config"],
        "C8": profile["audio_case_spec"],
    }
    config_fingerprints = dict(preflight["configuration_fingerprints"])
    config_fingerprints[configuration_paths["C6"]] = environment._sha256(root / configuration_paths["C6"])
    api_fingerprints = _mapping(api.get("config_fingerprints"), "API configuration fingerprints")
    for label, relative in configuration_paths.items():
        if api_fingerprints.get(label) != config_fingerprints[relative]:
            raise ValueError(f"API configuration fingerprint {label} does not match selected C6 files")
    for value, label, paths in (
        (worker, "Worker", (profile["detection_config"], profile["asset_manifest"])),
        (ui, "Talk UI", tuple(configuration_paths[key] for key in ("C1", "C2", "C6", "C7", "C8"))),
    ):
        fingerprints = _mapping(value.get("source_fingerprints"), f"{label} source fingerprints")
        if any(fingerprints.get(path) != config_fingerprints[path] for path in paths):
            raise ValueError(f"{label} source configuration fingerprint does not match the API")
    if worker.get("model_resource_fingerprint") != api_fingerprints["C2"]:
        raise ValueError("Worker model resource fingerprint does not match the API")
    image_id = _mapping(worker.get("container"), "Worker build fingerprint container").get("image_id")
    if not image_id:
        raise ValueError("Worker build fingerprint requires a standalone image identity")
    worker_build = hashlib.sha256(json.dumps({
        "source_fingerprints": worker["source_fingerprints"], "image_id": image_id,
    }, sort_keys=True).encode()).hexdigest()
    ui_build = hashlib.sha256(json.dumps({
        "source_fingerprints": ui["source_fingerprints"], "target": "talk-dev", "api_start_id": api["start_id"],
    }, sort_keys=True).encode()).hexdigest()
    if worker.get("build_fingerprint") != worker_build or ui.get("build_fingerprint") != ui_build:
        raise ValueError("Worker/Talk UI build fingerprint does not match its recorded build inputs")
    samples = audio.get("samples")
    if not isinstance(samples, list) or not samples or any(
        not isinstance(item, dict) or not isinstance(item.get("file_sha256"), str) or not _SHA256.fullmatch(item["file_sha256"])
        for item in samples
    ):
        raise ValueError("audio manifest must contain samples with file_sha256 fingerprints")
    for sample in samples:
        if not isinstance(sample.get("path"), str) or not Path(sample["path"]).is_file():
            raise ValueError("audio manifest sample path is missing")
        if environment._sha256(Path(sample["path"])) != sample["file_sha256"]:
            raise ValueError("audio sample hash does not match the frozen D1 manifest")
    return {
        "env_id": env_id,
        "database": expected_database,
        "api_url": expected_api_url,
        "browser_api_url": expected_api_url,
        "ui_origin": ui_origin,
        "worker_build_fingerprint": worker["build_fingerprint"],
        "ui_build_fingerprint": ui["build_fingerprint"],
        "api_start_id": api["start_id"],
        "api_source_fingerprints": api_source_fingerprints,
        "session_execution_policy": policy,
        "migration_verification": migration_verification,
        "acceptance_session_id": acceptance_session_id,
        "configuration_fingerprints": {label: api_fingerprints[label] for label in configuration_paths},
        "agent_status_fingerprint": source_agent_fingerprint,
        "startup_migration_verified": True,
        "audio_sample_count": len(samples),
    }


def build_talk_evidence_verification(
    c3: dict[str, Any],
    c3_path: Path | str,
    observation: dict[str, Any],
    database_readback: dict[str, Any],
) -> dict[str, Any]:
    """Validate an immutable Playwright observation against read-only DB facts."""
    resolved_c3_path = Path(c3_path).expanduser().resolve()
    if c3.get("kind") != "c3" or c3.get("schema_version") != 1:
        raise ValueError("selected run record is not a schema_version 1 C3")
    if (observation.get("kind") != "m6_talk_barge_in_observation"
            or observation.get("schema_version") not in {2, 3}):
        raise ValueError("selected Talk observation has an unsupported schema")
    policy = environment.session_execution_policy(c3)
    migration_verification = environment.validate_m6_migration_verification(c3.get("migration_verification"))
    if (observation.get("session_execution_policy") != policy
            or observation.get("session_id") != c3.get("acceptance_session_id")
            or observation.get("acceptance_session_id") != c3.get("acceptance_session_id")):
        raise ValueError("Talk observation policy or registered acceptance session does not match C3")
    if observation.get("migration_verification") != migration_verification:
        raise ValueError("Talk observation migration provenance does not match C3")
    if Path(str(observation.get("c3_path", ""))).expanduser().resolve() != resolved_c3_path:
        raise ValueError("Talk observation does not name the selected C3")
    if observation.get("c3_sha256") != environment._sha256(resolved_c3_path):
        raise ValueError("Talk observation C3 hash does not match its selected run record")
    if observation.get("env_id") != c3.get("env_id") or observation.get("api_start_id") != c3.get("api_start_id"):
        raise ValueError("Talk observation environment/API start identity does not match C3")
    command = _mapping(observation.get("command"), "confirmed barge-in command")
    command_result = _mapping(observation.get("command_result"), "confirmed barge-in result")
    target = _mapping(observation.get("target_identity"), "target identity")
    next_identity = _mapping(observation.get("next_identity"), "next response identity")
    ready = _mapping(observation.get("ready"), "server ready message")
    capture_open = _mapping(observation.get("capture_open"), "capture-open message")
    session_response = _mapping(observation.get("session_response"), "REST session response")
    barge_status = command_result.get("status")
    if barge_status not in {"accepted", "playback_only"}:
        raise ValueError("persisted-evidence verification requires an accepted or playback_only barge-in result")
    if observation.get("no_barge_commands_during_noise") is not True:
        raise ValueError("noise interruption must produce no confirmed barge-in command")
    if observation.get("target_was_audible") is not True:
        raise ValueError("barge-in target must have been confirmed audible")
    if observation.get("target_input_turn_id") != observation.get("prompt_input_turn_id"):
        raise ValueError("audible response target does not match its authorized prompt input")
    if observation.get("prompt_input_turn_id") == observation.get("interruption_input_turn_id"):
        raise ValueError("interruption speech must have a new input turn identity")
    if observation.get("committed_input_count") != 2:
        raise ValueError("prompt and interruption inputs must each be committed once")
    if (command.get("type") != "barge_in_confirmed"
            or command.get("session_id") != observation.get("session_id")
            or command.get("media_session_id") != observation.get("media_session_id")
            or command.get("capture_instance_id") != observation.get("capture_instance_id")
            or command.get("response_id") != observation.get("target_response_id")
            or command.get("playback_id") != observation.get("target_playback_id")
            or command.get("output_epoch") != observation.get("target_output_epoch")
            or command.get("local_playback_state") != "started"
            or not isinstance(command.get("receipt_prefix"), list)
            or not command["receipt_prefix"]):
        raise ValueError("confirmed command does not bind the audible target and persisted receipt prefix")
    previous_receipt_seq = -1
    for receipt_value in command["receipt_prefix"]:
        receipt = _mapping(receipt_value, "confirmed command playback receipt")
        if (receipt.get("session_id") != observation.get("session_id")
                or receipt.get("media_session_id") != observation.get("media_session_id")
                or receipt.get("response_id") != observation.get("target_response_id")
                or receipt.get("playback_id") != observation.get("target_playback_id")
                or receipt.get("output_epoch") != observation.get("target_output_epoch")
                or type(receipt.get("producer_seq")) is not int
                or receipt["producer_seq"] <= previous_receipt_seq
                or receipt.get("action") not in {"open", "audible", "progress", "end", "interrupt"}):
            raise ValueError("confirmed playback receipt prefix has an invalid identity, sequence, or action")
        previous_receipt_seq = receipt["producer_seq"]
    if command["receipt_prefix"][-1].get("action") != "interrupt":
        raise ValueError("started confirmed barge-in must end with the exact target interrupt receipt")
    if (command_result.get("type") != "barge_in_result"
            or command_result.get("command_id") != command.get("command_id")
            or command_result.get("capture_instance_id") != command.get("capture_instance_id")
            or command_result.get("session_id") != observation.get("session_id")
            or command_result.get("media_session_id") != observation.get("media_session_id")
            or command_result.get("response_id") != observation.get("target_response_id")
            or command_result.get("playback_id") != observation.get("target_playback_id")
            or command_result.get("output_epoch") != observation.get("target_output_epoch")):
        raise ValueError("barge-in result does not echo the exact confirmed command identity")
    if (target.get("session_id") != observation.get("session_id")
            or target.get("media_session_id") != observation.get("media_session_id")
            or target.get("response_id") != observation.get("target_response_id")
            or target.get("playback_id") != observation.get("target_playback_id")
            or target.get("output_epoch") != observation.get("target_output_epoch")
            or target.get("input_turn_id") != observation.get("prompt_input_turn_id")
            or target.get("stimulus_revision") != observation.get("prompt_stimulus_revision")):
        raise ValueError("target identity does not match the confirmed command")
    if (next_identity.get("session_id") != observation.get("session_id")
            or next_identity.get("media_session_id") != observation.get("media_session_id")
            or next_identity.get("response_id") != observation.get("next_response_id")
            or next_identity.get("playback_id") != observation.get("next_playback_id")
            or next_identity.get("output_epoch") != observation.get("next_output_epoch")
            or next_identity.get("input_turn_id") != observation.get("interruption_input_turn_id")
            or next_identity.get("stimulus_revision") != observation.get("interruption_stimulus_revision")):
        raise ValueError("next response identity does not match its interrupted voice input")
    if (capture_open.get("type") != "capture_open"
            or capture_open.get("session_id") != observation.get("session_id")
            or capture_open.get("media_session_id") != observation.get("media_session_id")
            or capture_open.get("capture_instance_id") != observation.get("capture_instance_id")):
        raise ValueError("capture identity does not match the confirmed command")
    session_capability = _mapping(session_response.get("barge_in_capability"), "REST barge-in capability")
    ready_capability = _mapping(ready.get("barge_in_capability"), "ready barge-in capability")
    if (ready.get("type") != "ready"
            or ready.get("session_id") != observation.get("session_id")
            or ready.get("media_session_id") != observation.get("media_session_id")
            or ready.get("cloud_session_id") != observation.get("cloud_session_id")
            or session_response.get("session_id") != observation.get("session_id")
            or session_response.get("media_session_id") != observation.get("media_session_id")
            or session_capability != ready_capability
            or session_capability.get("supported") is not True
            or not isinstance(observation.get("cloud_session_id"), str)
            or not observation["cloud_session_id"]):
        raise ValueError("REST session and server ready identities do not match the selected media session")
    media_reopen_sessions: list[dict[str, Any]] = []
    if observation.get("schema_version") == 3:
        raw_reopens = observation.get("media_reopen_sessions")
        if not isinstance(raw_reopens, list) or not raw_reopens:
            raise ValueError("schema v3 Talk evidence requires at least one completed media reopen")
        seen_media_sessions = {observation.get("media_session_id")}
        seen_cloud_sessions = {observation.get("cloud_session_id")}
        for index, raw_reopen in enumerate(raw_reopens):
            reopen = _mapping(raw_reopen, f"media reopen {index}")
            reopen_response = _mapping(reopen.get("session_response"), f"media reopen {index} REST response")
            reopen_ready = _mapping(reopen.get("ready"), f"media reopen {index} ready")
            reopen_capture = _mapping(reopen.get("capture_open"), f"media reopen {index} capture-open")
            reopen_rest_capability = _mapping(reopen_response.get("barge_in_capability"), f"media reopen {index} REST capability")
            reopen_ready_capability = _mapping(reopen_ready.get("barge_in_capability"), f"media reopen {index} ready capability")
            reopen_media = reopen_response.get("media_session_id")
            reopen_cloud = reopen_ready.get("cloud_session_id")
            if (reopen.get("stop_completed") is not True
                    or reopen_response.get("session_id") != observation.get("session_id")
                    or reopen_ready.get("type") != "ready"
                    or reopen_ready.get("session_id") != observation.get("session_id")
                    or reopen_ready.get("media_session_id") != reopen_media
                    or reopen_capture.get("type") != "capture_open"
                    or reopen_capture.get("session_id") != observation.get("session_id")
                    or reopen_capture.get("media_session_id") != reopen_media
                    or reopen_rest_capability != reopen_ready_capability
                    or reopen_rest_capability != _mapping(session_response.get("barge_in_capability"), "REST barge-in capability")
                    or not isinstance(reopen_media, str) or not reopen_media
                    or not isinstance(reopen_cloud, str) or not reopen_cloud
                    or reopen_media in seen_media_sessions or reopen_cloud in seen_cloud_sessions):
                raise ValueError(f"media reopen {index} does not prove a distinct ready and stopped C3 session")
            seen_media_sessions.add(reopen_media)
            seen_cloud_sessions.add(reopen_cloud)
            media_reopen_sessions.append({
                "media_session_id": reopen_media,
                "cloud_session_id": reopen_cloud,
            })
    for label, event_key, input_key, response_key in (
        ("target", "target_model_call_started", "prompt_input_turn_id", "target_response_id"),
        ("next", "next_model_call_started", "interruption_input_turn_id", "next_response_id"),
    ):
        message = _mapping(observation.get(event_key), f"{label} model-call event message")
        event = _mapping(message.get("event"), f"{label} model-call event")
        payload = _mapping(event.get("payload"), f"{label} model-call payload")
        identity = _mapping(payload.get("identity"), f"{label} model-call identity")
        if (message.get("type") != "event" or event.get("event_type") != "voice_model_call_started"
                or identity.get("session_id") != observation.get("session_id")
                or identity.get("input_turn_id") != observation.get(input_key)
                or payload.get("response_id") != observation.get(response_key)):
            raise ValueError(f"{label} response is not bound to its committed voice input")
        if label == "target":
            if (payload.get("playback_id") != observation.get("target_playback_id")
                    or payload.get("output_epoch") != observation.get("target_output_epoch")
                    or identity.get("stimulus_revision") != observation.get("prompt_stimulus_revision")):
                raise ValueError("target model-call event does not match the exact response playback identity")
        elif (payload.get("playback_id") != observation.get("next_playback_id")
                or payload.get("output_epoch") != observation.get("next_output_epoch")
                or identity.get("stimulus_revision") != observation.get("interruption_stimulus_revision")):
            raise ValueError("next model-call event does not match the exact response playback identity")
    for label, event_key, input_key, revision_key in (
        ("prompt", "prompt_input_committed", "prompt_input_turn_id", "prompt_stimulus_revision"),
        ("interruption", "interruption_input_committed", "interruption_input_turn_id", "interruption_stimulus_revision"),
    ):
        message = _mapping(observation.get(event_key), f"{label} input-commit message")
        event = _mapping(message.get("event"), f"{label} input-commit event")
        payload = _mapping(event.get("payload"), f"{label} input-commit payload")
        identity = _mapping(payload.get("identity"), f"{label} input-commit identity")
        if (message.get("type") != "event" or event.get("event_type") != "voice_input_committed"
                or identity.get("session_id") != observation.get("session_id")
                or identity.get("input_turn_id") != observation.get(input_key)
                or identity.get("stimulus_revision") != observation.get(revision_key)):
            raise ValueError(f"{label} input commit does not match the selected voice turn")
    if observation.get("next_response_end_receipt_persisted") is not True:
        raise ValueError("the next response must have a persisted terminal playback receipt")
    if observation.get("next_response_terminal_http_status") != 200:
        raise ValueError("the next response terminal playback receipt did not receive HTTP 200")
    next_terminal = _mapping(observation.get("next_response_terminal_receipt"), "next response terminal receipt")
    if (next_terminal.get("action") != "end" or next_terminal.get("scope") != "complete"
            or next_terminal.get("response_id") != observation.get("next_response_id")
            or next_terminal.get("playback_id") != observation.get("next_playback_id")
            or next_terminal.get("output_epoch") != observation.get("next_output_epoch")):
        raise ValueError("next response terminal receipt is not complete or target-matched")
    if observation.get("target_response_id") == observation.get("next_response_id"):
        raise ValueError("the post-interruption response must have a new response identity")
    for key in (
        "session_id", "capture_instance_id", "prompt_input_turn_id", "interruption_input_turn_id",
    ):
        try:
            uuid.UUID(str(observation[key]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Talk observation has an invalid {key}") from exc
    try:
        uuid.UUID(str(command.get("command_id")))
    except (TypeError, ValueError) as exc:
        raise ValueError("Talk observation has an invalid command_id") from exc
    for key in ("prompt_stimulus_revision", "interruption_stimulus_revision", "target_output_epoch", "next_output_epoch"):
        value = observation.get(key)
        if type(value) is not int or value < 0:
            raise ValueError(f"Talk observation has an invalid {key}")

    records = _mapping(c3.get("records"), "C3 records")
    cache_root = resolved_c3_path.parent.parent.resolve()
    record_documents: dict[str, dict[str, Any]] = {}
    for label, raw_record in records.items():
        record = _mapping(raw_record, f"C3 {label} record")
        record_path = Path(str(record.get("path", ""))).expanduser().resolve()
        if (not record_path.is_relative_to(cache_root) or not record_path.is_file()
                or environment._sha256(record_path) != record.get("sha256")):
            raise ValueError(f"C3 {label} record is missing, outside the cache, or changed")
        record_documents[label] = _read_json(record_path, f"C3 {label} record")
    for label in ("API", "U1"):
        document = _mapping(record_documents.get(label), f"C3 {label} document")
        if (document.get("session_execution_policy") != policy
                or document.get("acceptance_session_id") != c3.get("acceptance_session_id")
                or document.get("migration_verification") != migration_verification):
            raise ValueError(f"C3 {label} acceptance session/policy does not match C3")
    d1_record = _mapping(records.get("D1"), "C3 D1 record")
    d1_path = Path(str(d1_record.get("path", ""))).expanduser().resolve()
    if environment._sha256(d1_path) != d1_record.get("sha256"):
        raise ValueError("C3 D1 manifest changed before persistence verification")
    d1 = _read_json(d1_path, "C3 D1 manifest")
    samples = d1.get("samples")
    if not isinstance(samples, list):
        raise ValueError("C3 D1 manifest has no sample list")
    sample_index = {sample.get("sample_id"): sample for sample in samples if isinstance(sample, dict)}
    selected_samples = (
        ("ordinary-tuning-01", "prompt_sha256"),
        ("pure-noise-tuning-01", "noise_sha256"),
        ("short-stop-acceptance-01", "interruption_sha256"),
    )
    if observation.get("sample_ids") != [sample_id for sample_id, _ in selected_samples]:
        raise ValueError("Talk observation sample identities do not match the selected C3 D1 cases")
    for sample_id, digest_key in selected_samples:
        sample = sample_index.get(sample_id)
        if (not sample or observation.get(digest_key) != sample.get("file_sha256")
                or not isinstance(sample.get("path"), str)
                or environment._sha256(Path(sample["path"])) != sample.get("file_sha256")):
            raise ValueError(f"Talk observation does not match the frozen D1 sample {sample_id}")

    readback = _mapping(database_readback, "read-only talk database result")
    e1 = record_documents.get("E1")
    if not isinstance(e1, dict):
        raise ValueError("C3 E1 record is missing")
    talk_identity = _mapping(e1.get("db_identity", {}).get("talk"), "E1 talk database identity")
    expected_migrations = e1.get("source_clone", {}).get("target_migration_count")
    if (readback.get("database") != c3.get("database")
            or readback.get("database") != talk_identity.get("database")
            or readback.get("server_port") != talk_identity.get("server_port")):
        raise ValueError("read-only database identity does not match C3")
    if (type(readback.get("migration_count")) is not int or readback["migration_count"] <= 0
            or readback["migration_count"] != expected_migrations):
        raise ValueError("talk database has no verified migration history")
    if (readback.get("active_realtime_session_count") != 0
            or readback.get("matched_realtime_session_count") != 1
            or readback.get("matched_realtime_session_status") != "closed"
            or readback.get("matched_realtime_session_closed_at") is None):
        raise ValueError("selected cloud session is still active, missing, or did not close cleanly")

    prompt_turn = _mapping(readback.get("prompt_turn"), "prompt voice-turn readback")
    interruption_turn = _mapping(readback.get("interruption_turn"), "interruption voice-turn readback")
    for label, turn in (("prompt", prompt_turn), ("interruption", interruption_turn)):
        if (turn.get("completion_count") != 1 or turn.get("turn_status") != "completed"
                or turn.get("input_committed_event_count") != 1 or turn.get("turn_completed_event_count") != 1):
            raise ValueError(f"{label} input/turn completion was not persisted exactly once")
    if (prompt_turn.get("playback_scope") != "partial" or prompt_turn.get("user_message_count") != 1
            or prompt_turn.get("assistant_message_count") != 0):
        raise ValueError("interrupted prompt history does not reflect its partial audible playback")
    if (interruption_turn.get("playback_scope") != "complete" or interruption_turn.get("user_message_count") != 1
            or interruption_turn.get("assistant_message_count") != 1):
        raise ValueError("the next completed voice turn history is incomplete")
    for turn, response_key, playback_key, epoch_key in (
        (prompt_turn, "target_response_id", "target_playback_id", "target_output_epoch"),
        (interruption_turn, "next_response_id", "next_playback_id", "next_output_epoch"),
    ):
        metadata = _mapping(turn.get("completion_metadata"), "voice-turn completion metadata")
        if (metadata.get("final_response_id") != observation.get(response_key)
                or metadata.get("final_playback_id") != observation.get(playback_key)
                or metadata.get("final_output_epoch") != observation.get(epoch_key)):
            raise ValueError("voice-turn completion metadata does not match its exact response/playback")
    if readback.get("target_playback_audible_count") != 1 or readback.get("target_playback_interrupted_count") != 1:
        raise ValueError("target playback is not proven audible and interrupted exactly once")
    generation_order = None
    if barge_status == "playback_only":
        generation_order = readback.get("target_generation_before_interrupt")
        ordering_error = "playback_only requires one completed model-end event before the exact target interrupt"
        if (not isinstance(generation_order, dict)
                or generation_order.get("completed_model_end_count") != 1
                or generation_order.get("interrupt_count") != 1):
            raise ValueError(ordering_error)
        try:
            ended_at = datetime.fromisoformat(generation_order["model_ended_at"])
            interrupted_at = datetime.fromisoformat(generation_order["interrupted_at"])
            if ended_at.tzinfo is None or interrupted_at.tzinfo is None or ended_at >= interrupted_at:
                raise ValueError(ordering_error)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(ordering_error) from exc
    if readback.get("next_playback_audible_count") != 1 or readback.get("next_playback_end_count") != 1:
        raise ValueError("next response does not have one audible and terminal playback record")
    prefix_facts = readback.get("confirmed_receipt_prefix")
    if (not isinstance(prefix_facts, list) or len(prefix_facts) != len(command["receipt_prefix"])
            or any(
                fact.get("persisted_once") is not True
                or fact.get("action") != receipt.get("action")
                or fact.get("producer_seq") != receipt.get("producer_seq")
                for fact, receipt in zip(prefix_facts, command["receipt_prefix"], strict=True)
            )):
        raise ValueError("confirmed playback receipt prefix is not persisted exactly once with matching content")
    reopen_facts = readback.get("media_reopen_session_facts", [])
    if len(reopen_facts) != len(media_reopen_sessions) or any(
        fact.get("row_count") != 1 or fact.get("status") != "closed" or fact.get("closed_at") is None
        for fact in reopen_facts
    ):
        raise ValueError("reopened media sessions are missing one exact closed realtime row each")

    return {
        "schema_version": 1,
        "kind": "talk_persistence_verification",
        "status": "verified",
        "env_id": c3["env_id"],
        "database": c3["database"],
        "api_start_id": c3["api_start_id"],
        "session_id": observation["session_id"],
        "media_session_id": observation["media_session_id"],
        "cloud_session_id": observation["cloud_session_id"],
        "prompt_input_turn_id": observation["prompt_input_turn_id"],
        "interruption_input_turn_id": observation["interruption_input_turn_id"],
        "prompt_turn": {key: prompt_turn[key] for key in ("completion_count", "playback_scope", "turn_status", "user_message_count", "assistant_message_count")},
        "interruption_turn": {key: interruption_turn[key] for key in ("completion_count", "playback_scope", "turn_status", "user_message_count", "assistant_message_count")},
        "target_response_id": observation["target_response_id"],
        "next_response_id": observation["next_response_id"],
        "barge_in_result": command_result["status"],
        "barge_in_evidence_scope": (
            "active_generation_cancel_command" if barge_status == "accepted"
            else "local_playback_interrupt_after_generation"
        ),
        "provider_cancel_terminal_verified": False,
        "target_generation_before_interrupt": generation_order,
        "confirmed_receipt_prefix_count": len(prefix_facts),
        "media_reopen_sessions_verified_count": len(reopen_facts),
        "target_playback_interrupted_count": readback["target_playback_interrupted_count"],
        "next_playback_end_count": readback["next_playback_end_count"],
    }


async def _read_talk_database_evidence(
    environment_record_path: Path,
    c3: dict[str, Any],
    observation: dict[str, Any],
    environment_values: dict[str, str],
) -> dict[str, Any]:
    import asyncpg

    dsn, environment_record = environment.runtime_dsn_from_environment_record(
        environment_record_path, environment_values
    )
    if environment_record.get("env_id") != c3.get("env_id"):
        raise ValueError("selected E1 environment does not match the C3 run")
    connection = await asyncpg.connect(dsn, timeout=10)
    try:
        async with connection.transaction(readonly=True):
            identity = await connection.fetchrow(
                "SELECT current_database() AS database, inet_server_port() AS server_port"
            )
            migration_count = await connection.fetchval("SELECT count(*) FROM schema_migrations")
            session_id = uuid.UUID(str(observation["session_id"]))
            active_sessions = await connection.fetchval(
                "SELECT count(*) FROM voice_realtime_sessions WHERE session_id=$1 AND status='active'",
                session_id,
            )
            matched_session = await connection.fetchrow(
                """SELECT count(*)::int AS row_count, min(status) AS status, max(closed_at) AS closed_at
                   FROM voice_realtime_sessions WHERE session_id=$1 AND qwen_session_id=$2""",
                session_id, str(observation["cloud_session_id"]),
            )
            media_reopen_session_facts: list[dict[str, Any]] = []
            for reopen in observation.get("media_reopen_sessions", []):
                reopen_ready = _mapping(reopen.get("ready"), "media reopen ready")
                reopen_row = await connection.fetchrow(
                    """SELECT count(*)::int AS row_count, min(status) AS status, max(closed_at) AS closed_at
                       FROM voice_realtime_sessions WHERE session_id=$1 AND qwen_session_id=$2""",
                    session_id, str(reopen_ready["cloud_session_id"]),
                )
                media_reopen_session_facts.append(dict(reopen_row))

            async def turn_facts(input_turn_id: str, stimulus_revision: int) -> dict[str, Any]:
                input_uuid = uuid.UUID(input_turn_id)
                completion = await connection.fetchrow(
                    """SELECT count(*)::int AS completion_count,
                              min(c.result->>'playback_scope') AS playback_scope,
                              min(t.status) AS turn_status,
                              (array_agg(c.result->'completion_metadata'))[1] AS completion_metadata
                       FROM voice_turn_completions AS c
                       JOIN agent_turns AS t ON t.id=c.turn_id
                       WHERE c.session_id=$1 AND c.input_turn_id=$2 AND c.stimulus_revision=$3""",
                    session_id, input_uuid, stimulus_revision,
                )
                completion = dict(completion)
                metadata_value = completion.get("completion_metadata")
                if isinstance(metadata_value, list):
                    metadata_value = metadata_value[0] if len(metadata_value) == 1 else None
                completion["completion_metadata"] = (
                    _database_json_mapping(metadata_value, "voice-turn completion metadata")
                    if metadata_value is not None else None
                )
                source_identity = f"voice:{session_id}:{input_uuid}:{stimulus_revision}"
                messages = await connection.fetch(
                    "SELECT role, count(*)::int AS count FROM chat_messages "
                    "WHERE session_id=$1 AND source_message_id = ANY($2::text[]) GROUP BY role",
                    session_id, [f"{source_identity}:user", f"{source_identity}:assistant"],
                )
                message_counts = {row["role"]: row["count"] for row in messages}
                events = await connection.fetchrow(
                    """SELECT
                         count(*) FILTER (WHERE event_type='voice_input_committed')::int AS input_committed_event_count,
                         count(*) FILTER (WHERE event_type='voice_turn_completed')::int AS turn_completed_event_count
                       FROM behavior_events
                       WHERE session_id=$1 AND media_session_id=$2
                         AND input_turn_id=$3 AND stimulus_revision=$4""",
                    session_id, str(observation["media_session_id"]), str(input_uuid), stimulus_revision,
                )
                return {
                    **dict(completion),
                    "user_message_count": message_counts.get("user", 0),
                    "assistant_message_count": message_counts.get("assistant", 0),
                    **dict(events),
                }

            prompt_turn = await turn_facts(
                str(observation["prompt_input_turn_id"]), int(observation["prompt_stimulus_revision"])
            )
            interruption_turn = await turn_facts(
                str(observation["interruption_input_turn_id"]), int(observation["interruption_stimulus_revision"])
            )
            async def playback_count(event_type: str, response_id: str, playback_id: str, output_epoch: int) -> int:
                return int(await connection.fetchval(
                    """SELECT count(*) FROM behavior_events
                       WHERE session_id=$1 AND media_session_id=$2 AND event_type=$3 AND response_id=$4
                         AND output_epoch=$5 AND payload->>'playback_id'=$6""",
                    session_id, str(observation["media_session_id"]), event_type,
                    response_id, output_epoch, playback_id,
                ))
            target_audible = await playback_count(
                "voice_first_audible", str(observation["target_response_id"]),
                str(observation["target_playback_id"]), int(observation["target_output_epoch"]),
            )
            target_interrupted = await playback_count(
                "voice_playback_interrupted", str(observation["target_response_id"]),
                str(observation["target_playback_id"]), int(observation["target_output_epoch"]),
            )
            next_audible = await playback_count(
                "voice_first_audible", str(observation["next_response_id"]),
                str(observation["next_playback_id"]), int(observation["next_output_epoch"]),
            )
            next_ended = await playback_count(
                "voice_playback_ended", str(observation["next_response_id"]),
                str(observation["next_playback_id"]), int(observation["next_output_epoch"]),
            )
            generation_order = None
            if observation.get("command_result", {}).get("status") == "playback_only":
                order_row = await connection.fetchrow(
                    """SELECT
                         count(*) FILTER (WHERE event_type='voice_model_call_ended'
                           AND payload->>'reason'='completed' AND input_turn_id=$6
                           AND stimulus_revision=$7)::int AS completed_model_end_count,
                         count(*) FILTER (WHERE event_type='voice_playback_interrupted')::int AS interrupt_count,
                         min(recorded_at) FILTER (WHERE event_type='voice_model_call_ended'
                           AND payload->>'reason'='completed' AND input_turn_id=$6
                           AND stimulus_revision=$7) AS model_ended_at,
                         min(recorded_at) FILTER (WHERE event_type='voice_playback_interrupted') AS interrupted_at
                       FROM behavior_events
                       WHERE session_id=$1 AND media_session_id=$2 AND response_id=$3
                         AND output_epoch=$4 AND payload->>'playback_id'=$5
                         AND event_type IN ('voice_model_call_ended', 'voice_playback_interrupted')""",
                    session_id, str(observation["media_session_id"]), str(observation["target_response_id"]),
                    int(observation["target_output_epoch"]), str(observation["target_playback_id"]),
                    str(observation["prompt_input_turn_id"]), int(observation["prompt_stimulus_revision"]),
                )
                generation_order = dict(order_row)
                for key in ("model_ended_at", "interrupted_at"):
                    timestamp = generation_order[key]
                    generation_order[key] = timestamp.isoformat() if timestamp is not None else None
            receipt_facts: list[dict[str, Any]] = []
            action_events = {
                "open": ("voice_playback_started", ("start_ms",)),
                "audible": ("voice_first_audible", ("audible_ms",)),
                "progress": ("voice_playback_progress", ("played_ms", "interval_ms", "phase")),
                "end": ("voice_playback_ended", ("scope", "played_ms")),
                "interrupt": ("voice_playback_interrupted", ("reason", "played_ms")),
            }
            command = _mapping(observation["command"], "confirmed barge-in command")
            for receipt_value in command["receipt_prefix"]:
                receipt = _mapping(receipt_value, "confirmed command playback receipt")
                event_type, payload_keys = action_events[receipt["action"]]
                rows = await connection.fetch(
                    """SELECT event_type, producer_seq, payload, turn_id
                       FROM behavior_events
                       WHERE session_id=$1 AND media_session_id=$2 AND response_id=$3
                         AND output_epoch=$4 AND producer_seq=$5
                         AND event_type=$6 AND payload->>'playback_id'=$7""",
                    session_id, str(observation["media_session_id"]), receipt["response_id"],
                    int(receipt["output_epoch"]), int(receipt["producer_seq"]), event_type,
                    receipt["playback_id"],
                )
                row = rows[0] if len(rows) == 1 else None
                payload = _database_json_mapping(row["payload"], "playback event payload") if row is not None else {}
                matches_payload = row is not None and all(
                    payload.get(key) == receipt.get(key) for key in payload_keys
                )
                if matches_payload:
                    database_turn_id = row["turn_id"]
                    receipt_turn_id = receipt.get("turn_id")
                    matches_payload = (
                        None if database_turn_id is None else str(database_turn_id)
                    ) == (None if receipt_turn_id is None else str(receipt_turn_id))
                receipt_facts.append({
                    "action": receipt["action"],
                    "producer_seq": int(receipt["producer_seq"]),
                    "persisted_once": matches_payload,
                })
        return {
            "database": identity["database"],
            "server_port": int(identity["server_port"]),
            "migration_count": int(migration_count),
            "active_realtime_session_count": int(active_sessions),
            "matched_realtime_session_count": int(matched_session["row_count"]),
            "matched_realtime_session_status": matched_session["status"],
            "matched_realtime_session_closed_at": matched_session["closed_at"],
            "media_reopen_session_facts": media_reopen_session_facts,
            "prompt_turn": prompt_turn,
            "interruption_turn": interruption_turn,
            "target_playback_audible_count": target_audible,
            "target_playback_interrupted_count": target_interrupted,
            "next_playback_audible_count": next_audible,
            "next_playback_end_count": next_ended,
            "confirmed_receipt_prefix": receipt_facts,
            "target_generation_before_interrupt": generation_order,
        }
    finally:
        await connection.close()


async def verify_talk_evidence(
    *,
    c3_path: Path,
    scenario_path: Path,
    environment_record_path: Path,
    environment_values: dict[str, str],
) -> dict[str, Any]:
    c3 = _read_json(c3_path, "C3 run record")
    scenario = _read_json(scenario_path, "Talk scenario evidence")
    evidence_dir = Path(str(c3.get("evidence_dir", ""))).resolve()
    if not scenario_path.resolve().is_relative_to(evidence_dir):
        raise ValueError("Talk scenario evidence must remain under the C3 evidence directory")
    readback = await _read_talk_database_evidence(
        environment_record_path, c3, scenario, environment_values
    )
    return build_talk_evidence_verification(c3, c3_path, scenario, readback)


def run_environment_phase(args: argparse.Namespace) -> dict[str, Any]:
    output = args.output_manifest.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite preflight manifest: {output}")
    profile = environment.load_profile(args.profile)
    record = _read_json(args.env_record, "E1 environment record")
    result = build_environment_preflight(profile, record, repository_root=REPOSITORY_ROOT)
    for scope in ("pytest", "talk"):
        result["databases"][scope] = _probe_database_scope(record["env_id"], record, scope)
    environment._atomic_json_write(output, result, refuse_existing=True)
    return result


def run_talk_evidence_verification(args: argparse.Namespace) -> dict[str, Any]:
    output = args.output_manifest.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite Talk verification manifest: {output}")
    if args.c3_record is None or args.scenario_record is None:
        raise ValueError("verify phase requires explicit --c3-record and --scenario-record paths")
    c3_path = args.c3_record.resolve()
    scenario_path = args.scenario_record.resolve()
    cache_root = environment.cache_root_default().resolve() / "voice-barge-in"
    if not c3_path.is_relative_to(cache_root) or not scenario_path.is_relative_to(cache_root):
        raise ValueError("C3 and scenario evidence must remain under the selected Hexis cache")
    c3 = _read_json(c3_path, "C3 run record")
    if c3.get("kind") != "c3":
        raise ValueError("selected run record is not a C3")
    scenario_dir = Path(str(c3.get("evidence_dir", ""))).expanduser().resolve()
    if not scenario_path.is_relative_to(scenario_dir):
        raise ValueError("scenario evidence must be inside the selected C3 evidence directory")
    if not output.is_relative_to(scenario_dir):
        raise ValueError("verification output must be inside the selected C3 evidence directory")
    e1_path = args.env_record.resolve()
    c3_records = _mapping(c3.get("records"), "C3 input records")
    documents: dict[str, dict[str, Any]] = {}
    for label in ("E1", "W1", "U1", "API", "D1"):
        item = _mapping(c3_records.get(label), f"C3 {label} record")
        record_path = Path(str(item.get("path", ""))).expanduser().resolve()
        if (not record_path.is_relative_to(cache_root) or not record_path.is_file()
                or environment._sha256(record_path) != item.get("sha256")):
            raise ValueError(f"C3 {label} record is missing, outside the cache, or changed")
        documents[label] = _read_json(record_path, f"C3 {label} record")
    e1_ref = c3_records.get("E1", {})
    frozen_e1_path = Path(str(e1_ref.get("path", ""))).expanduser().resolve()
    source_e1_path = Path(str(e1_ref.get("source_path", e1_ref.get("path", "")))).expanduser().resolve()
    if e1_path not in {source_e1_path, frozen_e1_path}:
        raise ValueError("selected E1 path does not match the C3 run record")
    profile = environment.load_profile(args.profile)
    if c3.get("session_execution_policy") != environment.session_execution_policy(profile):
        raise ValueError("C3 session execution policy does not match selected restricted C6")
    environment.validate_m6_migration_verification(c3.get("migration_verification"))
    config_paths = {
        "C1": profile["detection_config"], "C2": profile["asset_manifest"],
        "C4": profile["runtime_config"], "C5": profile["realtime_config"],
        "C6": "config/voice-barge-in/run-profile.json", "C7": profile["playback_coordination_config"],
        "C8": profile["audio_case_spec"],
    }
    for label, relative in config_paths.items():
        path = (REPOSITORY_ROOT / relative).resolve()
        if environment._sha256(path) != c3.get("configuration_fingerprints", {}).get(label):
            raise ValueError(f"{label} changed after C3 creation; regenerate a C3 before verification")
    selected = validate_talk_records(documents["W1"], documents["U1"], documents["API"], documents["D1"], documents["E1"], profile)
    if c3.get("acceptance_session_id") != selected["acceptance_session_id"]:
        raise ValueError("C3 acceptance session does not match the selected API/U1 registration")
    if c3.get("migration_verification") != selected["migration_verification"]:
        raise ValueError("C3 migration provenance does not match selected API/U1")
    if documents["W1"].get("source_fingerprints") != environment._worker_ui_fingerprints():
        raise ValueError("Worker source fingerprint drifted after C3 creation")
    if documents["U1"].get("source_fingerprints") != environment._talk_ui_fingerprints():
        raise ValueError("Talk UI source fingerprint drifted after C3 creation")
    if documents["API"].get("source_fingerprints") != _api_source_fingerprints(REPOSITORY_ROOT):
        raise ValueError("API source fingerprint drifted after C3 creation")
    report = asyncio.run(verify_talk_evidence(
        c3_path=c3_path,
        scenario_path=scenario_path,
        environment_record_path=frozen_e1_path,
        environment_values=dict(os.environ),
    ))
    report["c3_record"] = str(c3_path)
    report["scenario_record"] = str(scenario_path)
    report["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    environment._atomic_json_write(output, report, refuse_existing=True)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only M6 voice barge-in environment preflight")
    parser.add_argument("--phase", choices=("environment", "talk", "verify"), required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--env-record", type=Path, required=True)
    parser.add_argument("--output-manifest", type=Path, required=True)
    parser.add_argument("--worker-run-record", type=Path)
    parser.add_argument("--ui-run-record", type=Path)
    parser.add_argument("--api-start-record", type=Path)
    parser.add_argument("--audio-manifest", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--c3-record", type=Path)
    parser.add_argument("--scenario-record", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.phase == "environment":
            result = run_environment_phase(args)
        elif args.phase == "talk":
            if args.output_manifest.resolve().exists():
                raise FileExistsError(f"refusing to overwrite C3 manifest: {args.output_manifest}")
            profile = environment.load_profile(args.profile)
            record = _read_json(args.env_record, "E1 environment record")
            paths = (args.worker_run_record, args.ui_run_record, args.api_start_record, args.audio_manifest)
            if any(path is None for path in paths) or args.evidence_dir is None:
                raise ValueError("talk phase requires worker/UI/API/audio records and an evidence directory")
            selected_start_path = record.get("owned_resources", {}).get("api_process", {}).get("start_record_path")
            if not isinstance(selected_start_path, str) or args.api_start_record.resolve() != Path(selected_start_path).resolve():
                raise ValueError("API start record path does not match the E1 owned process")
            docs = [_read_json(path, label) for path, label in zip(paths, ("Worker record", "UI record", "API start record", "audio manifest"))]
            summary = validate_talk_records(*docs, record, profile)
            input_paths = (args.env_record, *paths)
            frozen_records = _freeze_talk_input_records(input_paths, (record, *docs), args.evidence_dir)
            result = {
                "schema_version": 1, "kind": "c3", **summary,
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "evidence_dir": str(args.evidence_dir.resolve()),
                "records": frozen_records,
            }
            environment._atomic_json_write(args.output_manifest.resolve(), result, refuse_existing=True)
        else:
            result = run_talk_evidence_verification(args)
    except (FileExistsError, OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Voice barge-in preflight failed: {exc}", file=sys.stderr)
        return 1
    print(f"Voice barge-in {result['kind']} passed for {result['env_id']}: {args.output_manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
