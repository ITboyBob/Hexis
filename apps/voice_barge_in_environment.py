"""Prepare and use an explicitly owned voice barge-in test environment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import signal
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.error import URLError
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import quote
from pathlib import Path
from typing import Any, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DEFAULT = REPOSITORY_ROOT / "config/voice-barge-in/run-profile.json"
COMPOSE_FILE = REPOSITORY_ROOT / "ops/compose.voice-barge-in-test.yml"
ENV_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
PROJECT_PREFIX = "hexis-"
FINGERPRINT_PATHS = (
    "hexis-ui/package.json",
    "hexis-ui/package-lock.json",
    "hexis-ui/scripts/prepare-voice-vad-assets.mjs",
    "hexis-ui/lib/voice-realtime/speech-detection-config.json",
    "hexis-ui/lib/voice-realtime/vad-assets-manifest.json",
    "ops/Dockerfile.ui",
    "ops/Dockerfile.db",
    "config/voice-barge-in/run-profile.json",
    "config/voice-barge-in/playback-coordination.json",
    "config/voice-barge-in/audio-cases.json",
    "ops/compose.voice-barge-in-test.yml",
    "apps/voice_barge_in_environment.py",
    "hexis-ui/app/api/voice-barge-in/runtime-identity/route.ts",
)
REQUIRED_DATABASE_EXTENSIONS = {"age", "vector", "http", "btree_gist", "pg_trgm", "pgcrypto", "pgtap"}
EXPECTED_SPRINT_USAGE = {
    "S1": ["inspection"],
    "S2": ["inspection", "automation"],
    "S3": [],
    "S4": ["inspection"],
    "S5": ["automation"],
    "S6": ["inspection"],
}
M6_SESSION_EXECUTION_POLICY = {
    "policy_id": "m6-c5-realtime-only-v1",
    "background_workers": "disabled",
    "business_tools": "permission_denied",
    "voice_recall": "permission_denied",
    "hydration": "database_only_no_recall",
    "emotion": "permission_denied",
    "external_requests": "selected_c5_realtime_only",
    "schema_migrations": "verify_committed_applied_only",
}


def m6_migration_source_manifest(repository_root: Path = REPOSITORY_ROOT) -> dict[str, Any]:
    """只选择 HEAD 已提交迁移；未授权文件仅记录路径，不读取 SQL。"""
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD", "--", "db/migrations"],
        cwd=repository_root, capture_output=True, text=True, check=True,
    )
    relative_paths = sorted(line for line in result.stdout.splitlines() if line.endswith(".sql"))
    if not relative_paths:
        raise ValueError("M6 migration verification requires committed repository migration sources")
    committed = {}
    for relative in relative_paths:
        path = (repository_root / relative).resolve()
        if (not path.is_relative_to((repository_root / "db/migrations").resolve())
                or not path.is_file() or not re.fullmatch(r"[0-9]{4}_.+\.sql", path.name)):
            raise ValueError("committed M6 migration source is missing or invalid")
        committed[relative] = hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()
    excluded = sorted(str(path.relative_to(repository_root)) for path in (repository_root / "db/migrations").glob("*.sql") if str(path.relative_to(repository_root)) not in committed)
    return {"mode": "committed_applied_only", "committed_files": committed, "excluded_files": excluded}


def validate_m6_migration_verification(verification: Any, repository_root: Path = REPOSITORY_ROOT) -> dict[str, Any]:
    """回读只读 schema 核验事实与已选择源字节，不打开数据库或外来 SQL。"""
    if not isinstance(verification, dict) or verification.get("status") != "verified_read_only" or verification.get("mode") != "committed_applied_only":
        raise ValueError("M6 migration provenance must be verified read-only against committed sources")
    committed = verification.get("committed_files")
    if not isinstance(committed, dict) or not committed:
        raise ValueError("M6 migration provenance has no committed source files")
    for relative, digest in committed.items():
        if not isinstance(relative, str) or not relative.startswith("db/migrations/"):
            raise ValueError("M6 migration provenance source path is invalid")
        path = (repository_root / relative).resolve()
        if (not path.is_relative_to((repository_root / "db/migrations").resolve()) or not path.is_file()
                or hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest() != digest):
            raise ValueError("M6 committed migration source changed after read-only verification")
    excluded = sorted(str(path.relative_to(repository_root)) for path in (repository_root / "db/migrations").glob("*.sql") if str(path.relative_to(repository_root)) not in committed)
    if (verification.get("excluded_files") != excluded or verification.get("ledger_count") != len(committed)
            or verification.get("ledger_latest_version") != max(Path(relative).stem for relative in committed)
            or verification.get("ledger_table") not in {"public.schema_migrations", "ag_catalog.schema_migrations"}):
        raise ValueError("M6 migration ledger/source identity or excluded-file inventory is inconsistent")
    return verification


def session_execution_policy(profile: dict[str, Any]) -> dict[str, str]:
    """拒绝缺失或放宽的验收执行策略；只返回明确选择的 C6 策略。"""
    policy = profile.get("session_execution_policy")
    if policy != M6_SESSION_EXECUTION_POLICY:
        raise ValueError("C6 session_execution_policy must select m6-c5-realtime-only-v1 without widened permissions")
    return dict(policy)


def selected_ui_origin(profile: dict[str, Any], requested_origin: str | None = None) -> str:
    """UI origin 来自 C6，或本次 owned API 的明确端口选择。"""
    parsed = urlparse(requested_origin if requested_origin is not None else profile["ui_url"])
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or requested_origin is not None and parsed.path not in {"", "/"}):
        raise ValueError("selected UI origin must be an explicit local HTTP origin without credentials or a path")
    port = parsed.port or 80
    _validate_port(port)
    authority = parsed.hostname if port == 80 else f"{parsed.hostname}:{port}"
    return f"{parsed.scheme}://{authority}"


def selected_api_url(profile: dict[str, Any], requested_port: int | None = None) -> str:
    """保持 C6 的 loopback 主机，只允许本次启动明确选择不同端口。"""
    parsed = urlparse(profile["api_url"])
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise ValueError("selected API endpoint must be a local HTTP origin without credentials or a path")
    port = (parsed.port or 80) if requested_port is None else requested_port
    if type(port) is not int:
        raise ValueError("selected API port must be an integer")
    _validate_port(port)
    authority = parsed.hostname if port == 80 else f"{parsed.hostname}:{port}"
    return f"http://{authority}"


def validate_api_start_endpoint(profile: dict[str, Any], start_record: dict[str, Any]) -> str:
    """UI、probe 与 API 策略共同核验 owned startup 的端口选择。"""
    source = start_record.get("api_url_source", "C6_api_url")
    endpoint = start_record.get("endpoints", {})
    if source not in {"C6_api_url", "start_api_cli"}:
        raise ValueError("selected API endpoint has no recognized startup selection source")
    selected = selected_api_url(profile, endpoint.get("api_port") if source == "start_api_cli" else None)
    parsed = urlparse(selected)
    if (start_record.get("browser_api_url") != selected
            or endpoint.get("api_host") != "127.0.0.1"
            or endpoint.get("api_port") != (parsed.port or 80)):
        raise ValueError("selected API endpoint does not match its explicit startup selection")
    return selected


def cache_root_default(environment: dict[str, str] | None = None) -> Path:
    values = os.environ if environment is None else environment
    cache_home = values.get("XDG_CACHE_HOME")
    return Path(cache_home).expanduser() / "hexis" if cache_home else Path.home() / ".cache/hexis"


def validate_environment_id(environment_id: str) -> str:
    if not ENV_ID_PATTERN.fullmatch(environment_id):
        raise ValueError("env-id must use lowercase letters, digits, and hyphens; path separators are forbidden")
    return environment_id


def derive_project_name(environment_id: str) -> str:
    return PROJECT_PREFIX + validate_environment_id(environment_id)


def validate_source_container_metadata(
    container: dict[str, Any],
    *,
    expected_container_id: str,
    expected_database: str,
    expected_image_id: str,
) -> dict[str, Any]:
    if container.get("Id") != expected_container_id:
        raise ValueError("source container ID changed since the selected instance was identified")
    if container.get("Image") != expected_image_id:
        raise ValueError("source container image ID changed since the selected instance was identified")
    if not container.get("State", {}).get("Running") or container.get("State", {}).get("Health", {}).get("Status") != "healthy":
        raise ValueError("source Hexis database container must remain running and healthy during the read-only clone")
    labels = container.get("Config", {}).get("Labels") or {}
    if labels.get("com.docker.compose.project") != "hexis" or labels.get("com.docker.compose.service") != "db":
        raise ValueError("selected source is not the configured Hexis main database service")
    environment = dict(
        entry.split("=", 1)
        for entry in container.get("Config", {}).get("Env", [])
        if "=" in entry
    )
    if environment.get("POSTGRES_DB") != expected_database:
        raise ValueError("source database does not match the configured primary database")
    data_volumes = [
        mount.get("Name")
        for mount in container.get("Mounts", [])
        if mount.get("Destination") == "/var/lib/postgresql/data" and mount.get("Name")
    ]
    if len(data_volumes) != 1:
        raise ValueError("source database must have one explicit named data volume")
    return {
        "container_id": container["Id"],
        "database": expected_database,
        "image_id": container["Image"],
        "source_project": labels["com.docker.compose.project"],
        "source_service": labels["com.docker.compose.service"],
        "data_volume": data_volumes[0],
    }


def validate_profile(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, dict) or profile.get("schema_version") != 2:
        raise ValueError("run profile must use schema_version 2")
    if "database_url_env" in profile:
        raise ValueError("legacy database_url_env is not supported in schema_version 2")
    if profile.get("path_base") != "repository_root":
        raise ValueError("run profile path_base must be repository_root")
    session_execution_policy(profile)
    if profile.get("database") != {
        "pytest_service": "pytest-db",
        "talk_service": "talk-db",
        "user_env": "POSTGRES_USER",
        "password_env": "POSTGRES_PASSWORD",
        "admin_database": "postgres",
    }:
        raise ValueError("database profile must explicitly select isolated pytest-db and talk-db services")
    if profile.get("runtime_environment") != {
        "api_environment_record_env": "HEXIS_VOICE_BARGE_IN_ENV",
        "api_start_record_env": "HEXIS_VOICE_BARGE_IN_API_START_RECORD",
        "ui_database_url_env": "HEXIS_DATABASE_URL",
        "ui_database_url_compat_env": "DATABASE_URL",
        "ui_api_url_env": "HEXIS_API_URL",
    }:
        raise ValueError("runtime_environment does not match the explicit M6 profile contract")
    browser = profile.get("browser")
    if not isinstance(browser, dict) or browser.get("inspection") != "in_app" or browser.get("automation") != "playwright_chrome" or browser.get("headless") is not False:
        raise ValueError("browser profile must require in_app inspection and visible Playwright Chrome")
    if browser.get("sprint_usage") != EXPECTED_SPRINT_USAGE:
        raise ValueError("browser sprint_usage mapping is incomplete or unsupported")
    for key in ("realtime_config", "runtime_config", "detection_config", "asset_manifest", "playback_coordination_config", "audio_case_spec"):
        if not isinstance(profile.get(key), str) or not profile[key]:
            raise ValueError(f"run profile is missing {key}")
    return profile


def load_profile(profile_path: Path | str = PROFILE_DEFAULT) -> dict[str, Any]:
    try:
        profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read run profile {profile_path}: {exc}") from exc
    return validate_profile(profile)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprints(repository_root: Path, paths: Sequence[str] | None = None) -> dict[str, str]:
    result = {}
    for relative in (FINGERPRINT_PATHS if paths is None else paths):
        path = repository_root / relative
        result[relative] = _sha256(path) if path.is_file() else "missing"
    return result


def build_environment_record(
    *,
    environment_id: str,
    repository_root: Path,
    head: str,
    profile: dict[str, Any],
    owned_resources: dict[str, Any],
    environment: dict[str, str],
) -> dict[str, Any]:
    """Create an E1 record containing identity only, never credential values."""
    validate_environment_id(environment_id)
    profile_bytes = json.dumps(profile, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return {
        "schema_version": 1,
        "env_id": environment_id,
        "repository_root": str(repository_root.resolve()),
        "head": head,
        "content_fingerprints": _fingerprints(repository_root),
        "profile_fingerprint": hashlib.sha256(profile_bytes).hexdigest(),
        "credential_env_names": sorted(name for name in ("POSTGRES_USER", "POSTGRES_PASSWORD") if name in environment),
        "owned_resources": owned_resources,
        "db_identity": {},
        "status": "prepared",
    }


def build_compose_command(environment_id: str, *arguments: str) -> list[str]:
    project_name = derive_project_name(environment_id)
    return ["docker", "compose", "--project-name", project_name, "--file", str(COMPOSE_FILE), *arguments]


def build_pytest_environment(
    parent_environment: dict[str, str], database_identity: dict[str, Any]
) -> dict[str, str]:
    username = parent_environment.get("POSTGRES_USER")
    password = parent_environment.get("POSTGRES_PASSWORD")
    if not username or not password:
        raise ValueError("POSTGRES_USER and POSTGRES_PASSWORD must be present in the selected environment")
    host_port = int(database_identity["host_port"])
    if not 1 <= host_port <= 65535:
        raise ValueError("selected pytest database host port is invalid")
    env = dict(parent_environment)
    env.update({
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": str(host_port),
        "POSTGRES_DB": "postgres",
        "POSTGRES_ADMIN_DB": "postgres",
    })
    return env


def _validate_port(port: int) -> None:
    if not 1 <= int(port) <= 65535:
        raise ValueError(f"port must be between 1 and 65535: {port}")


def build_worker_environment(parent_environment: dict[str, str], *, port: int) -> dict[str, str]:
    _validate_port(port)
    safe_host_settings = {"PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "TZ", "TERM", "NO_COLOR"}
    worker_environment = {
        key: value
        for key, value in parent_environment.items()
        if key in safe_host_settings
    }
    worker_environment.update({
        "HEXIS_VAD_TEST_HARNESS": "1",
        "HEXIS_UI_TEST_TARGET": "worker",
        "PORT": str(port),
        "NEXT_TELEMETRY_DISABLED": "1",
    })
    return worker_environment


def build_api_environment(
    parent_environment: dict[str, str], *, provider_key_name: str | None = None
) -> dict[str, str]:
    """仅允许本地运行设置与选定数据库凭据进入 M6 API。"""
    missing_credentials = [
        name for name in ("POSTGRES_USER", "POSTGRES_PASSWORD")
        if not parent_environment.get(name)
    ]
    if missing_credentials:
        raise ValueError(
            "selected clone database credential environment variables must be present before API startup: "
            + ", ".join(missing_credentials)
        )
    allowed_settings = {
        "PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "LC_CTYPE",
        "TZ", "TERM", "NO_COLOR", "VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH",
        "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX",
        "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONHASHSEED", "XDG_CACHE_HOME",
        "POSTGRES_USER", "POSTGRES_PASSWORD",
    }
    result = {
        key: value
        for key, value in parent_environment.items()
        if key in allowed_settings
    }
    if provider_key_name:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", provider_key_name):
            raise ValueError("selected provider credential must be an explicit environment variable name")
        provider_key = parent_environment.get(provider_key_name)
        if not provider_key:
            raise ValueError(f"selected provider credential environment variable is not present: {provider_key_name}")
        result[provider_key_name] = provider_key
    return result


def validate_selected_provider_key_env(
    profile: dict[str, Any], requested_name: str | None, parent_environment: dict[str, str]
) -> str | None:
    if requested_name is None:
        return None
    config_path = (REPOSITORY_ROOT / profile["realtime_config"]).resolve()
    if not config_path.is_relative_to(REPOSITORY_ROOT) or not config_path.is_file():
        raise ValueError(f"selected realtime configuration is missing: {profile['realtime_config']}")
    try:
        realtime = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("selected realtime configuration is invalid JSON") from exc
    configured_name = realtime.get("api_key_env") if isinstance(realtime, dict) else None
    if requested_name != configured_name:
        raise ValueError("provider key environment name must exactly match C5 api_key_env")
    if not parent_environment.get(requested_name):
        raise ValueError(f"selected provider credential environment variable is not present: {requested_name}")
    return requested_name


def build_talk_ui_environment(
    parent_environment: dict[str, str],
    *,
    environment_id: str,
    environment_record_path: Path,
    environment_record: dict[str, Any],
    profile: dict[str, Any],
    port: int,
    dist_dir: str,
    api_start: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Build a Talk UI environment bound only to the selected E1 clone and API."""
    validate_environment_id(environment_id)
    _validate_port(port)
    if environment_record.get("env_id") != environment_id:
        raise ValueError("selected E1 environment identity does not match the Talk UI")
    identity = environment_record.get("db_identity", {}).get("talk")
    owned = environment_record.get("owned_resources", {})
    if not identity or identity.get("database") != owned.get("talk_database"):
        raise ValueError("selected E1 talk database identity is missing or mismatched")
    if int(identity.get("host_port", 0)) != int(owned.get("talk_host_port", -1)):
        raise ValueError("selected E1 talk host port does not match its live identity")
    username = parent_environment.get("POSTGRES_USER")
    password = parent_environment.get("POSTGRES_PASSWORD")
    if not username or not password:
        raise ValueError("POSTGRES_USER and POSTGRES_PASSWORD for the selected clone are required")
    relative_dist = Path(dist_dir)
    if relative_dist.is_absolute() or ".." in relative_dist.parts or not relative_dist.parts or relative_dist.parts[0] != ".next":
        raise ValueError("Talk UI dist directory must be a selected child of .next")
    api_url = validate_api_start_endpoint(profile, api_start) if api_start is not None else selected_api_url(profile)
    database_url = (
        f"postgresql://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@127.0.0.1:{int(owned['talk_host_port'])}/{quote(str(identity['database']), safe='')}"
    )
    safe_settings = {
        "PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "LC_CTYPE",
        "TZ", "TERM", "NO_COLOR", "XDG_CACHE_HOME",
    }
    result = {key: value for key, value in parent_environment.items() if key in safe_settings}
    result.update({
        "NODE_ENV": "development",
        "HEXIS_UI_TEST_TARGET": "talk",
        "HEXIS_UI_DIST_DIR": relative_dist.as_posix(),
        "HEXIS_VOICE_BARGE_IN_ENV": str(environment_record_path.resolve()),
        "HEXIS_DATABASE_URL": database_url,
        "DATABASE_URL": database_url,
        "HEXIS_API_URL": api_url,
        "HEXIS_BROWSER_API_URL": api_url.rstrip("/"),
        "HEXIS_UI_ORIGIN": selected_ui_origin(profile, f"{urlparse(profile['ui_url']).scheme}://{urlparse(profile['ui_url']).hostname}:{port}"),
        "PORT": str(port),
        "NEXT_TELEMETRY_DISABLED": "1",
    })
    return result


