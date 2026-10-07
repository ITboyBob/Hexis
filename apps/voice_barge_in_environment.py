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
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import urlopen
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


def _fingerprints(repository_root: Path) -> dict[str, str]:
    result = {}
    for relative in FINGERPRINT_PATHS:
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
    if not 1 <= port <= 65535:
        raise ValueError(f"port must be between 1 and 65535: {port}")
    with socket.socket() as listener:
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
    if record.get("env_id") != environment_id or record.get("status") not in {"prepared", "partially_verified", "ready"}:
        raise ValueError("selected E1 record does not identify a prepared environment")
    owned = record.get("owned_resources", {})
    if owned.get("project_name") != derive_project_name(environment_id):
        raise ValueError("selected E1 project identity does not match env-id")
    return path, record


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
        "target_size_bytes": target_identity["size_bytes"],
        "target_migration_count": target_identity["migration_count"],
        "target_extensions": target_identity["extensions"],
        "table_count": target_identity["table_count"],
        "row_count_fingerprint": target_identity["row_count_fingerprint"],
        "source_row_count_fingerprint": target_identity["source_row_count_fingerprint"],
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
    api_url = urlparse(profile["api_url"])
    if api_url.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("M6 API startup must bind to a local loopback address")
    listen_host = "127.0.0.1"
    listen_port = api_url.port or 43817
    _assert_port_available(listen_port)
    record_path = args.output_start_record.resolve()
    api_runs = (args.cache_root / "voice-barge-in" / "api-runs").resolve()
    if not record_path.is_relative_to(api_runs) or record_path == api_runs:
        raise ValueError("API start record must be inside the selected cache root's api-runs directory")
    if record_path.exists():
        raise FileExistsError(f"refusing to overwrite API start record: {record_path}")
    env = _compose_environment(environment_id, record)
    env.update({
        "HEXIS_VOICE_BARGE_IN_ENV": str(environment_record_path),
        "HEXIS_VOICE_BARGE_IN_API_START_RECORD": str(record_path),
        "HEXIS_VOICE_BARGE_IN_LISTEN_HOST": listen_host,
        "HEXIS_VOICE_BARGE_IN_LISTEN_PORT": str(listen_port),
    })
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
                record["owned_resources"]["api_process"].update({
                    "status": "healthy",
                    "start_id": start_record["start_id"],
                    "api_url": profile["api_url"],
                })
                _atomic_json_write(environment_record_path, record)
                print(f"M6 API healthy for {environment_id} at {profile['api_url']}; pid={process.pid}; record={record_path}")
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
    start_api.add_argument("--cache-root", type=Path, default=cache_default)
    start_api.set_defaults(handler=_start_api)

    stop = subparsers.add_parser("stop", help="stop one explicitly selected owned API process")
    stop.add_argument("--env-id", required=True)
    stop.add_argument("--target", choices=("api",), required=True)
    stop.add_argument("--run-record", type=Path, required=True)
    stop.add_argument("--cache-root", type=Path, default=cache_default)
    stop.set_defaults(handler=_stop_api)

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