def load_owned_environment_record(record_path: Path | str) -> tuple[Path, dict[str, Any]]:
    selected_path = Path(record_path).expanduser().resolve()
    if selected_path.name != "environment.json" or selected_path.parent.parent.name != "environments" or selected_path.parent.parent.parent.name != "voice-barge-in":
        raise ValueError("E1 record path does not match the owned environment layout")
    environment_id = validate_environment_id(selected_path.parent.name)
    cache_root = selected_path.parent.parent.parent.parent
    expected_path = _environment_record_path(cache_root, environment_id).resolve()
    if selected_path != expected_path:
        raise ValueError("E1 record path is not canonical")
    return _read_environment_record(cache_root, environment_id)


def runtime_dsn_from_environment_record(
    record_path: Path | str, environment: dict[str, str]
) -> tuple[str, dict[str, Any]]:
    _, record = load_owned_environment_record(record_path)
    identity = record.get("db_identity", {}).get("talk")
    if not identity:
        raise ValueError("selected E1 talk database identity has not been verified")
    env_names = set(record.get("credential_env_names", []))
    username_name = "POSTGRES_USER"
    password_name = "POSTGRES_PASSWORD"
    if not {username_name, password_name} <= env_names:
        raise ValueError("selected E1 does not declare both required database credential environment variables")
    username = environment.get(username_name)
    password = environment.get(password_name)
    if not username or not password:
        raise ValueError("selected database credential environment variables are missing")
    port = int(record["owned_resources"]["talk_host_port"])
    database = identity["database"]
    dsn = f"postgresql://{quote(username, safe='')}:{quote(password, safe='')}@127.0.0.1:{port}/{quote(database, safe='')}"
    return dsn, record


def _atomic_json_write(path: Path, record: dict[str, Any], *, refuse_existing: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    if refuse_existing and path.exists():
        raise FileExistsError(f"refusing to overwrite existing run record: {path}")
    if refuse_existing:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def _environment_record_path(cache_root: Path, environment_id: str) -> Path:
    return cache_root / "voice-barge-in" / "environments" / environment_id / "environment.json"


def _run(command: Sequence[str], *, environment: dict[str, str] | None = None, cwd: Path = REPOSITORY_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=environment, check=True, text=True, capture_output=True)


def _inspect_source_container(container_name: str) -> dict[str, Any]:
    result = _run(["docker", "inspect", container_name])
    inspected = json.loads(result.stdout)
    if len(inspected) != 1:
        raise RuntimeError("source container inspection did not return exactly one container")
    return inspected[0]


def _source_container_environment(container: dict[str, Any]) -> dict[str, str]:
    return dict(
        entry.split("=", 1)
        for entry in container.get("Config", {}).get("Env", [])
        if "=" in entry
    )


def _image_digest_reference(image_id: str) -> str:
    inspected = json.loads(_run(["docker", "image", "inspect", image_id]).stdout)
    if len(inspected) != 1 or inspected[0].get("Id") != image_id:
        raise RuntimeError("source database image inspection did not match the selected image ID")
    digests = inspected[0].get("RepoDigests") or []
    if not digests or any("@sha256:" not in digest for digest in digests):
        raise RuntimeError("source database image has no immutable registry digest; refusing a mutable tag")
    return sorted(digests)[0]


def _source_psql_command(container_id: str, username: str, database: str, sql: str | None = None) -> list[str]:
    command = ["docker", "exec", "--interactive", "--env", f"PGUSER={username}", container_id, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", username, "-d", database, "-A", "-t", "-F", "\t"]
    if sql is not None:
        command.extend(["-c", sql])
    return command


def _target_psql_command(environment_id: str, database: str, username: str, *arguments: str) -> list[str]:
    return build_compose_command(
        environment_id,
        "exec",
        "--no-TTY",
        "--interactive",
        "--env",
        f"PGUSER={username}",
        "talk-db",
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        username,
        "-d",
        database,
        "-A",
        "-t",
        "-F",
        "\t",
        *arguments,
    )


def _database_table_row_counts(command: list[str], *, sql_input: str | None = None) -> dict[str, int]:
    result = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        input=sql_input,
        text=True,
        capture_output=True,
        check=True,
    )
    counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 3:
            counts[f"{fields[0]}.{fields[1]}"] = int(fields[2])
    return counts


def _database_copy_script(tables: list[tuple[str, str]]) -> str:
    statements = ["BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;"]
    for schema_name, table_name in tables:
        schema_identifier = schema_name.replace('"', '""')
        table_identifier = table_name.replace('"', '""')
        table_label = f"{schema_name}.{table_name}"
        table_label_literal = table_label.replace("'", "''")
        statements.append(
            "COPY (SELECT '" + table_label_literal + "'::text, '__table__'::text "
            "UNION ALL SELECT '" + table_label_literal + "'::text, to_jsonb(row_value)::text "
            f'FROM "{schema_identifier}"."{table_identifier}" AS row_value '
            "ORDER BY 1, 2) TO STDOUT;"
        )
    statements.append("COMMIT;")
    return "\n".join(statements) + "\n"


def _stream_fingerprint(command: list[str], sql_input: str, *, environment: dict[str, str] | None = None) -> tuple[str, int]:
    process = subprocess.Popen(
        command,
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.kill()
        raise RuntimeError("could not open the selected database fingerprint streams")
    digest = hashlib.sha256()
    byte_count = 0
    try:
        process.stdin.write(sql_input.encode("utf-8"))
        process.stdin.close()
        for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
            digest.update(chunk)
            byte_count += len(chunk)
        process.stderr.read()
        return_code = process.wait()
    except Exception:
        process.kill()
        process.communicate()
        raise
    if return_code:
        raise RuntimeError(f"read-only database content fingerprint failed with exit code {return_code}")
    return digest.hexdigest(), byte_count


def _database_table_names(command: list[str]) -> list[tuple[str, str]]:
    result = subprocess.run(command, cwd=REPOSITORY_ROOT, text=True, capture_output=True, check=True)
    tables = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 2:
            tables.append((fields[0], fields[1]))
    return tables


def _row_count_fingerprint(counts: dict[str, int]) -> str:
    canonical = json.dumps(sorted(counts.items()), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _database_inventory_commands(environment_id: str, container: dict[str, Any], source_role: str, source_database: str, target_role: str) -> tuple[list[str], list[str], str]:
    source_id = container["Id"]
    row_count_sql = (
        "SELECT format('SELECT %L, %L, count(*) FROM %I.%I;', n.nspname, c.relname, n.nspname, c.relname) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema','ag_catalog') "
        "AND n.nspname NOT LIKE 'pg_toast%' ORDER BY n.nspname,c.relname;\n\\gexec\n"
    )
    source = _source_psql_command(source_id, source_role, source_database)
    target = _target_psql_command(environment_id, source_database, target_role)
    return source, target, row_count_sql


def _clone_database_stream(
    *,
    source_container_id: str,
    source_database: str,
    source_role: str,
    environment_id: str,
    target_role: str,
    target_environment: dict[str, str],
) -> dict[str, Any]:
    source = subprocess.Popen(
        ["docker", "exec", "--interactive", "--env", f"PGUSER={source_role}", source_container_id, "pg_dump", "--format=custom", "--no-owner", "--no-acl", "--username", source_role, "--dbname", source_database],
        cwd=REPOSITORY_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert source.stdout is not None and source.stderr is not None
    create_target = build_compose_command(
        environment_id,
        "exec",
        "--no-TTY",
        "--env",
        f"PGUSER={target_role}",
        "talk-db",
        "createdb",
        "-U",
        target_role,
        "-T",
        "template0",
        source_database,
    )
    try:
        _run(create_target, environment=target_environment)
    except Exception:
        source.kill()
        source.communicate()
        raise

    restore = subprocess.Popen(
        build_compose_command(
            environment_id,
            "exec",
            "--no-TTY",
            "--env",
            f"PGUSER={target_role}",
            "--interactive",
            "talk-db",
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "-U",
            target_role,
            "-d",
            source_database,
        ),
        cwd=REPOSITORY_ROOT,
        env=target_environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    assert restore.stdin is not None and restore.stderr is not None
    digest = hashlib.sha256()
    byte_count = 0
    try:
        for chunk in iter(lambda: source.stdout.read(1024 * 1024), b""):
            digest.update(chunk)
            byte_count += len(chunk)
            restore.stdin.write(chunk)
        restore.stdin.close()
        source_return_code = source.wait()
        source.stderr.read()
        restore.stderr.read()
        restore_return_code = restore.wait()
    except Exception:
        source.kill()
        restore.kill()
        source.communicate()
        restore.communicate()
        raise
    if source_return_code or restore_return_code:
        raise RuntimeError(
            "read-only source dump or isolated target restore failed "
            f"(pg_dump={source_return_code}, pg_restore={restore_return_code}); "
            "inspect the owned target Compose project logs"
        )
    return {"dump_sha256": digest.hexdigest(), "dump_bytes": byte_count, "restore_status": "restored"}


def _query_source(source: dict[str, Any], username: str, database: str, sql: str) -> str:
    command = _source_psql_command(source["Id"], username, database, sql)
    return _run(command).stdout.strip()


def _query_target(environment_id: str, username: str, database: str, sql: str, environment: dict[str, str]) -> str:
    command = _target_psql_command(environment_id, database, username, "-c", sql)
    return _run(command, environment=environment).stdout.strip()


def _source_database_metadata(source: dict[str, Any], username: str, database: str) -> dict[str, Any]:
    result = _query_source(
        source,
        username,
        database,
        "SELECT current_database(), current_setting('server_version'), pg_database_size(current_database()), "
        "(SELECT count(*) FROM schema_migrations)",
    ).split("\t")
    if len(result) != 4 or result[0] != database:
        raise RuntimeError("source database identity query returned unexpected data")
    extensions = _query_source(
        source,
        username,
        database,
        "SELECT COALESCE(string_agg(extname || '=' || extversion, ',' ORDER BY extname), '') FROM pg_extension",
    ).split(",")
    if not extensions or not extensions[0]:
        raise RuntimeError("source database has no recorded PostgreSQL extensions")
    agent_status = json.loads(_query_source(source, username, database, "SELECT get_agent_status()"))
    return {
        "database": result[0],
        "server_version": result[1],
        "size_bytes": int(result[2]),
        "migration_count": int(result[3]),
        "extensions": sorted(extensions),
        "agent_status_fingerprint": hashlib.sha256(
            json.dumps(agent_status, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "agent_configured": bool(agent_status.get("configured")),
        "agent_terminated": bool(agent_status.get("terminated")),
        "consent_valid": agent_status.get("consent_status") == "consent" and bool(agent_status.get("consent_log_id")),
    }


def _verify_restored_database(
    *,
    source: dict[str, Any],
    source_user: str,
    database: str,
    environment_id: str,
    target_user: str,
    target_environment: dict[str, str],
) -> dict[str, Any]:
    source_counts_command, target_counts_command, row_count_sql = _database_inventory_commands(
        environment_id, source, source_user, database, target_user
    )
    table_names_sql = (
        "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema','ag_catalog') "
        "AND n.nspname NOT LIKE 'pg_toast%' ORDER BY n.nspname,c.relname"
    )
    source_tables = _database_table_names(_source_psql_command(source["Id"], source_user, database, table_names_sql))
    target_tables = _database_table_names(_target_psql_command(environment_id, database, target_user, "-c", table_names_sql))
    if source_tables != target_tables:
        raise RuntimeError("restored talk database table inventory does not match the source snapshot")
    data_script = _database_copy_script(source_tables)
    source_data_fingerprint, source_data_bytes = _stream_fingerprint(
        _source_psql_command(source["Id"], source_user, database),
        data_script,
    )
    target_data_fingerprint, target_data_bytes = _stream_fingerprint(
        _target_psql_command(environment_id, database, target_user),
        data_script,
        environment=target_environment,
    )
    if source_data_fingerprint != target_data_fingerprint or source_data_bytes != target_data_bytes:
        raise RuntimeError("restored talk database full-table content fingerprint differs from the source snapshot")
    source_counts = _database_table_row_counts(source_counts_command, sql_input=row_count_sql)
    target_counts = _database_table_row_counts(target_counts_command, sql_input=row_count_sql)
    if source_counts != target_counts:
        raise RuntimeError("restored talk database table row counts do not match the read-only source snapshot")
    target_metadata = _query_target(
        environment_id,
        target_user,
        database,
        "SELECT current_database(), current_setting('server_version'), pg_database_size(current_database()), "
        "(SELECT count(*) FROM schema_migrations)",
        target_environment,
    ).split("\t")
    if len(target_metadata) != 4 or target_metadata[0] != database:
        raise RuntimeError("restored talk database identity query returned unexpected data")
    if int(target_metadata[3]) != int(_query_source(source, source_user, database, "SELECT count(*) FROM schema_migrations")):
        raise RuntimeError("restored talk database migration history differs from the source snapshot")
    target_extensions = _query_target(
        environment_id,
        target_user,
        database,
        "SELECT COALESCE(string_agg(extname || '=' || extversion, ',' ORDER BY extname), '') FROM pg_extension",
        target_environment,
    ).split(",")
    source_extensions = _query_source(
        source,
        source_user,
        database,
        "SELECT COALESCE(string_agg(extname || '=' || extversion, ',' ORDER BY extname), '') FROM pg_extension",
    ).split(",")
    if sorted(target_extensions) != sorted(source_extensions):
        raise RuntimeError("restored talk database PostgreSQL extensions differ from the source snapshot")
    source_agent = _query_source(source, source_user, database, "SELECT get_agent_status()")
    target_agent = _query_target(environment_id, target_user, database, "SELECT get_agent_status()", target_environment)
    source_agent_status = json.loads(source_agent)
    target_agent_status = json.loads(target_agent)
    source_agent_fingerprint = hashlib.sha256(json.dumps(source_agent_status, sort_keys=True).encode()).hexdigest()
    target_agent_fingerprint = hashlib.sha256(json.dumps(target_agent_status, sort_keys=True).encode()).hexdigest()
    if source_agent_fingerprint != target_agent_fingerprint:
        raise RuntimeError("restored talk database agent/consent status does not match the source snapshot")
    return {
        "database": database,
        "server_version": target_metadata[1],
        "size_bytes": int(target_metadata[2]),
        "migration_count": int(target_metadata[3]),
        "extensions": sorted(target_extensions),
        "table_count": len(target_counts),
        "row_count_fingerprint": _row_count_fingerprint(target_counts),
        "source_row_count_fingerprint": _row_count_fingerprint(source_counts),
        "source_data_fingerprint": source_data_fingerprint,
        "target_data_fingerprint": target_data_fingerprint,
        "data_fingerprint_bytes": source_data_bytes,
        "agent_status_fingerprint": target_agent_fingerprint,
        "agent_status_matches": True,
        "agent_configured": bool(target_agent_status.get("configured")),
        "agent_terminated": bool(target_agent_status.get("terminated")),
        "consent_valid": target_agent_status.get("consent_status") == "consent" and bool(target_agent_status.get("consent_log_id")),
    }


def _assert_port_available(port: int) -> None:
    _validate_port(port)
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"loopback port {port} is already occupied; choose an unused port") from exc


def _read_environment_record(cache_root: Path, environment_id: str) -> tuple[Path, dict[str, Any]]:
    path = _environment_record_path(cache_root, validate_environment_id(environment_id))
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read selected E1 environment record {path}: {exc}") from exc
    if not isinstance(record, dict) or record.get("env_id") != environment_id or record.get("status") not in {"prepared", "partially_verified", "ready"}:
        raise ValueError("selected E1 record does not identify a prepared environment")
    owned = record.get("owned_resources", {})
    if not isinstance(owned, dict) or owned.get("project_name") != derive_project_name(environment_id):
        raise ValueError("selected E1 project identity does not match env-id")
    return path, record


def _assert_rebind_runtime_inactive(cache_root: Path, environment_id: str, record: dict[str, Any]) -> None:
    """查真实本地进程与带 owned 标签的容器，不查询数据库。"""
    terminal = {"stopped", "already_exited", "exited_before_health", "start_failed"}
    runtime_records = []
    api_process = record.get("owned_resources", {}).get("api_process")
    if api_process:
        if not isinstance(api_process, dict):
            raise ValueError("E1 owned API process identity is invalid")
        runtime_records.append(("API", api_process, api_process))
    for path in sorted((cache_root / "voice-barge-in/ui-runs").glob("*.json")):
        try:
            ui = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot verify owned UI runtime record: {path.name}") from exc
        if not isinstance(ui, dict):
            raise ValueError(f"owned UI runtime record is not a JSON object: {path.name}")
        if ui.get("env_id") == environment_id and ui.get("kind") in {"worker_ui_run", "talk_ui_run"}:
            runtime_records.append((ui["kind"], ui, ui.get("process", {})))
    for label, lifecycle, process in runtime_records:
        if not isinstance(process, dict):
            raise ValueError(f"owned {label} process identity is invalid")
        if lifecycle.get("status") not in terminal:
            missing_container_handle = (
                label == "worker_ui_run" and lifecycle.get("target") == "standalone"
                and lifecycle.get("status") == "start_failed_resource_left_for_explicit_recovery"
                and not (lifecycle.get("container") or {}).get("id")
            )
            if not missing_container_handle:
                raise RuntimeError(f"owned {label} must be explicitly stopped before profile rebinding")
            run_id = lifecycle.get("run_id")
            if not isinstance(run_id, str) or not run_id.strip():
                raise ValueError("failed standalone Worker record has no explicit run-id for recovery verification")
            matches = _run([
                "docker", "ps", "-a", "--filter", f"label=com.hexis.voice-barge-in.env-id={environment_id}",
                "--filter", f"label=com.hexis.voice-barge-in.run-id={run_id}",
                "--filter", "label=com.hexis.voice-barge-in.mode=worker", "--format", "{{.ID}}",
            ]).stdout.strip()
            if matches:
                raise RuntimeError("failed standalone Worker still has a matching owned container; explicit recovery is required")
            # 真实 Docker 查询已证明该 env/run 的句柄不存在；保留旧 W1 状态。
        pid = process.get("pid")
        group_id = process.get("process_group_id")
        if pid is not None:
            if type(pid) is not int or pid <= 0 or group_id != pid:
                raise ValueError(f"owned {label} process identity is invalid")
            try:
                os.killpg(group_id, 0)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise RuntimeError(f"cannot prove owned {label} process group is inactive") from exc
            else:
                raise RuntimeError(f"owned {label} process group is still live; profile rebinding is blocked")
    running = _run([
        "docker", "ps", "--filter", f"label=com.hexis.voice-barge-in.env-id={environment_id}",
        "--filter", "label=com.hexis.voice-barge-in.mode=worker", "--format", "{{.ID}}",
    ]).stdout.strip()
    if running:
        raise RuntimeError("owned Worker container is still running; profile rebinding is blocked")


def _rebind_profile(args: argparse.Namespace) -> None:
    """显式重绑停机 E1 的 C6 指纹，保留克隆身份与每次绑定历史。"""
    from apps.voice_barge_in_probe import build_environment_preflight

    environment_id = validate_environment_id(args.env_id)
    record_path, record = _read_environment_record(args.cache_root, environment_id)
    original_bytes = record_path.read_bytes()
    if record.get("status") != "ready":
        raise ValueError("profile rebinding requires an E1 ready environment")
    if args.profile.resolve() != PROFILE_DEFAULT.resolve():
        raise ValueError("profile rebinding must select the current repository C6 profile")
    for name in ("expected_profile_fingerprint", "expected_clone_fingerprint"):
        if not re.fullmatch(r"[a-f0-9]{64}", str(getattr(args, name, ""))):
            raise ValueError(f"{name} must be an explicit SHA-256 fingerprint")
    profile = load_profile(args.profile)
    current_fingerprint = hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    previous_fingerprint = record.get("profile_fingerprint")
    history = record.get("profile_binding_history", [])
    if not isinstance(history, list) or any(not isinstance(entry, dict) for entry in history):
        raise ValueError("E1 profile binding history is invalid")
    repeated = previous_fingerprint == current_fingerprint
    if previous_fingerprint != args.expected_profile_fingerprint:
        if not (repeated and history and history[-1].get("old_profile_fingerprint") == args.expected_profile_fingerprint
                and history[-1].get("new_profile_fingerprint") == current_fingerprint
                and history[-1].get("clone_data_fingerprint") == args.expected_clone_fingerprint):
            raise ValueError("E1 prior profile binding does not match the explicitly selected fingerprint")
    clone = record.get("source_clone", {})
    if not isinstance(clone, dict):
        raise ValueError("E1 clone identity is not a JSON object")
    if (clone.get("source_data_fingerprint") != args.expected_clone_fingerprint
            or clone.get("target_data_fingerprint") != args.expected_clone_fingerprint
            or clone.get("agent_status_fingerprint") != clone.get("source_agent_status_fingerprint")
            or not re.fullmatch(r"[a-f0-9]{64}", str(clone.get("agent_status_fingerprint", "")))
            or clone.get("source_agent_configured") is not True
            or clone.get("source_agent_terminated") is not False
            or clone.get("source_consent_valid") is not True):
        raise ValueError("E1 clone data/agent identity or consent does not match the selected verified clone")
    previous_entry = None
    for entry in history:
        if (not all(re.fullmatch(r"[a-f0-9]{64}", str(entry.get(key, "")))
                    for key in ("old_profile_fingerprint", "new_profile_fingerprint"))
                or entry.get("old_profile_fingerprint") == entry.get("new_profile_fingerprint")
                or entry.get("clone_data_fingerprint") != args.expected_clone_fingerprint
                or entry.get("agent_status_fingerprint") != clone["agent_status_fingerprint"]
                or entry.get("profile_path") != str(args.profile.resolve())
                or (previous_entry and previous_entry["new_profile_fingerprint"] != entry["old_profile_fingerprint"])):
            raise ValueError("E1 profile binding history does not preserve the selected clone/binding chain")
        try:
            timestamp = datetime.fromisoformat(entry["rebound_at_utc"])
            if timestamp.tzinfo is None:
                raise ValueError("missing timezone")
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("E1 profile binding history timestamp is invalid") from exc
        previous_entry = entry
    if history and history[-1].get("new_profile_fingerprint") != previous_fingerprint:
        raise ValueError("E1 current profile binding does not match its history")
    # 使用同一严格 preflight 核验克隆状态、双 DB 身份、仓库和配置；
    # 这里只给内存候选换指纹，未触及缓存或数据库。
    candidate = dict(record, profile_fingerprint=current_fingerprint)
    build_environment_preflight(profile, candidate, repository_root=REPOSITORY_ROOT)
    _assert_rebind_runtime_inactive(args.cache_root, environment_id, record)
    if record_path.read_bytes() != original_bytes:
        raise RuntimeError("E1 changed during profile rebinding; repeat the explicit preflight")
    if repeated:
        print(f"E1 {environment_id} already binds the selected C6 profile; no JSON change")
        return
    candidate["profile_binding_history"] = history + [{
        "old_profile_fingerprint": previous_fingerprint,
        "new_profile_fingerprint": current_fingerprint,
        "clone_data_fingerprint": args.expected_clone_fingerprint,
        "agent_status_fingerprint": clone["agent_status_fingerprint"],
        "profile_path": str(args.profile.resolve()),
        "rebound_at_utc": datetime.now(timezone.utc).isoformat(),
    }]
    _atomic_json_write(record_path, candidate)
    print(f"Rebound only E1 {environment_id} C6 profile fingerprint; clone identity and credentials unchanged")


def _compose_environment(environment_id: str, record: dict[str, Any], base: dict[str, str] | None = None) -> dict[str, str]:
    owned = record["owned_resources"]
    environment = dict(os.environ if base is None else base)
    environment.update({
        "VOICE_BARGE_IN_ENV_ID": environment_id,
        "VOICE_BARGE_IN_PYTEST_DB_PORT": str(owned["pytest_host_port"]),
        "VOICE_BARGE_IN_TALK_DB_PORT": str(owned["talk_host_port"]),
        "VOICE_BARGE_IN_DB_IMAGE": owned["database_image_digest"],
        "POSTGRES_DB": "postgres",
    })
    return environment


def _prepare_db(args: argparse.Namespace) -> None:
    environment_id = validate_environment_id(args.env_id)
    profile = load_profile(args.profile)
    expected_project = derive_project_name(environment_id)
    if args.project_name != expected_project:
        raise ValueError(f"project-name must be derived from env-id: {expected_project}")
    if os.name == "nt":
        raise RuntimeError("this isolated Compose profile currently requires a Unix-like host")
    target_user = os.environ.get("POSTGRES_USER")
    if not target_user or not os.environ.get("POSTGRES_PASSWORD"):
        raise ValueError("set the new isolated target's POSTGRES_USER and POSTGRES_PASSWORD in the shell")
    source = _inspect_source_container(args.source_container)
    source_summary = validate_source_container_metadata(
        source,
        expected_container_id=args.source_container_id,
        expected_database=args.source_database,
        expected_image_id=args.source_image_id,
    )
    source_environment = _source_container_environment(source)
    source_user = source_environment.get("POSTGRES_USER")
    if not source_user:
        raise ValueError("selected source container does not declare its database role")
    if target_user == source_user:
        raise ValueError("target database role must be new and independent from the source role")
    image_reference = _image_digest_reference(args.source_image_id)
    source_database = _source_database_metadata(source, source_user, args.source_database)
    _assert_port_available(args.pytest_db_port)
    _assert_port_available(args.talk_db_port)
    record_path = _environment_record_path(args.cache_root, environment_id)
    if record_path.exists():
        raise FileExistsError(f"E1 record already exists; choose a new env-id: {record_path}")
    compose_environment = dict(os.environ)
    compose_environment.update({
        "VOICE_BARGE_IN_ENV_ID": environment_id,
        "VOICE_BARGE_IN_PYTEST_DB_PORT": str(args.pytest_db_port),
        "VOICE_BARGE_IN_TALK_DB_PORT": str(args.talk_db_port),
        "VOICE_BARGE_IN_DB_IMAGE": image_reference,
        "POSTGRES_DB": "postgres",
    })
    project_list = _run(["docker", "compose", "ls", "--all", "--format", "json"], environment=compose_environment)
    if expected_project in project_list.stdout:
        raise RuntimeError(f"Compose project {expected_project} already exists; refusing to reuse or mutate it")
    volumes = _run(["docker", "volume", "ls", "--filter", f"label=com.docker.compose.project={expected_project}", "--format", "{{.Name}}"], environment=compose_environment)
    if volumes.stdout.strip():
        raise RuntimeError(f"Compose volumes already exist for {expected_project}; refusing to reuse them")
    containers = _run(["docker", "ps", "--all", "--filter", f"label=com.docker.compose.project={expected_project}", "--format", "{{.ID}}"], environment=compose_environment)
    if containers.stdout.strip():
        raise RuntimeError(f"Compose containers already exist for {expected_project}; refusing to reuse them")
    target_image_id = _run(["docker", "image", "inspect", image_reference, "--format", "{{.Id}}"], environment=compose_environment).stdout.strip()
    if target_image_id != args.source_image_id:
        raise RuntimeError("verified source digest does not resolve to the selected source image ID")
    owned_resources = {
        "compose_project": expected_project,
        "project_name": expected_project,
        "pytest_service": profile["database"]["pytest_service"],
        "talk_service": profile["database"]["talk_service"],
        "pytest_host_port": args.pytest_db_port,
        "talk_host_port": args.talk_db_port,
        "pytest_database": "postgres",
        "talk_database": args.source_database,
        "volumes": [f"{expected_project}_pytest_data", f"{expected_project}_talk_data"],
        "database_image_id": target_image_id,
        "database_image_digest": image_reference,
    }
    head_result = _run(["git", "rev-parse", "HEAD"], environment=compose_environment)
    record = build_environment_record(
        environment_id=environment_id,
        repository_root=REPOSITORY_ROOT,
        head=head_result.stdout.strip(),
        profile=profile,
        owned_resources=owned_resources,
        environment=compose_environment,
    )
    record["source_clone"] = {
        "status": "pending",
        **source_summary,
        "source_image_digest": image_reference,
        "source_server_version": source_database["server_version"],
        "source_size_bytes": source_database["size_bytes"],
        "source_migration_count": source_database["migration_count"],
        "source_extensions": source_database["extensions"],
        "source_agent_status_fingerprint": source_database["agent_status_fingerprint"],
        "source_agent_configured": source_database["agent_configured"],
        "source_agent_terminated": source_database["agent_terminated"],
        "source_consent_valid": source_database["consent_valid"],
    }
    record["status"] = "preparing"
    _atomic_json_write(record_path, record, refuse_existing=True)
    try:
        _run(build_compose_command(environment_id, "up", "--detach", "--wait"), environment=compose_environment)
        clone_result = _clone_database_stream(
            source_container_id=source["Id"],
            source_database=args.source_database,
            source_role=source_user,
            environment_id=environment_id,
            target_role=target_user,
            target_environment=compose_environment,
        )
        record["source_clone"].update(clone_result)
        record["source_clone"]["status"] = "restored"
        _atomic_json_write(record_path, record)
        target_identity = _verify_restored_database(
            source=source,
            source_user=source_user,
            database=args.source_database,
            environment_id=environment_id,
            target_user=target_user,
            target_environment=compose_environment,
        )
    except subprocess.CalledProcessError as exc:
        record["status"] = "failed"
        record["failure"] = {"exit_code": exc.returncode, "step": "compose_or_database_clone"}
        _atomic_json_write(record_path, record)
        raise
    except Exception:
        record["status"] = "failed"
        record["failure"] = {"step": "database_clone_or_verification"}
        _atomic_json_write(record_path, record)
        raise
    record["source_clone"].update({
        "status": "verified",
        "recovery_verification": "full_table_content_hash",
        "target_size_bytes": target_identity["size_bytes"],
        "target_migration_count": target_identity["migration_count"],
        "target_extensions": target_identity["extensions"],
        "table_count": target_identity["table_count"],
        "row_count_fingerprint": target_identity["row_count_fingerprint"],
        "source_row_count_fingerprint": target_identity["source_row_count_fingerprint"],
        "source_data_fingerprint": target_identity["source_data_fingerprint"],
        "target_data_fingerprint": target_identity["target_data_fingerprint"],
        "data_fingerprint_bytes": target_identity["data_fingerprint_bytes"],
        "agent_status_fingerprint": target_identity["agent_status_fingerprint"],
        "agent_status_matches": target_identity["agent_status_matches"],
    })
    record.setdefault("db_identity", {})["talk"] = {
        "database": target_identity["database"],
        "server_port": 5432,
        "host_port": args.talk_db_port,
        "extensions": target_identity["extensions"],
    }
    record["status"] = "prepared"
    _atomic_json_write(record_path, record)
    print(f"Prepared isolated clone environment {environment_id}; cloned DB verified, E1: {record_path}")


def _check_db(args: argparse.Namespace) -> None:
    environment_id = validate_environment_id(args.env_id)
    record_path, record = _read_environment_record(args.cache_root, environment_id)
    profile = load_profile(args.profile)
    service = profile["database"]["pytest_service" if args.scope == "pytest" else "talk_service"]
    project_name = derive_project_name(environment_id)
    compose_environment = _compose_environment(environment_id, record)
    username = compose_environment.get("POSTGRES_USER", "")
    database = record["owned_resources"]["pytest_database" if args.scope == "pytest" else "talk_database"]
    command = build_compose_command(environment_id, "exec", "--no-TTY", service, "psql", "-U", username, "-d", database, "-A", "-t", "-F", "\t", "-c", "SELECT current_database(), current_setting('port')::int, current_user")
    result = _run(command, environment=compose_environment)
    row = result.stdout.strip().split("\t")
    if len(row) != 3 or row[0] != database:
        raise RuntimeError("database identity query returned an unexpected result")
    active_extensions = REQUIRED_DATABASE_EXTENSIONS - {"pgtap"}
    extension_list = ",".join(f"'{extension}'" for extension in sorted(active_extensions))
    ext_command = build_compose_command(environment_id, "exec", "--no-TTY", service, "psql", "-U", username, "-d", database, "-A", "-t", "-c", f"SELECT string_agg(extname || '=' || extversion, ',' ORDER BY extname) FROM pg_extension WHERE extname IN ({extension_list})")
    extension_rows = _run(ext_command, environment=compose_environment).stdout.strip().split(",")
    extensions = [value.split("=", 1)[0] for value in extension_rows if value]
    missing = sorted(active_extensions - set(extensions))
    if missing:
        raise RuntimeError(f"selected {args.scope} database is missing required extensions: {', '.join(missing)}")
    pgtap = build_compose_command(environment_id, "exec", "--no-TTY", service, "psql", "-U", username, "-d", database, "-A", "-t", "-c", "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name='pgtap')::text")
    if _run(pgtap, environment=compose_environment).stdout.strip() != "true":
        raise RuntimeError(f"selected {args.scope} PostgreSQL image does not provide pgtap")
    createdb = build_compose_command(environment_id, "exec", "--no-TTY", service, "psql", "-U", username, "-d", database, "-A", "-t", "-c", "SELECT (rolsuper OR rolcreatedb)::text FROM pg_roles WHERE rolname = current_user")
    if _run(createdb, environment=compose_environment).stdout.strip() != "true":
        raise RuntimeError(f"selected {args.scope} database role does not have CREATEDB permission")
    owned = record["owned_resources"]
    expected_port = owned["pytest_host_port" if args.scope == "pytest" else "talk_host_port"]
    identity = {"database": row[0], "server_port": int(row[1]), "role": row[2], "host_port": expected_port, "extensions": sorted(extensions)}
    record.setdefault("db_identity", {})[args.scope] = identity
    record["status"] = "ready" if {"pytest", "talk"} <= (set(record.get("db_identity", {})) | {args.scope}) else "partially_verified"
    _atomic_json_write(record_path, record)
    print(f"{args.scope} database ready for {environment_id}: {identity['database']} at 127.0.0.1:{expected_port} ({project_name})")


def _verify_clone_identity(args: argparse.Namespace) -> None:
    environment_id = validate_environment_id(args.env_id)
    record_path = _environment_record_path(args.cache_root, environment_id)
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read selected clone E1 record: {exc}") from exc
    owned = record.get("owned_resources", {})
    source_clone = record.get("source_clone", {})
    if record.get("env_id") != environment_id or owned.get("project_name") != derive_project_name(environment_id):
        raise ValueError("selected E1 does not identify the owned M6 clone environment")
    if source_clone.get("container_id") != args.source_container_id or source_clone.get("database") != args.source_database:
        raise ValueError("source container or database does not match the source captured in E1")
    source = _inspect_source_container(args.source_container)
    source_summary = validate_source_container_metadata(
        source,
        expected_container_id=args.source_container_id,
        expected_database=args.source_database,
        expected_image_id=source_clone.get("image_id", ""),
    )
    if source_summary.get("data_volume") != source_clone.get("data_volume"):
        raise ValueError("source data volume identity changed since E1 was prepared")
    source_user = _source_container_environment(source).get("POSTGRES_USER")
    target_user = os.environ.get("POSTGRES_USER")
    if not source_user or not target_user or not os.environ.get("POSTGRES_PASSWORD"):
        raise ValueError("source role and target database credential variables must be available in the environment")
    if target_user == source_user:
        raise ValueError("target database role must remain independent from the source role")
    compose_environment = _compose_environment(environment_id, record)
    verification = _verify_restored_database(
        source=source,
        source_user=source_user,
        database=args.source_database,
        environment_id=environment_id,
        target_user=target_user,
        target_environment=compose_environment,
    )
    source_clone.update({
        "status": "verified",
        "recovery_verification": "full_table_content_hash",
        "recovery_time_utc": datetime.now(timezone.utc).isoformat(),
        "target_size_bytes": verification["size_bytes"],
        "target_migration_count": verification["migration_count"],
        "target_extensions": verification["extensions"],
        "table_count": verification["table_count"],
        "row_count_fingerprint": verification["row_count_fingerprint"],
        "source_row_count_fingerprint": verification["source_row_count_fingerprint"],
        "source_data_fingerprint": verification["source_data_fingerprint"],
        "target_data_fingerprint": verification["target_data_fingerprint"],
        "data_fingerprint_bytes": verification["data_fingerprint_bytes"],
        "agent_status_fingerprint": verification["agent_status_fingerprint"],
        "agent_status_matches": verification["agent_status_matches"],
    })
    if not source_clone.get("dump_sha256"):
        source_clone["dump_digest_status"] = "unavailable_from_initial_attempt; restored database verified by source-target full-table comparison"
    record.setdefault("db_identity", {})["talk"] = {
        "database": verification["database"],
        "server_port": 5432,
        "host_port": owned["talk_host_port"],
        "extensions": verification["extensions"],
    }
    record["status"] = "prepared"
    _atomic_json_write(record_path, record)
    print(f"Verified isolated clone {environment_id}: full table content and agent/consent status match the selected source")


def _start_api(args: argparse.Namespace) -> None:
    environment_id = validate_environment_id(args.env_id)
    environment_record_path, record = _read_environment_record(args.cache_root, environment_id)
    if record.get("status") != "ready" or not record.get("source_clone", {}).get("agent_status_matches"):
        raise ValueError("the isolated source clone and both database scopes must be verified before API startup")
    if os.environ.get("HEXIS_M5_ACCEPTANCE_RUN"):
        raise ValueError("HEXIS_M5_ACCEPTANCE_RUN conflicts with the selected M6 clone environment")
    profile = load_profile(args.profile)
    provider_key_name = validate_selected_provider_key_env(
        profile, getattr(args, "provider_key_env", None), dict(os.environ)
    )
    ui_origin = selected_ui_origin(profile, getattr(args, "ui_origin", None))
    browser_api_url = selected_api_url(profile, getattr(args, "api_port", None))
    api_url = urlparse(browser_api_url)
    listen_host = "127.0.0.1"
    listen_port = api_url.port or 80
    _assert_port_available(listen_port)
    record_path = args.output_start_record.resolve()
    api_runs = (args.cache_root / "voice-barge-in" / "api-runs").resolve()
    if not record_path.is_relative_to(api_runs) or record_path == api_runs:
        raise ValueError("API start record must be inside the selected cache root's api-runs directory")
    if record_path.exists():
        raise FileExistsError(f"refusing to overwrite API start record: {record_path}")
    env = _compose_environment(
        environment_id, record,
        base=build_api_environment(dict(os.environ), provider_key_name=provider_key_name),
    )
    env.update({
        "HEXIS_VOICE_BARGE_IN_ENV": str(environment_record_path),
        "HEXIS_VOICE_BARGE_IN_API_START_RECORD": str(record_path),
        "HEXIS_VOICE_BARGE_IN_LISTEN_HOST": listen_host,
        "HEXIS_VOICE_BARGE_IN_LISTEN_PORT": str(listen_port),
        "HEXIS_BROWSER_API_URL": browser_api_url,
        "HEXIS_VOICE_BARGE_IN_API_URL_SOURCE": "start_api_cli" if getattr(args, "api_port", None) is not None else "C6_api_url",
        "HEXIS_UI_ORIGIN": ui_origin,
        "HEXIS_VOICE_BARGE_IN_UI_ORIGIN_SOURCE": "start_api_cli" if getattr(args, "ui_origin", None) is not None else "C6_ui_url",
    })
    if provider_key_name:
        env["HEXIS_VOICE_BARGE_IN_PROVIDER_KEY_ENV"] = provider_key_name
    log_path = record_path.with_suffix(".log")
    if log_path.exists():
        raise FileExistsError(f"refusing to overwrite API log: {log_path}")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_descriptor = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(log_descriptor, "wb") as log_stream:
        process = subprocess.Popen(
            [sys.executable, "-m", "apps.hexis_api", "--host", listen_host, "--port", str(listen_port)],
            cwd=REPOSITORY_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    record.setdefault("owned_resources", {})["api_process"] = {
        "pid": process.pid,
        "process_group_id": process.pid,
        "log_path": str(log_path),
        "start_record_path": str(record_path),
        "status": "starting",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_json_write(environment_record_path, record)
    health_url = f"http://{listen_host}:{listen_port}/health"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            record["owned_resources"]["api_process"]["status"] = "exited_before_health"
            _atomic_json_write(environment_record_path, record)
            raise RuntimeError(f"M6 API exited before health succeeded; inspect the private run log at {log_path}")
        try:
            with urlopen(health_url, timeout=2) as response:
                health = json.loads(response.read())
            identity = health.get("voice_barge_in") or {}
            if (
                response.status == 200
                and health.get("status") == "ok"
                and identity.get("environment_id") == environment_id
                and identity.get("database") == record["owned_resources"]["talk_database"]
            ):
                start_record = json.loads(record_path.read_text(encoding="utf-8"))
                if start_record.get("environment_id") != environment_id or not start_record.get("start_id"):
                    raise RuntimeError("API start record does not match the selected E1 environment")
                if identity.get("start_id") != start_record["start_id"]:
                    raise RuntimeError("API health start identity does not match api-start.json")
                if (identity.get("session_execution_policy") != session_execution_policy(profile)
                        or start_record.get("session_execution_policy") != session_execution_policy(profile)):
                    raise RuntimeError("API health/start execution policy does not match restricted C6")
                if identity.get("migration_verification") != start_record.get("migration_verification"):
                    raise RuntimeError("API health/start read-only migration verification does not match")
                validate_m6_migration_verification(start_record.get("migration_verification"))
                if validate_api_start_endpoint(profile, start_record) != browser_api_url:
                    raise RuntimeError("API startup endpoint does not match the explicit owned launch selection")
                if start_record.get("api_url_source", "C6_api_url") != env["HEXIS_VOICE_BARGE_IN_API_URL_SOURCE"]:
                    raise RuntimeError("API startup endpoint selection source does not match its explicit launch")
                if (start_record.get("browser_api_url") != browser_api_url
                        or identity.get("browser_api_url") != start_record["browser_api_url"]):
                    raise RuntimeError("API browser-reachable URL does not match selected C6")
                if start_record.get("ui_origin") != ui_origin or identity.get("ui_origin") != ui_origin:
                    raise RuntimeError("API UI origin does not match the explicit owned launch selection")
                record["owned_resources"]["api_process"].update({
                    "status": "healthy",
                    "start_id": start_record["start_id"],
                    "api_url": browser_api_url,
                })
                _atomic_json_write(environment_record_path, record)
                print(f"M6 API healthy for {environment_id} at {browser_api_url}; pid={process.pid}; record={record_path}")
                return
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(0.25)
    record["owned_resources"]["api_process"]["status"] = "health_timeout_process_left_running"
    _atomic_json_write(environment_record_path, record)
    raise RuntimeError(
        f"M6 API health did not match the selected clone within 90 seconds; process {process.pid} remains available for inspection. "
        f"Run stop --env-id {environment_id} --target api --run-record {environment_record_path} to stop only this owned process; inspect {log_path} for diagnostics."
    )


def _stop_api(args: argparse.Namespace) -> None:
    environment_id = validate_environment_id(args.env_id)
    record_path, record = _read_environment_record(args.cache_root, environment_id)
    if args.run_record.resolve() != record_path.resolve():
        raise ValueError("API stop requires the explicit E1 record that owns the selected process")
    process = record.get("owned_resources", {}).get("api_process")
    if not process or process.get("process_group_id") != process.get("pid"):
        raise ValueError("selected E1 has no verified owned API process")
    pid = int(process["pid"])
    command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False).stdout.strip()
    if not command:
        process["status"] = "already_exited"
        _atomic_json_write(record_path, record)
        print(f"M6 API process for {environment_id} is already stopped")
        return
    if "apps.hexis_api" not in command:
        raise RuntimeError("recorded API PID now belongs to a different command; refusing to signal it")
    environment_output = subprocess.run(["ps", "eww", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False).stdout
    expected_record = f"HEXIS_VOICE_BARGE_IN_ENV={_environment_record_path(args.cache_root, environment_id)}"
    if expected_record not in environment_output:
        raise RuntimeError("recorded API process does not carry the selected E1 identity; refusing to signal it")
    if os.getpgid(pid) != int(process["process_group_id"]):
        raise RuntimeError("recorded API process group changed; refusing to signal it")
    os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            process["status"] = "stopped"
            process["stopped_at_utc"] = datetime.now(timezone.utc).isoformat()
            _atomic_json_write(record_path, record)
            print(f"Stopped only the owned M6 API process for {environment_id}")
            return
        time.sleep(0.2)
    raise RuntimeError(f"M6 API process group {pid} did not stop after SIGTERM; inspect its private log and choose the next action")


def _worker_ui_container_name(environment_id: str, run_id: str) -> str:
    run_digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
    return f"{derive_project_name(environment_id)}-worker-{run_digest}"


def _worker_ui_fingerprints() -> dict[str, str]:
    paths = (
        "hexis-ui/package.json",
        "hexis-ui/package-lock.json",
        "hexis-ui/next.config.ts",
        "hexis-ui/app/layout.tsx",
        "hexis-ui/app/components/nav/shell.tsx",
        "hexis-ui/app/components/pwa/pwa-client.tsx",
        "hexis-ui/lib/voice-realtime/speech-detection-config.json",
        "hexis-ui/lib/voice-realtime/vad-assets-manifest.json",
        "hexis-ui/lib/voice-realtime/speech-detector.ts",
        "hexis-ui/lib/voice-realtime/speech-detector.worker.ts",
        "hexis-ui/app/voice-vad-test/page.tsx",
        "hexis-ui/app/voice-vad-test/worker-harness.tsx",
        "hexis-ui/e2e/voice-vad-worker.spec.ts",
    )
    return _fingerprints(REPOSITORY_ROOT, paths)


def _talk_ui_fingerprints() -> dict[str, str]:
    paths = (
        "hexis-ui/package.json",
        "hexis-ui/package-lock.json",
        "hexis-ui/next.config.ts",
        "hexis-ui/app/layout.tsx",
        "hexis-ui/app/chat/talk-mode.tsx",
        "hexis-ui/lib/prisma.ts",
        "hexis-ui/lib/voice-realtime/client.ts",
        "hexis-ui/lib/voice-realtime/capture.ts",
        "hexis-ui/lib/voice-realtime/speech-detection-config.json",
        "hexis-ui/lib/voice-realtime/vad-assets-manifest.json",
        "hexis-ui/lib/voice-playback/native-player.ts",
        "hexis-ui/app/api/voice-barge-in/runtime-identity/route.ts",
        "config/voice-barge-in/run-profile.json",
        "config/voice-barge-in/playback-coordination.json",
        "config/voice-barge-in/audio-cases.json",
    )
    return _fingerprints(REPOSITORY_ROOT, paths)


def _wait_for_worker_page(url: str, *, process: subprocess.Popen[Any] | None, container_id: str | None, timeout_seconds: float = 60) -> int:
    deadline = time.monotonic() + timeout_seconds
    last_status: int | None = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Worker UI process exited before the test page was ready (exit {process.returncode}); inspect its private W1 log")
        if container_id is not None:
            state = _run(["docker", "inspect", "--format", "{{.State.Status}}", container_id]).stdout.strip()
            if state in {"exited", "dead", "removing"}:
                raise RuntimeError(f"Worker UI container exited before the test page was ready ({state})")
        try:
            with urlopen(url, timeout=1) as response:
                last_status = response.status
                if response.status == 200:
                    return response.status
        except Exception as exc:
            status = getattr(exc, "code", None)
            if status is not None:
                last_status = int(status)
        time.sleep(0.25)
    raise RuntimeError(f"Worker test page did not become available at {url} within {timeout_seconds:g}s (last HTTP status: {last_status})")


def _start_worker_ui(args: argparse.Namespace) -> None:
    environment_id = validate_environment_id(args.env_id)
    if args.mode != "worker":
        raise ValueError("talk UI startup is reserved for Task 5.3 after API/database identity verification")
    profile = load_profile(args.profile)
    public_url = urlparse(profile["ui_url"])
    if public_url.scheme != "http" or public_url.hostname != "127.0.0.1":
        raise ValueError("worker UI must use an explicitly selected loopback URL")
    port = args.port if args.port is not None else (public_url.port or 3477)
    _assert_port_available(port)
    output_record = args.output_record.resolve()
    ui_runs_root = (args.cache_root / "voice-barge-in" / "ui-runs").resolve()
    if not output_record.is_relative_to(ui_runs_root) or output_record == ui_runs_root:
        raise ValueError("W1/U1 output must be inside the selected cache root's ui-runs directory")
    if not output_record.name.endswith("-worker.json"):
        raise ValueError("worker UI output record filename must end in -worker.json")
    if output_record.exists():
        raise FileExistsError(f"refusing to overwrite existing UI run record: {output_record}")
    run_id = output_record.stem
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", run_id):
        raise ValueError("worker UI run id derived from output filename contains unsupported characters")
    log_path = output_record.with_suffix(".log")
    if log_path.exists():
        raise FileExistsError(f"refusing to overwrite existing UI run log: {log_path}")
    fingerprints = _worker_ui_fingerprints()
    worker_env = build_worker_environment(os.environ.copy(), port=port)
    if args.target == "dev":
        run_digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
        worker_env["HEXIS_UI_DIST_DIR"] = f".next/voice-barge-in-{run_digest}"
    process: subprocess.Popen[Any] | None = None
    container_id: str | None = None
    ui_target = "dev" if args.target == "dev" else "standalone"
    run_record: dict[str, Any] = {
        "schema_version": 1,
        "kind": "worker_ui_run",
        "run_id": run_id,
        "env_id": environment_id,
        "mode": "worker",
        "target": ui_target,
        "ui_port": port,
        "ui_url": f"http://127.0.0.1:{port}/voice-vad-test",
        "runtime_flag": "HEXIS_VAD_TEST_HARNESS=1",
        "source_root": str(REPOSITORY_ROOT),
        "source_head": _run(["git", "rev-parse", "HEAD"]).stdout.strip(),
        "source_fingerprints": fingerprints,
        "model_resource_fingerprint": fingerprints.get("hexis-ui/lib/voice-realtime/vad-assets-manifest.json"),
        "credential_env_names": [],
        "network_services": [],
        "status": "starting",
        "log_path": str(log_path),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if args.target == "dev":
        run_record["dev_dist_dir"] = worker_env["HEXIS_UI_DIST_DIR"]
    _atomic_json_write(output_record, run_record, refuse_existing=True)
    try:
        if args.target == "dev":
            log_descriptor = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(log_descriptor, "wb") as log_stream:
                process = subprocess.Popen(
                    [str(REPOSITORY_ROOT / "hexis-ui/node_modules/.bin/next"), "dev", "--hostname", "127.0.0.1", "--port", str(port)],
                    cwd=REPOSITORY_ROOT / "hexis-ui",
                    env=worker_env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            run_record["process"] = {"pid": process.pid, "process_group_id": process.pid}
        else:
            image = f"hexis-ui-voice-barge-in-w1:{run_id}"
            source_head = run_record["source_head"]
            _run([
                "docker", "build", "--quiet", "--file", "ops/Dockerfile.ui", "--tag", image,
                "--label", f"com.hexis.voice-barge-in.w1-run-id={run_id}",
                "--label", f"com.hexis.voice-barge-in.source-head={source_head}",
                ".",
            ])
            image_id = _run(["docker", "image", "inspect", image, "--format", "{{.Id}}"] ).stdout.strip()
            container_name = _worker_ui_container_name(environment_id, run_id)
            labels = [
                "--label", f"com.hexis.voice-barge-in.env-id={environment_id}",
                "--label", f"com.hexis.voice-barge-in.run-id={run_id}",
                "--label", "com.hexis.voice-barge-in.mode=worker",
            ]
            command = [
                "docker", "run", "--detach", "--pull", "never", "--name", container_name,
                *labels,
                "--publish", f"127.0.0.1:{port}:3477",
                "--env", "HEXIS_VAD_TEST_HARNESS=1",
                "--env", "HEXIS_UI_TEST_TARGET=worker",
                "--env", "PORT=3477",
                "--env", "NEXT_TELEMETRY_DISABLED=1",
                image,
            ]
            container_id = _run(command).stdout.strip()
            run_record["build_fingerprint"] = hashlib.sha256(
                json.dumps({"source_fingerprints": fingerprints, "image_id": image_id}, sort_keys=True).encode("utf-8")
            ).hexdigest()
            run_record["container"] = {"id": container_id, "name": container_name, "image": image, "image_id": image_id}
        run_record["status"] = "starting"
        _atomic_json_write(output_record, run_record)
        http_status = _wait_for_worker_page(run_record["ui_url"], process=process, container_id=container_id)
        run_record["status"] = "ready"
        run_record["runtime_http_status"] = http_status
        run_record["ready_at_utc"] = datetime.now(timezone.utc).isoformat()
        _atomic_json_write(output_record, run_record)
        print(f"Worker UI ready at {run_record['ui_url']} ({ui_target}); W1: {output_record}")
    except Exception:
        if container_id is not None and not log_path.exists():
            try:
                diagnostic = _run(["docker", "logs", "--tail", "100", container_id]).stdout
                descriptor = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as log_stream:
                    log_stream.write(diagnostic)
            except Exception:
                pass
        run_record["status"] = "start_failed_resource_left_for_explicit_recovery"
        _atomic_json_write(output_record, run_record)
        raise


def _selected_api_start_identity(record: dict[str, Any], profile: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    process = record.get("owned_resources", {}).get("api_process", {})
    if process.get("status") != "healthy":
        raise ValueError("selected M6 API is not healthy at the URL in the selected profile")
    start_path_value = process.get("start_record_path")
    if not start_path_value:
        raise ValueError("selected M6 API has no owned api-start record")
    start_path = Path(start_path_value).expanduser().resolve()
    expected_root = Path(record["repository_root"]).resolve()
    try:
        start_record = json.loads(start_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read selected API start record: {exc}") from exc
    if start_record.get("environment_id") != record.get("env_id") or start_record.get("start_id") != process.get("start_id"):
        raise ValueError("selected API start record does not match the E1 owned process")
    if start_record.get("session_execution_policy") != session_execution_policy(profile):
        raise ValueError("selected API start execution policy does not match restricted C6")
    api_url = validate_api_start_endpoint(profile, start_record)
    if process.get("api_url", "").rstrip("/") != api_url:
        raise ValueError("selected API process URL does not match its startup endpoint")
    if (selected_ui_origin(profile, start_record.get("ui_origin")) != start_record.get("ui_origin")
            or start_record.get("ui_origin_source") not in {"C6_ui_url", "start_api_cli"}):
        raise ValueError("selected API has no verified owned UI origin selection")
    validate_m6_migration_verification(start_record.get("migration_verification"), expected_root)
    if Path(start_record.get("source", {}).get("repository_root", "")).resolve() != expected_root:
        raise ValueError("selected API start record points to a different source checkout")
    return start_path, start_record


def _register_selected_acceptance_session(profile: dict[str, Any], api_start: dict[str, Any]) -> dict[str, Any]:
    """owned runner 显式注册本次服务器生成的验收会话，只访问所选本地 API。"""
    request = Request(validate_api_start_endpoint(profile, api_start) + "/api/voice-barge-in/chat-session", method="POST")
    with urlopen(request, timeout=5) as response:
        registration = json.loads(response.read())
    if (registration.get("session_id") != api_start.get("acceptance_session_id")
            or registration.get("api_start_id") != api_start.get("start_id")
            or registration.get("session_execution_policy") != session_execution_policy(profile)):
        raise ValueError("M6 acceptance session registration does not match selected API/C6")
    return registration


def _read_talk_runtime_identity(url: str) -> dict[str, Any]:
    try:
        with urlopen(url, timeout=5) as response:
            value = json.loads(response.read())
    except HTTPError as exc:
        if exc.code == 409:
            raise RuntimeError("Talk UI runtime identity returned a database/upstream mismatch") from exc
        if exc.code == 404:
            raise RuntimeError("Talk UI runtime identity endpoint is missing") from exc
        raise URLError(f"Talk UI runtime identity is not ready (HTTP {exc.code})") from exc
    if not isinstance(value, dict):
        raise ValueError("Talk UI runtime identity did not return a JSON object")
    return value


def _wait_for_talk_runtime_identity(
    url: str,
    *,
    process: subprocess.Popen[Any],
    environment_id: str,
    database: str,
    database_host_port: int,
    database_server_port: int,
    api_host: str,
    api_port: int,
    timeout_seconds: float = 90,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error = "no response"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Talk UI process exited before runtime identity was verified (exit {process.returncode})")
        try:
            identity = _read_talk_runtime_identity(url)
            db_identity = identity.get("database", {})
            api_identity = identity.get("api", {})
            if (
                identity.get("env_id") != environment_id
                or db_identity.get("database") != database
                or int(db_identity.get("server_port", 0)) != database_server_port
                or int(db_identity.get("url_port", 0)) != database_host_port
                or db_identity.get("url_source") != "HEXIS_DATABASE_URL"
                or api_identity.get("status") != "ok"
                or api_identity.get("checks", {}).get("db") is not True
                or api_identity.get("url_host") != api_host
                or int(api_identity.get("url_port", 0)) != api_port
            ):
                raise RuntimeError("Talk UI runtime identity does not match selected E1/API identities")
            return identity
        except RuntimeError:
            raise
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.25)
    raise RuntimeError(f"Talk UI identity did not become ready within {timeout_seconds:g}s ({last_error})")


def _start_talk_ui(args: argparse.Namespace) -> None:
    environment_id = validate_environment_id(args.env_id)
    _, record = _read_environment_record(args.cache_root, environment_id)
    if record.get("status") != "ready" or not record.get("source_clone", {}).get("agent_status_matches"):
        raise ValueError("the isolated source clone and both database scopes must be verified before Talk UI startup")
    profile = load_profile(args.profile)
    _, api_start = _selected_api_start_identity(record, profile)
    public_url = urlparse(profile["ui_url"])
    if public_url.scheme != "http" or public_url.hostname != "127.0.0.1":
        raise ValueError("Talk UI must use the explicitly selected loopback profile URL")
    port = args.port if args.port is not None else (public_url.port or 3477)
    actual_ui_origin = selected_ui_origin(profile, f"{public_url.scheme}://{public_url.hostname}:{port}")
    if api_start.get("ui_origin") != actual_ui_origin:
        raise ValueError("selected API UI origin does not match this actual Talk UI port; restart API with the matching --ui-origin before continuing")
    registration = _register_selected_acceptance_session(profile, api_start)
    browser_api_url = validate_api_start_endpoint(profile, api_start)
    api_url = urlparse(browser_api_url)
    health_url = f"{browser_api_url}/health"
    with urlopen(health_url, timeout=5) as response:
        health = json.loads(response.read())
    expected_api_identity = health.get("voice_barge_in") or {}
    if (
        health.get("status") != "ok"
        or health.get("checks", {}).get("db") is not True
        or expected_api_identity.get("environment_id") != environment_id
        or expected_api_identity.get("database") != record["owned_resources"]["talk_database"]
        or expected_api_identity.get("start_id") != api_start["start_id"]
        or expected_api_identity.get("session_execution_policy") != session_execution_policy(profile)
        or expected_api_identity.get("acceptance_session_id") != registration["session_id"]
        or expected_api_identity.get("acceptance_session_registered") is not True
        or expected_api_identity.get("migration_verification") != api_start["migration_verification"]
        or expected_api_identity.get("browser_api_url") != browser_api_url
        or expected_api_identity.get("ui_origin") != actual_ui_origin
    ):
        raise ValueError("selected API health does not match the owned E1 and api-start identities")

    _assert_port_available(port)
    output_record = args.output_record.resolve()
    ui_runs_root = (args.cache_root / "voice-barge-in" / "ui-runs").resolve()
    if not output_record.is_relative_to(ui_runs_root) or output_record == ui_runs_root:
        raise ValueError("U1 output must be inside the selected cache root's ui-runs directory")
    if not output_record.name.endswith("-talk.json"):
        raise ValueError("Talk UI output record filename must end in -talk.json")
    if output_record.exists():
        raise FileExistsError(f"refusing to overwrite existing UI run record: {output_record}")
    run_id = output_record.stem
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", run_id):
        raise ValueError("Talk UI run id derived from output filename contains unsupported characters")
    log_path = output_record.with_suffix(".log")
    if log_path.exists():
        raise FileExistsError(f"refusing to overwrite existing Talk UI log: {log_path}")
    dist_dir = f".next/voice-barge-in-{hashlib.sha256(run_id.encode()).hexdigest()[:12]}"
    ui_environment = build_talk_ui_environment(
        os.environ.copy(),
        environment_id=environment_id,
        environment_record_path=_environment_record_path(args.cache_root, environment_id),
        environment_record=record,
        profile=profile,
        port=port,
        dist_dir=dist_dir,
        api_start=api_start,
    )
    fingerprints = _talk_ui_fingerprints()
    build_fingerprint = hashlib.sha256(
        json.dumps({"source_fingerprints": fingerprints, "target": "talk-dev", "api_start_id": api_start["start_id"]}, sort_keys=True).encode()
    ).hexdigest()
    run_record: dict[str, Any] = {
        "schema_version": 1,
        "kind": "talk_ui_run",
        "run_id": run_id,
        "env_id": environment_id,
        "mode": "talk",
        "target": "dev",
        "ui_port": port,
        "ui_url": f"http://127.0.0.1:{port}/chat",
        "browser_api_url": browser_api_url,
        "ui_origin": actual_ui_origin,
        "runtime_flag": "HEXIS_UI_TEST_TARGET=talk",
        "source_root": str(REPOSITORY_ROOT),
        "source_head": _run(["git", "rev-parse", "HEAD"]).stdout.strip(),
        "source_fingerprints": fingerprints,
        "build_fingerprint": build_fingerprint,
        "api_start_id": api_start["start_id"],
        "session_execution_policy": session_execution_policy(profile),
        "migration_verification": api_start["migration_verification"],
        "acceptance_session_id": registration["session_id"],
        "api_start_record": str(_selected_api_start_identity(record, profile)[0]),
        "credential_env_names": ["POSTGRES_USER", "POSTGRES_PASSWORD", "HEXIS_DATABASE_URL", "DATABASE_URL"],
        "network_services": [
            {"kind": "isolated_talk_database", "database": record["owned_resources"]["talk_database"], "port": record["owned_resources"]["talk_host_port"]},
            {"kind": "selected_local_api", "host": api_url.hostname, "port": api_url.port or 80},
        ],
        "status": "starting",
        "log_path": str(log_path),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "dist_dir": dist_dir,
    }
    _atomic_json_write(output_record, run_record, refuse_existing=True)
    process: subprocess.Popen[Any] | None = None
    try:
        log_descriptor = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(log_descriptor, "wb") as log_stream:
            process = subprocess.Popen(
                [str(REPOSITORY_ROOT / "hexis-ui/node_modules/.bin/next"), "dev", "--hostname", "127.0.0.1", "--port", str(port)],
                cwd=REPOSITORY_ROOT / "hexis-ui",
                env=ui_environment,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        run_record["process"] = {"pid": process.pid, "process_group_id": process.pid}
        _atomic_json_write(output_record, run_record)
        _wait_for_worker_page(run_record["ui_url"], process=process, container_id=None)
        identity_url = f"http://127.0.0.1:{port}/api/voice-barge-in/runtime-identity"
        runtime_identity = _wait_for_talk_runtime_identity(
            identity_url,
            process=process,
            environment_id=environment_id,
            database=record["owned_resources"]["talk_database"],
            database_host_port=record["owned_resources"]["talk_host_port"],
            database_server_port=record["db_identity"]["talk"]["server_port"],
            api_host=api_url.hostname or "",
            api_port=api_url.port or 80,
        )
        actual_api = runtime_identity.get("api", {})
        if (actual_api.get("session_execution_policy") != session_execution_policy(profile)
                or actual_api.get("acceptance_session_id") != registration["session_id"]
                or actual_api.get("acceptance_session_registered") is not True
                or actual_api.get("start_id") != api_start["start_id"]):
            raise ValueError("Talk UI live acceptance session/policy does not match selected API/C6")
        if actual_api.get("migration_verification") != api_start["migration_verification"]:
            raise ValueError("Talk UI live migration provenance does not match selected API")
        if (runtime_identity.get("browser_api_url") != browser_api_url
                or actual_api.get("browser_api_url") != browser_api_url):
            raise ValueError("Talk UI live browser URL does not match selected C6")
        if runtime_identity.get("ui_origin") != actual_ui_origin or actual_api.get("ui_origin") != actual_ui_origin:
            raise ValueError("Talk UI live origin does not match the owned API origin selection")
        run_record["runtime_identity"] = runtime_identity
        run_record["status"] = "ready"
        run_record["ready_at_utc"] = datetime.now(timezone.utc).isoformat()
        _atomic_json_write(output_record, run_record)
        print(f"Talk UI identity verified for {environment_id} at {run_record['ui_url']}; U1: {output_record}")
    except Exception:
        run_record["status"] = "identity_failed_process_left_for_explicit_recovery" if process else "start_failed"
        _atomic_json_write(output_record, run_record)
        raise


def _stop_worker_ui(args: argparse.Namespace) -> None:
    environment_id = validate_environment_id(args.env_id)
    run_path = args.run_record.resolve()
    ui_runs_root = (args.cache_root / "voice-barge-in" / "ui-runs").resolve()
    if not run_path.is_relative_to(ui_runs_root):
        raise ValueError("worker UI stop requires an explicit W1 record under the selected cache root")
    try:
        record = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read selected W1 record: {exc}") from exc
    if record.get("env_id") != environment_id or record.get("mode") != "worker" or record.get("kind") != "worker_ui_run":
        raise ValueError("selected W1 record does not own a Worker UI for this env-id")
    if record.get("status") == "stopped":
        print(f"Worker UI run {record['run_id']} is already stopped")
        return
    if record.get("target") == "standalone":
        container = record.get("container", {})
        container_id = container.get("id")
        if not container_id:
            raise ValueError("selected W1 record has no owned container ID")
        inspected = json.loads(_run(["docker", "inspect", container_id]).stdout)[0]
        labels = inspected.get("Config", {}).get("Labels") or {}
        if (
            labels.get("com.hexis.voice-barge-in.env-id") != environment_id
            or labels.get("com.hexis.voice-barge-in.run-id") != record.get("run_id")
            or labels.get("com.hexis.voice-barge-in.mode") != "worker"
            or inspected.get("Image") != container.get("image_id")
        ):
            raise RuntimeError("container labels do not match the selected W1; refusing to stop it")
        _run(["docker", "stop", "--time", "10", container_id])
    else:
        process = record.get("process", {})
        pid = int(process.get("pid", 0))
        group_id = int(process.get("process_group_id", 0))
        if pid <= 0 or group_id != pid:
            raise ValueError("selected W1 has no verified owned process group")
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False).stdout.strip()
        if not command:
            record["status"] = "stopped"
            record["stopped_at_utc"] = datetime.now(timezone.utc).isoformat()
            _atomic_json_write(run_path, record)
            print(f"Worker UI process for {record['run_id']} is already stopped")
            return
        if "next" not in command or "dev" not in command:
            raise RuntimeError("recorded UI PID now belongs to a different command; refusing to signal it")
        env_output = subprocess.run(["ps", "eww", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False).stdout
        if (
            "HEXIS_VAD_TEST_HARNESS=1" not in env_output
            or f"PORT={record.get('ui_port')}" not in env_output
            or os.getpgid(pid) != group_id
        ):
            raise RuntimeError("recorded UI process does not match the selected W1 identity; refusing to stop it")
        os.killpg(group_id, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.killpg(group_id, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError(f"Worker UI process group {group_id} did not stop after SIGTERM; inspect the private W1 log")
    record["status"] = "stopped"
    record["stopped_at_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_json_write(run_path, record)
    print(f"Stopped only the Worker UI owned by W1 run {record['run_id']}")


def _stop_talk_ui(args: argparse.Namespace) -> None:
    environment_id = validate_environment_id(args.env_id)
    run_path = args.run_record.resolve()
    ui_runs_root = (args.cache_root / "voice-barge-in" / "ui-runs").resolve()
    if not run_path.is_relative_to(ui_runs_root):
        raise ValueError("Talk UI stop requires an explicit U1 record under the selected cache root")
    try:
        record = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read selected U1 record: {exc}") from exc
    if record.get("env_id") != environment_id or record.get("mode") != "talk" or record.get("kind") != "talk_ui_run":
        raise ValueError("selected U1 record does not own a Talk UI for this env-id")
    if record.get("status") == "stopped":
        print(f"Talk UI run {record['run_id']} is already stopped")
        return
    process = record.get("process", {})
    pid = int(process.get("pid", 0))
    group_id = int(process.get("process_group_id", 0))
    if pid <= 0 or group_id != pid:
        raise ValueError("selected U1 has no verified owned process group")
    command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False).stdout.strip()
    if not command:
        record["status"] = "stopped"
        record["stopped_at_utc"] = datetime.now(timezone.utc).isoformat()
        _atomic_json_write(run_path, record)
        print(f"Talk UI process for {record['run_id']} is already stopped")
        return
    if "next" not in command or "dev" not in command:
        raise RuntimeError("recorded Talk UI PID now belongs to a different command; refusing to signal it")
    environment_output = subprocess.run(["ps", "eww", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False).stdout
    expected_record = f"HEXIS_VOICE_BARGE_IN_ENV={_environment_record_path(args.cache_root, environment_id)}"
    if (
        "HEXIS_UI_TEST_TARGET=talk" not in environment_output
        or f"PORT={record.get('ui_port')}" not in environment_output
        or expected_record not in environment_output
        or os.getpgid(pid) != group_id
    ):
        raise RuntimeError("recorded Talk UI process does not match the selected U1 identity; refusing to signal it")
    os.killpg(group_id, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.2)
    else:
        raise RuntimeError(f"Talk UI process group {group_id} did not stop after SIGTERM; inspect the private U1 log")
    _assert_port_available(int(record["ui_port"]))
    record["status"] = "stopped"
    record["stopped_at_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_json_write(run_path, record)
    print(f"Stopped only the Talk UI owned by U1 run {record['run_id']}")


def _run_pytest(args: argparse.Namespace) -> None:
    _, record = _read_environment_record(args.cache_root, args.env_id)
    identity = record.get("db_identity", {}).get("pytest")
    if not identity:
        raise ValueError("pytest database has not passed check-db; run check-db --scope pytest first")
    env = build_pytest_environment(os.environ.copy(), identity)
    command = [sys.executable, "-m", "pytest", *args.pytest_args]
    result = subprocess.run(command, cwd=REPOSITORY_ROOT, env=env, check=False, text=True)
    if result.returncode:
        raise SystemExit(result.returncode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Explicitly scoped M6 voice barge-in test environment")
    subparsers = parser.add_subparsers(dest="command", required=True)
    cache_default = cache_root_default()

    prepare = subparsers.add_parser("prepare-db", help="create two isolated database services and E1")
    prepare.add_argument("--env-id", required=True)
    prepare.add_argument("--profile", type=Path, default=PROFILE_DEFAULT)
    prepare.add_argument("--project-name", required=True)
    prepare.add_argument("--pytest-db-port", type=int, required=True)
    prepare.add_argument("--talk-db-port", type=int, required=True)
    prepare.add_argument("--source-container", required=True)
    prepare.add_argument("--source-container-id", required=True)
    prepare.add_argument("--source-database", required=True)
    prepare.add_argument("--source-image-id", required=True)
    prepare.add_argument("--cache-root", type=Path, default=cache_default)
    prepare.set_defaults(handler=_prepare_db)

    check = subparsers.add_parser("check-db", help="verify one selected isolated database and its extensions")
    check.add_argument("--env-id", required=True)
    check.add_argument("--scope", choices=("pytest", "talk"), required=True)
    check.add_argument("--profile", type=Path, default=PROFILE_DEFAULT)
    check.add_argument("--cache-root", type=Path, default=cache_default)
    check.set_defaults(handler=_check_db)

    rebind = subparsers.add_parser("rebind-profile", help="explicitly rebind an inactive verified E1 to the current repository C6 without database access")
    rebind.add_argument("--env-id", required=True)
    rebind.add_argument("--profile", type=Path, default=PROFILE_DEFAULT)
    rebind.add_argument("--expected-profile-fingerprint", required=True)
    rebind.add_argument("--expected-clone-fingerprint", required=True)
    rebind.add_argument("--cache-root", type=Path, default=cache_default)
    rebind.set_defaults(handler=_rebind_profile)

    verify_clone = subparsers.add_parser("verify-clone-identity", help="read-only source/target data and agent identity comparison")
    verify_clone.add_argument("--env-id", required=True)
    verify_clone.add_argument("--source-container", required=True)
    verify_clone.add_argument("--source-container-id", required=True)
    verify_clone.add_argument("--source-database", required=True)
    verify_clone.add_argument("--cache-root", type=Path, default=cache_default)
    verify_clone.set_defaults(handler=_verify_clone_identity)

    start_api = subparsers.add_parser("start-api", help="start the API only against the selected verified clone")
    start_api.add_argument("--env-id", required=True)
    start_api.add_argument("--profile", type=Path, default=PROFILE_DEFAULT)
    start_api.add_argument("--output-start-record", type=Path, required=True)
    start_api.add_argument("--provider-key-env", help="explicitly pass only the environment variable named by C5; omitted by default")
    start_api.add_argument("--ui-origin", help="explicit loopback UI origin when its actual port overrides C6 ui_url")
    start_api.add_argument("--api-port", type=int, help="explicit owned loopback API port when C6 api_url is already occupied")
    start_api.add_argument("--cache-root", type=Path, default=cache_default)
    start_api.set_defaults(handler=_start_api)

    start_ui = subparsers.add_parser("start-ui", help="start the selected W1 Worker page or isolated U1 Talk UI")
    start_ui.add_argument("--env-id", required=True)
    start_ui.add_argument("--profile", type=Path, default=PROFILE_DEFAULT)
    start_ui.add_argument("--mode", choices=("worker", "talk"), required=True)
    start_ui.add_argument("--target", choices=("dev", "standalone"), required=True)
    start_ui.add_argument("--port", type=int)
    start_ui.add_argument("--output-record", type=Path, required=True)
    start_ui.add_argument("--cache-root", type=Path, default=cache_default)
    start_ui.set_defaults(handler=lambda parsed: _start_worker_ui(parsed) if parsed.mode == "worker" else _start_talk_ui(parsed))

    stop = subparsers.add_parser("stop", help="stop one explicitly selected owned API process")
    stop.add_argument("--env-id", required=True)
    stop.add_argument("--target", choices=("api", "worker-ui", "talk-ui"), required=True)
    stop.add_argument("--run-record", type=Path, required=True)
    stop.add_argument("--cache-root", type=Path, default=cache_default)
    stop.set_defaults(handler=lambda parsed: _stop_api(parsed) if parsed.target == "api" else (_stop_worker_ui(parsed) if parsed.target == "worker-ui" else _stop_talk_ui(parsed)))

    run_tests = subparsers.add_parser("run-pytest", help="run tests only against the owned pytest database")
    run_tests.add_argument("--env-id", required=True)
    run_tests.add_argument("--admin-database", choices=("postgres",), required=True)
    run_tests.add_argument("--cache-root", type=Path, default=cache_default)
    run_tests.add_argument("pytest_args", nargs=argparse.REMAINDER)
    run_tests.set_defaults(handler=_run_pytest)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run-pytest" and args.pytest_args and args.pytest_args[0] == "--":
        args.pytest_args = args.pytest_args[1:]
    try:
        args.handler(args)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        print(f"Command failed with exit code {exc.returncode}: {Path(exc.cmd[0]).name if exc.cmd else 'process'}; {stderr or 'see command output for details'}", file=sys.stderr)
        return exc.returncode or 1
    except (FileExistsError, OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Environment operation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
