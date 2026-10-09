"""BOB-108 的实时工具 Activity 隔离准备、同意、服务与验收入口。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

from dotenv import dotenv_values

from apps import bob108_test_runtime as control_runtime


ROOT = Path(__file__).resolve().parents[1]
MATERIAL_ROOT = Path("/Users/itboybob/.cache/hexis/bob108-20261009")
DEFAULT_PORTS = {"api": 44817, "ui": 3577, "pytest": 45816, "browser": 45815, "embeddings": 45435}
FINAL_VERIFICATION_SOURCE_PATHS = (
    "apps/bob108_tool_activity_probe.py",
    "tests/services/test_bob108_tool_activity_probe.py",
    "hexis-ui/e2e/tool-activity.spec.ts",
    "hexis-ui/lib/tool-activity-startup.test.ts",
)
RESUME_HARNESS_FILES = {"hexis-ui/e2e/tool-activity.spec.ts", "hexis-ui/lib/tool-activity-startup.test.ts"}
RESUME_INTRINSIC_FILES = {"apps/bob108_tool_activity_probe.py", "tests/services/test_bob108_tool_activity_probe.py"}
PROFILE_KEYS = ("agent.init_profile", "agent.mode", "agent.timezone", "llm.chat", "llm.heartbeat", "llm.subconscious")
SECRET_NAMES = {"POSTGRES_PASSWORD", "DASHSCOPE_API_KEY"}
PROFILE_REQUIRED = ("provider", "model", "endpoint", "api_key_env")
EXPECTED_SELECTION_SHA256 = "2a3fd21bdc8d1d9c72a087dca67b648629d8f7c2508c8ef6f8e4ae27698d2186"
EXPECTED_PROFILE_SHA256 = "c9e58538468202a54616711996fd637485fc7bc30059e291ca840c32cf78874b"
EXPECTED_MATERIALS_SHA256 = "8eb5bf4abce7c5dbbbcc1d51f22b5ca223a37d7899b3aa834d81715ba4a0dd88"

# Task 4.3：源码定位与执行结果分开，历史消息 shape 不证明内容保持不变。
COVERAGE_TESTS = {
    "unknown": (
        "tests/services/test_tool_activity.py::test_lightweight_receipt_state_is_authoritative_for_activity_visibility",
        "tests/services/test_m5_lightweight.py::test_terminal_event_preserves_the_authoritative_receipt_state",
        "hexis-ui/app/chat/page.test.tsx::shows the neutral unknown receipt label without inferring failure from success=false",
    ),
    "accepted-cursor": (
        "tests/web/test_tool_activity_api.py::test_accepted_result_is_returned_without_activity_projection_but_advances_cursor",
        "hexis-ui/lib/tool-activity.test.ts::consumes accepted results through the cursor without creating a log row",
        "hexis-ui/app/chat/page.test.tsx::replays persisted request and result once, consumes accepted results, and keeps long recall text readable",
    ),
    "refresh-replay": (
        "tests/web/test_tool_activity_api.py::test_cursor_replay_never_returns_old_or_foreign_session_tool_events",
        "hexis-ui/app/chat/page.test.tsx::replays persisted request and result once, consumes accepted results, and keeps long recall text readable",
    ),
    "disconnect-replay": (
        "tests/voice_realtime/test_voice_recall.py::test_talk_disconnect_preserves_dispatched_tool_receipt_and_replay",
    ),
    "out-of-order": (
        "hexis-ui/lib/tool-activity.test.ts::keeps event identity and current session when merging out-of-order page results",
    ),
    "same-seq-cross-stream": (
        "hexis-ui/lib/tool-activity.test.ts::preserves same-sequence events from independent streams",
    ),
    "late-session-response": (
        "hexis-ui/lib/tool-activity.test.ts::discards late pages for a different current session",
        "hexis-ui/app/chat/page.test.tsx::keeps late text-stream activity in its source session after opening a new conversation",
    ),
    "origin-task-isolation": (
        "tests/web/test_tool_activity_api.py::test_cursor_replay_never_returns_old_or_foreign_session_tool_events",
        "tests/services/test_m5_task_worker.py::test_worker_persists_actual_tool_events_while_work_is_running",
        "hexis-ui/app/chat/page.test.tsx::opens and returns from the fixed task session without mixing conversation history",
    ),
    "original-tool-messages-unchanged": (
        "hexis-ui/app/chat/page.test.tsx::restores tool calls and accepted results from the DB without creating a blank assistant bubble",
    ),
}


class ProbeError(RuntimeError):
    """Fail-closed BOB-108 run preparation or evidence error."""


def choose_ports(
    requested: dict[str, int], *, port_available: Any = control_runtime.port_is_available
) -> dict[str, int]:
    """Select unique loopback ports, advancing only when a selected port is occupied."""
    selected: dict[str, int] = {}
    reserved: set[int] = set()
    for service, base_port in requested.items():
        if isinstance(base_port, bool) or not isinstance(base_port, int) or not 1 <= base_port <= 65535:
            raise ProbeError(f"invalid port selection for {service}")
        for candidate in range(base_port, min(65536, base_port + 1000)):
            if candidate not in reserved and port_available(candidate):
                selected[service] = candidate
                reserved.add(candidate)
                break
        else:
            raise ProbeError(f"no available loopback port found for {service}")
    return selected


def target_database_name(run_id: str, *, source_database: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ProbeError("run_id must be 32 lowercase hexadecimal characters")
    database = f"m5_acceptance_{run_id}"
    validate_target_database(database, source_database=source_database, run_id=run_id)
    return database


def validate_target_database(database: str, *, source_database: str, run_id: str) -> None:
    expected = f"m5_acceptance_{run_id}"
    if database == source_database or database != expected:
        raise ProbeError("target database must be run-scoped and differ from the source database")


def build_runtime_bindings(
    selection: dict[str, Any],
    profile: dict[str, Any],
    selected_values: dict[str, str],
    *,
    run_id: str,
    ports: dict[str, int],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Separate persistable non-secret bindings from the child process environment."""
    if any(not isinstance(profile.get(key), str) or not profile[key].strip() for key in PROFILE_REQUIRED):
        raise ProbeError("selected background profile is incomplete")
    if profile["model"] != "qwen-plus" or profile["provider"] != "openai-chat-completions-endpoint":
        raise ProbeError("selected background profile must explicitly choose qwen-plus")
    credential_name = profile["api_key_env"]
    if not selected_values.get(credential_name):
        raise ProbeError("selected credential file lacks the chosen provider environment variable")
    db_required = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
    if any(not selected_values.get(key) for key in db_required):
        raise ProbeError("selected credential file lacks a database binding")
    source_database = selected_values["POSTGRES_DB"]
    database = target_database_name(run_id, source_database=source_database)
    admin_database = selected_values.get("POSTGRES_ADMIN_DB") or "postgres"
    api_url = f"http://127.0.0.1:{ports['api']}"
    ui_url = f"http://127.0.0.1:{ports['ui']}"
    embedding_url = f"http://127.0.0.1:{ports['embeddings']}/api/embed"
    user = quote(selected_values["POSTGRES_USER"], safe="")
    password = quote(selected_values["POSTGRES_PASSWORD"], safe="")
    database_url = f"postgresql://{user}:{password}@127.0.0.1:{ports['browser']}/{quote(database, safe='')}"

    runtime = {
        "run_id": run_id,
        "database": database,
        "source": {
            "host": selected_values["POSTGRES_HOST"],
            "port": int(selected_values["POSTGRES_PORT"]),
            "user": selected_values["POSTGRES_USER"],
            "database": source_database,
            "admin_database": admin_database,
        },
        "target": {
            "host": "127.0.0.1",
            "port": ports["browser"],
            "user": selected_values["POSTGRES_USER"],
            "database": database,
            "admin_database": admin_database,
            "admin_database_source": selected_values.get("POSTGRES_ADMIN_DB_SOURCE", "selected .env"),
        },
        "pytest": {"host": "127.0.0.1", "port": ports["pytest"], "database": "bob108_pytest", "admin_database": admin_database},
        "ports": dict(ports),
        "api_url": api_url,
        "ui_url": ui_url,
        "embedding_url": embedding_url,
        "credential_env_file": selection["credential_env_file"],
        "credential_env_names": sorted({"POSTGRES_PASSWORD", credential_name}),
        "credential_password_env": "POSTGRES_PASSWORD",
        "provider_profile": dict(profile),
        "runtime_env_names": list(selection.get("runtime_env_names", [])),
        "realtime_config": dict(selection.get("realtime", {})),
        "input_gate_config": dict(selection.get("input_gate", {})),
        "playback_config": dict(selection.get("playback_coordination", {})),
    }
    child = {
        "HOME": "",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": str(ports["browser"]),
        "POSTGRES_USER": selected_values["POSTGRES_USER"],
        "POSTGRES_PASSWORD": selected_values["POSTGRES_PASSWORD"],
        "POSTGRES_DB": database,
        "POSTGRES_ADMIN_DB": admin_database,
        "HEXIS_DATABASE_URL": database_url,
        "HEXIS_API_URL": api_url,
        "HEXIS_BROWSER_API_URL": api_url,
        "HEXIS_UI_ORIGIN": ui_url,
        "HEXIS_CORS_ORIGINS": ui_url,
        "HEXIS_REALTIME_CONFIG": str(selection["realtime"]["path"]),
        "HEXIS_M4_VOICE_CONFIG": str(selection["input_gate"]["path"]),
        "HEXIS_RUNTIME_IDENTITY_STATUS": "1",
        "HEXIS_BOB108_RUN_ID": run_id,
        "HEXIS_BOB108_RUN_MANIFEST": "",
        "HEXIS_BOB108_EMBEDDING_URL": embedding_url,
        "EMBEDDING_SERVICE_URL": embedding_url,
        "DASHSCOPE_API_KEY": selected_values[credential_name],
        "FAKE_EMBEDDINGS_HOST": "127.0.0.1",
        "FAKE_EMBEDDINGS_PORT": str(ports["embeddings"]),
        "EMBEDDING_DIMENSION": "768",
        "EMBEDDING_MODEL_ID": "bob108-fake-768",
    }
    return runtime, child


def validate_model_consent(row: dict[str, Any] | None, profile: dict[str, Any]) -> bool:
    if not isinstance(row, dict) or row.get("decision") != "consent":
        raise ProbeError("model consent decision is not consent")
    if row.get("operator_override"):
        raise ProbeError("operator override cannot satisfy model consent")
    if any(row.get(key) != profile.get(key) for key in ("provider", "model", "endpoint")):
        raise ProbeError("model consent does not match the selected provider profile")
    return True


def _absolute(path: str | Path) -> Path:
    result = Path(path).expanduser()
    if not result.is_absolute():
        raise ProbeError("selected paths must be absolute")
    return result


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _verify_file_ref(reference: dict[str, Any]) -> Path:
    path = _absolute(reference["path"])
    if not path.is_file() or _digest(path) != reference.get("sha256"):
        raise ProbeError(f"selected file is missing or has a different SHA-256: {path}")
    return path


def _deny_inline_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"api_key", "password", "secret", "token"} and item:
                raise ProbeError("selected runtime configuration must reference environment variables, not inline secrets")
            _deny_inline_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _deny_inline_secrets(item)


def _source_changed_files(source_head: str) -> set[str]:
    committed = subprocess.run(
        ["git", "diff", "--name-only", f"{source_head}..HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    working = subprocess.run(
        ["git", "diff", "--name-only"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return set(committed) | set(working) | set(untracked)


def verify_unchanged_baseline_sources(materials: dict[str, Any]) -> dict[str, Any]:
    changed = _source_changed_files(str(materials["source_head"]))
    checked: dict[str, str] = {}
    skipped: dict[str, str] = {}
    for relative, expected in materials.get("source_fingerprints", {}).items():
        path = ROOT / relative
        if not path.is_file():
            raise ProbeError(f"baseline source file is missing: {relative}")
        if relative in changed:
            skipped[relative] = "changed in an earlier authorized Sprint or current run"
            continue
        actual = _digest(path)
        if actual != expected:
            raise ProbeError(f"unchanged baseline source fingerprint differs: {relative}")
        checked[relative] = actual
    return {"checked": checked, "previously_changed": skipped, "source_head": materials["source_head"]}


def verify_offline_materials() -> dict[str, Any]:
    script = _absolute(MATERIAL_ROOT / "verify_materials.py")
    if not script.is_file():
        raise ProbeError("the selected offline material verifier is missing")
    completed = subprocess.run([sys.executable, str(script)], cwd=ROOT, capture_output=True, text=True)
    if completed.returncode != 0:
        raise ProbeError("offline asset/hash validation failed; no database or provider call was made")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError("offline material verifier returned an invalid summary") from exc
    if result.get("状态") != "离线材料核对通过" or result.get("数据库或provider调用") is not False:
        raise ProbeError("offline material verifier did not confirm a no-provider, no-database pass")
    return result


def load_selected_inputs(
    selection_path: str | Path,
    profile_path: str | Path,
    *,
    materials_path: str | Path | None = None,
    repo_root: str | Path = ROOT,
    strict_material_hashes: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str], dict[str, Any]]:
    """Read only the user-selected files; credentials remain in memory."""
    selected_file = _absolute(selection_path)
    profile_file = _absolute(profile_path)
    selected_root = _absolute(repo_root)
    if not selected_file.is_file() or not profile_file.is_file():
        raise ProbeError("an explicitly selected runtime configuration file is missing")
    if strict_material_hashes and _digest(selected_file) != EXPECTED_SELECTION_SHA256:
        raise ProbeError("selected config-selection.json SHA-256 differs from the execution plan")
    if strict_material_hashes and _digest(profile_file) != EXPECTED_PROFILE_SHA256:
        raise ProbeError("selected background qwen-plus profile SHA-256 differs from the execution plan")
    selection = json.loads(selected_file.read_text(encoding="utf-8"))
    profile = json.loads(profile_file.read_text(encoding="utf-8"))
    if selection.get("schema") != "bob108-config-selection-v1":
        raise ProbeError("the selected BOB-108 config-selection version is unsupported")
    if profile.get("schema") != "bob108-background-llm-selection-v1":
        raise ProbeError("the selected background profile version is unsupported")
    _deny_inline_secrets(selection)
    _deny_inline_secrets(profile)
    profile_ref = selection.get("background_llm_profile", {})
    if profile_ref.get("path") != str(profile_file) or profile_ref.get("sha256") != _digest(profile_file):
        raise ProbeError("the background profile differs from the selected hash or path")
    profile_source = profile.get("配置来源", {})
    if profile_source.get("path") != selection.get("input_gate", {}).get("path"):
        raise ProbeError("the selected qwen profile source differs from the voice runtime selection")
    if profile_source.get("sha256") != selection.get("input_gate", {}).get("sha256"):
        raise ProbeError("the selected qwen profile source hash differs from the voice runtime selection")
    if selection.get("source_root") != str(selected_root):
        raise ProbeError("the selected source root differs from this repository")

    config_hashes: dict[str, str] = {}
    for key in ("realtime", "input_gate", "playback_coordination"):
        reference = selection.get(key)
        if not isinstance(reference, dict):
            raise ProbeError(f"selected {key} configuration is missing")
        path = _verify_file_ref(reference)
        config_hashes[str(path)] = _digest(path)
    realtime_config = json.loads(Path(selection["realtime"]["path"]).read_text(encoding="utf-8"))
    input_gate_config = json.loads(Path(selection["input_gate"]["path"]).read_text(encoding="utf-8"))
    credential_refs = selection.get("credentials", {})
    expected_credential = profile["profile"]["api_key_env"]
    if (
        credential_refs.get("background_ref") != expected_credential
        or credential_refs.get("realtime_ref") != realtime_config.get("api_key_env")
        or credential_refs.get("emotion_ref") != input_gate_config.get("emotion", {}).get("api_key_env")
        or credential_refs.get("background_ref") != credential_refs.get("realtime_ref")
        or credential_refs.get("realtime_ref") != credential_refs.get("emotion_ref")
    ):
        raise ProbeError("the selected voice and background profile credential references do not match")
    if not Path(selection["realtime"]["path"]).is_file() or not Path(selection["input_gate"]["path"]).is_file():
        raise ProbeError("a selected voice runtime configuration is unavailable")

    materials_file = _absolute(materials_path or MATERIAL_ROOT / "materials-manifest.json")
    if strict_material_hashes and _digest(materials_file) != EXPECTED_MATERIALS_SHA256:
        raise ProbeError("materials manifest SHA-256 differs from the execution plan")
    materials = json.loads(materials_file.read_text(encoding="utf-8"))
    if materials.get("source_head") != "ee2c2e00d4c95fddf807d287b16ab6105e37d2cf":
        raise ProbeError("materials manifest source baseline differs from the execution plan")
    for reference in [*materials.get("audio", []), *materials.get("data_files", [])]:
        _verify_file_ref(reference)
    if isinstance(materials.get("reuse_test_fixture"), dict):
        _verify_file_ref(materials["reuse_test_fixture"])
    seed_records = [item for item in materials.get("data_files", []) if Path(str(item.get("path", ""))).name == "seed-data.json"]
    if len(seed_records) != 1:
        raise ProbeError("the approved seed data is absent from the material manifest")
    seed_file = _verify_file_ref(seed_records[0])

    env_file = _absolute(selection["credential_env_file"])
    values = {key: str(value) for key, value in dotenv_values(env_file, interpolate=False).items() if value is not None}
    required = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", profile["profile"]["api_key_env"])
    if any(not values.get(key) for key in required):
        raise ProbeError("the explicitly selected .env lacks a required database or provider credential")
    # The repository test fixture and existing BOB-108 runner both define postgres as the admin default.
    admin_source = "selected .env" if values.get("POSTGRES_ADMIN_DB") else "tests/conftest.py default"
    values["POSTGRES_ADMIN_DB"] = values.get("POSTGRES_ADMIN_DB") or "postgres"
    values["POSTGRES_ADMIN_DB_SOURCE"] = admin_source

    if Path(selection["credential_env_file"]).resolve() != selected_root / ".env":
        raise ProbeError("the selected credential source differs from the recorded .env file")
    if Path(selection["background_llm_profile"]["path"]).resolve() != profile_file:
        raise ProbeError("the selected background profile path changed during loading")
    selection["_selected_path"] = str(selected_file)
    selection["_credential_path"] = str(env_file)
    selection["_admin_database_source"] = admin_source
    selection["_config_hashes"] = config_hashes
    selection["_materials_path"] = str(materials_file)
    selection["_materials_sha256"] = _digest(materials_file)
    selection["_seed_data_path"] = str(seed_file)
    selection["_seed_data_sha256"] = _digest(seed_file)
    return selection, profile["profile"], values, materials


def _dsn(host: str, port: int, database: str, user: str, password: str) -> str:
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@"
        f"{host}:{port}/{quote(database, safe='')}"
    )


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _safe_profile(value: Any) -> dict[str, Any]:
    profile = _json_value(value)
    if not isinstance(profile, dict):
        raise ProbeError("a source provider profile is not a JSON object")
    return {key: profile.get(key) for key in PROFILE_REQUIRED}


async def read_source_profile(values: dict[str, str], selected_profile: dict[str, Any]) -> dict[str, Any]:
    import asyncpg

    conn = await asyncpg.connect(
        _dsn(values["POSTGRES_HOST"], int(values["POSTGRES_PORT"]), values["POSTGRES_DB"],
             values["POSTGRES_USER"], values["POSTGRES_PASSWORD"]),
        timeout=5,
    )
    try:
        async with conn.transaction(readonly=True):
            identity = await conn.fetchrow(
                "SELECT current_database() AS database, "
                "(SELECT system_identifier::text FROM pg_control_system()) AS system_identifier"
            )
            if identity["database"] != values["POSTGRES_DB"]:
                raise ProbeError("source DB connection resolved to a different database")
            status = _json_value(await conn.fetchval("SELECT get_agent_status()"))
            if not isinstance(status, dict) or not status.get("configured"):
                raise ProbeError("the selected source profile is not configured")
            rows = await conn.fetch("SELECT key,value FROM config WHERE key=ANY($1::text[])", list(PROFILE_KEYS))
            profiles = {row["key"]: _json_value(row["value"]) for row in rows}
            if set(profiles) != set(PROFILE_KEYS):
                raise ProbeError("the source DB is missing one or more selected profile fields")
            for value in profiles.values():
                _deny_inline_secrets(value)
            source_chat = _safe_profile(profiles["llm.chat"])
            if source_chat != {key: selected_profile.get(key) for key in PROFILE_REQUIRED}:
                raise ProbeError("source chat profile differs from the explicitly selected qwen-plus profile")
            qwen_consents = await conn.fetch(
                "SELECT id::text,decision,provider,model,endpoint,decided_at "
                "FROM consent_log WHERE provider=$1 AND model=$2 ORDER BY decided_at DESC",
                selected_profile["provider"], selected_profile["model"],
            )
        return {
            "identity": dict(identity),
            "configured": True,
            "profiles": profiles,
            "profiles_sha256": hashlib.sha256(
                json.dumps(profiles, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "matching_consent_count": len(qwen_consents),
            "matching_consents": [dict(row) for row in qwen_consents],
        }
    finally:
        await conn.close()


def compose_text(project: str, run_id: str, ports: dict[str, int], volumes: dict[str, str]) -> str:
    database = control_runtime._repository_postgres_config()
    image = json.dumps(database["image"])
    context = json.dumps(database["context"])
    dockerfile = json.dumps(database["dockerfile"])
    readme_volume = json.dumps(str(ROOT / "ops/ci/fake_embeddings.py") + ":/app/fake_embeddings.py:ro")
    run_label = json.dumps(run_id)
    return f'''name: {project}
services:
  pytest-db:
    image: {image}
    build:
      context: {context}
      dockerfile: {dockerfile}
    environment:
      POSTGRES_USER: ${{POSTGRES_USER}}
      POSTGRES_PASSWORD: ${{POSTGRES_PASSWORD}}
      POSTGRES_DB: bob108_pytest
    ports:
      - "127.0.0.1:{ports['pytest']}:5432"
    command:
      - postgres
      - -c
      - search_path=public,ag_catalog
      - -c
      - app.embedding_dimension=768
      - -c
      - app.embedding_service_url=http://fake-embeddings:45435/api/embed
      - -c
      - app.embedding_model_id=bob108-fake-768
    volumes:
      - {volumes['pytest']}:/var/lib/postgresql/data
    labels:
      hexis.bob108.run_id: {run_label}
  browser-db:
    image: {image}
    build:
      context: {context}
      dockerfile: {dockerfile}
    environment:
      POSTGRES_USER: ${{POSTGRES_USER}}
      POSTGRES_PASSWORD: ${{POSTGRES_PASSWORD}}
      POSTGRES_DB: postgres
    ports:
      - "127.0.0.1:{ports['browser']}:5432"
    command:
      - postgres
      - -c
      - search_path=public,ag_catalog
      - -c
      - app.embedding_dimension=768
      - -c
      - app.embedding_service_url=http://fake-embeddings:45435/api/embed
      - -c
      - app.embedding_model_id=bob108-fake-768
    volumes:
      - {volumes['browser']}:/var/lib/postgresql/data
    labels:
      hexis.bob108.run_id: {run_label}
  fake-embeddings:
    image: python:3.13-slim
    command: ["python", "/app/fake_embeddings.py"]
    environment:
      EMBEDDING_DIMENSION: "768"
      FAKE_EMBEDDINGS_HOST: 0.0.0.0
      FAKE_EMBEDDINGS_PORT: "45435"
    ports:
      - "127.0.0.1:{ports['embeddings']}:45435"
    volumes:
      - {readme_volume}
    labels:
      hexis.bob108.run_id: {run_label}
volumes:
  {volumes['pytest']}:
    labels:
      hexis.bob108.run_id: {run_label}
  {volumes['browser']}:
    labels:
      hexis.bob108.run_id: {run_label}
'''


def _write_runtime_env(path: Path, runtime: dict[str, Any]) -> None:
    values = {
        "POSTGRES_HOST": runtime["target"]["host"],
        "POSTGRES_PORT": runtime["target"]["port"],
        "POSTGRES_USER": runtime["target"]["user"],
        "POSTGRES_DB": runtime["target"]["database"],
        "POSTGRES_ADMIN_DB": runtime["target"]["admin_database"],
        "POSTGRES_PASSWORD_ENV": "POSTGRES_PASSWORD",
        "DASHSCOPE_API_KEY_ENV": runtime["provider_profile"]["api_key_env"],
        "HEXIS_DATABASE_URL_ENV": "HEXIS_DATABASE_URL",
        "HEXIS_API_URL": runtime["api_url"],
        "HEXIS_UI_ORIGIN": runtime["ui_url"],
        "HEXIS_REALTIME_CONFIG": runtime["realtime_config"]["path"],
        "HEXIS_M4_VOICE_CONFIG": runtime["input_gate_config"]["path"],
        "FAKE_EMBEDDINGS_URL": runtime["embedding_url"],
    }
    text = "".join(f"{key}={value}\n" for key, value in values.items())
    forbidden = [runtime.get("_postgres_password", ""), runtime.get("_provider_key", "")]
    if any(secret and secret in text for secret in forbidden):
        raise ProbeError("runtime.env unexpectedly contains a selected secret value")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


def _compose_command(run_dir: Path, manifest: dict[str, Any], env_file: Path) -> list[str]:
    return [
        "docker", "compose", "--project-name", manifest["project"],
        "--env-file", str(env_file), "-f", str(run_dir / "compose.yml"),
    ]


async def _prepare_browser_database(
    *,
    runtime: dict[str, Any],
    selected_profile: dict[str, Any],
    source: dict[str, Any],
    values: dict[str, str],
    workspace: Path,
) -> dict[str, Any]:
    import asyncpg
    from core.migrations import apply_pending_migrations

    target = runtime["target"]
    admin = await asyncpg.connect(
        _dsn(target["host"], target["port"], target["admin_database"], target["user"], values["POSTGRES_PASSWORD"]),
        timeout=5,
    )
    try:
        if await admin.fetchval("SELECT current_database()") != target["admin_database"]:
            raise ProbeError("browser PostgreSQL admin connection resolved to an unexpected database")
        exists = await admin.fetchval("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", target["database"])
        if exists:
            raise ProbeError("the run-scoped target database already exists; refusing to reuse it")
        await admin.execute(f'CREATE DATABASE "{target["database"]}"')
    finally:
        await admin.close()

    conn = await asyncpg.connect(
        _dsn(target["host"], target["port"], target["database"], target["user"], values["POSTGRES_PASSWORD"]),
        command_timeout=120,
        timeout=5,
    )
    try:
        if await conn.fetchval("SELECT current_database()") != target["database"]:
            raise ProbeError("target PostgreSQL connection resolved to an unexpected database")
        for schema_file in sorted((ROOT / "db").glob("*.sql")):
            await conn.execute(schema_file.read_text(encoding="utf-8"))
        migrations = await apply_pending_migrations(conn)
        async with conn.transaction():
            for key in PROFILE_KEYS:
                value = selected_profile if key == "llm.chat" else source["profiles"][key]
                await conn.execute("SELECT set_config($1,$2::jsonb)", key, json.dumps(value, ensure_ascii=False))
            tools_row = await conn.fetchrow("SELECT value FROM config WHERE key='tools'")
            tools_config = _json_value(tools_row["value"]) if tools_row else {}
            if not isinstance(tools_config, dict):
                raise ProbeError("target default tools configuration is not a JSON object")
            tools_config["workspace_path"] = str(workspace)
            await conn.execute("SELECT set_config('tools',$1::jsonb)", json.dumps(tools_config, ensure_ascii=False))
            await conn.execute("UPDATE heartbeat_state SET is_paused=TRUE WHERE id=1")
        identity = await conn.fetchrow(
            "SELECT current_database() AS database, inet_server_addr()::text AS address, "
            "inet_server_port() AS port, (SELECT system_identifier::text FROM pg_control_system()) AS system_identifier"
        )
        if identity["database"] != target["database"]:
            raise ProbeError("target DB identity changed during baseline and migration application")
        if identity["system_identifier"] == source["identity"]["system_identifier"]:
            raise ProbeError("target browser PostgreSQL shares the source server identity")
        profile_values = await conn.fetch(
            "SELECT key,value FROM config WHERE key=ANY($1::text[])", ["llm.chat", "embedding.service_url", "tools"]
        )
        observed = {row["key"]: _json_value(row["value"]) for row in profile_values}
        if _safe_profile(observed.get("llm.chat")) != {key: selected_profile.get(key) for key in PROFILE_REQUIRED}:
            raise ProbeError("target DB chat profile did not match the explicitly selected qwen-plus profile")
        if observed.get("tools", {}).get("workspace_path") != str(workspace):
            raise ProbeError("target workspace sandbox binding did not persist")
        return {"identity": dict(identity), "migrations_applied": migrations, "profile_readback": _safe_profile(observed["llm.chat"]), "embedding_service_url": observed.get("embedding.service_url"), "workspace_path": str(workspace)}
    finally:
        await conn.close()


async def _read_source_state(values: dict[str, str], selected_profile: dict[str, Any]) -> dict[str, Any]:
    """Read the selected source profile and consent history under a read-only transaction."""
    from apps.m5_acceptance_probe import PROFILE_KEYS

    raw = await read_source_profile(values, selected_profile)
    return {"identity": raw["identity"], "profiles": raw["profiles"], "profiles_sha256": raw["profiles_sha256"],
            "matching_consent_count": raw["matching_consent_count"]}


def _compose_environment_file(selection: dict[str, Any]) -> Path:
    return _absolute(selection["credential_env_file"])


def _run_compose(run_dir: Path, manifest: dict[str, Any], selection: dict[str, Any], *args: str) -> subprocess.CompletedProcess[str]:
    command = [*_compose_command(run_dir, manifest, _compose_environment_file(selection)), *args]
    try:
        return subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        # Docker diagnostics can include environment-expanded values. Keep only safe process metadata.
        raise ProbeError(f"owned Compose operation failed ({exc.returncode})") from exc


async def _wait_owned_postgres(
    runtime: dict[str, Any], values: dict[str, str], *, timeout_seconds: float = 90
) -> dict[str, Any]:
    import asyncpg
    import urllib.request

    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        identities: dict[str, Any] = {}
        try:
            for name in ("pytest", "target"):
                binding = runtime[name]
                database = binding["admin_database"] if name == "target" else binding["database"]
                identities[name] = await control_runtime._pg_identity(
                    binding["host"], binding["port"], database,
                    binding["user"], values["POSTGRES_PASSWORD"],
                )
            with urllib.request.urlopen(
                f"http://127.0.0.1:{runtime['ports']['embeddings']}/api/tags", timeout=3
            ) as response:
                embedding = json.loads(response.read().decode("utf-8"))
            if not isinstance(embedding, dict):
                raise ProbeError("fake embedding health returned a non-object")
            return {"postgres": identities, "fake_embeddings_health": embedding}
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(1)
    raise ProbeError("owned run-01 PostgreSQL or fake embedding service did not become ready") from last_error


async def _prepare_real_async(
    selection_path: str | Path,
    profile_path: str | Path,
    run_dir: str | Path,
) -> dict[str, Any]:
    selection, profile, values, materials = load_selected_inputs(selection_path, profile_path)
    run_path = _absolute(run_dir)
    if run_path.exists() and any(run_path.iterdir()):
        raise ProbeError("run-01 directory already contains files; refusing to overwrite or resume implicitly")
    offline_materials = verify_offline_materials()
    ports_config = selection.get("selected_local_endpoints", {})
    requested = {
        "api": urlsplit(ports_config.get("api_url", "")).port or DEFAULT_PORTS["api"],
        "ui": urlsplit(ports_config.get("ui_url", "")).port or DEFAULT_PORTS["ui"],
        "pytest": int(ports_config.get("pytest_postgres_port") or DEFAULT_PORTS["pytest"]),
        "browser": int(ports_config.get("browser_postgres_port") or DEFAULT_PORTS["browser"]),
        "embeddings": int(ports_config.get("fake_embedding_port") or DEFAULT_PORTS["embeddings"]),
    }
    ports = choose_ports(requested)
    source = await read_source_profile(values, profile)
    fingerprints = verify_unchanged_baseline_sources(materials)
    run_id = uuid4().hex
    runtime, child = build_runtime_bindings(selection, profile, values, run_id=run_id, ports=ports)
    project = f"hexis-bob108-{run_path.name}"
    runtime["project"] = project
    runtime["source_database_identity"] = source["identity"]
    runtime["source_profiles_sha256"] = source["profiles_sha256"]
    runtime["source_matching_qwen_consent_count"] = source["matching_consent_count"]
    runtime["materials_manifest_path"] = str(_absolute(MATERIAL_ROOT / "materials-manifest.json"))
    runtime["materials_manifest_sha256"] = _digest(_absolute(MATERIAL_ROOT / "materials-manifest.json"))
    runtime["seed_data_path"] = str(_absolute(MATERIAL_ROOT / "seed-data.json"))
    runtime["seed_data_sha256"] = _digest(_absolute(MATERIAL_ROOT / "seed-data.json"))
    runtime["source_fingerprint_check"] = fingerprints
    runtime["offline_material_validation"] = offline_materials
    volumes = {
        "pytest": f"{project}-pytest-brain-pgdata",
        "browser": f"{project}-browser-brain-pgdata",
    }
    source_fingerprints = control_runtime._current_source_fingerprints()
    source_fingerprints["apps/bob108_tool_activity_probe.py"] = _digest(ROOT / "apps/bob108_tool_activity_probe.py")
    e2e_spec = ROOT / "hexis-ui/e2e/tool-activity.spec.ts"
    if e2e_spec.is_file():
        source_fingerprints["hexis-ui/e2e/tool-activity.spec.ts"] = _digest(e2e_spec)
    manifest: dict[str, Any] = {
        "schema": "bob108-tool-activity-run-v1",
        "run_id": run_id,
        "run_directory": str(run_path),
        "project": project,
        "state": "preparing",
        "services": ["pytest-db", "browser-db", "fake-embeddings"],
        "volumes": volumes,
        "ports": ports,
        "source_head": control_runtime._git_head(),
        "source_fingerprints": source_fingerprints,
        "selected_runtime_path": str(run_path / "selected-runtime.json"),
        "runtime_env_path": str(run_path / "runtime.env"),
        "compose_path": str(run_path / "compose.yml"),
        "evidence_directory": str(run_path / "evidence"),
        "runtime": runtime,
        "service_identities": {},
        "owned_resources": {},
        "api_process": None,
        "ui_process": None,
        "consent": {"state": "required", "provider": profile["provider"], "model": profile["model"], "endpoint": profile["endpoint"]},
    }
    run_path.mkdir(parents=True, exist_ok=True)
    (run_path / "evidence").mkdir()
    workspace = run_path / "runtime-home"
    workspace.mkdir()
    runtime["workspace_path"] = str(workspace)
    readme = ROOT / "README.md"
    readme_copy = workspace / "README.md"
    shutil.copyfile(readme, readme_copy)
    runtime["workspace_readme"] = {"path": str(readme_copy), "sha256": _digest(readme_copy), "source_sha256": _digest(readme)}
    child["HOME"] = str(workspace)
    child["HEXIS_BOB108_RUN_MANIFEST"] = str(run_path / "run-manifest.json")
    child["HEXIS_API_URL"] = runtime["api_url"]
    child["HEXIS_UI_ORIGIN"] = runtime["ui_url"]
    child["POSTGRES_DB"] = runtime["target"]["database"]
    child["POSTGRES_PORT"] = str(runtime["target"]["port"])
    child["POSTGRES_HOST"] = str(runtime["target"]["host"])
    child["POSTGRES_USER"] = str(runtime["target"]["user"])
    child["POSTGRES_ADMIN_DB"] = str(runtime["target"]["admin_database"])
    child["FAKE_EMBEDDINGS_URL"] = runtime["embedding_url"]
    selected_runtime = {
        "schema": "bob108-selected-runtime-v1",
        "run_id": run_id,
        "source_head": manifest["source_head"],
        "source_database": source["identity"],
        "source_profiles_sha256": source["profiles_sha256"],
        "selected_profile": dict(profile),
        "source_consent_matches": source["matching_consent_count"],
        "ports": ports,
        "database": runtime["database"],
        "admin_database": runtime["target"]["admin_database"],
        "admin_database_source": runtime["target"]["admin_database_source"],
        "credential_env_file": runtime["credential_env_file"],
        "credential_env_names": runtime["credential_env_names"],
        "config_selection_path": selection["_selected_path"],
        "background_profile_path": selection["background_llm_profile"]["path"],
        "configuration_hashes": runtime["configuration_hashes"] if "configuration_hashes" in runtime else selection["_config_hashes"],
        "materials_manifest_sha256": runtime["materials_manifest_sha256"],
        "seed_data_sha256": runtime["seed_data_sha256"],
        "workspace_path": str(workspace),
        "workspace_readme": runtime["workspace_readme"],
    }
    _atomic_json(run_path / "selected-runtime.json", selected_runtime)
    runtime_env_text = "\n".join([
        f"POSTGRES_HOST={runtime['target']['host']}",
        f"POSTGRES_PORT={runtime['target']['port']}",
        f"POSTGRES_USER={runtime['target']['user']}",
        f"POSTGRES_DB={runtime['target']['database']}",
        f"POSTGRES_ADMIN_DB={runtime['target']['admin_database']}",
        "POSTGRES_PASSWORD_ENV=POSTGRES_PASSWORD",
        f"DASHSCOPE_API_KEY_ENV={profile['api_key_env']}",
        f"HEXIS_API_URL={runtime['api_url']}",
        f"HEXIS_UI_ORIGIN={runtime['ui_url']}",
        f"EMBEDDING_SERVICE_URL={runtime['embedding_url']}",
    ]) + "\n"
    if values["POSTGRES_PASSWORD"] in runtime_env_text or values[profile["api_key_env"]] in runtime_env_text:
        raise ProbeError("runtime.env would contain an inline credential")
    (run_path / "runtime.env").write_text(runtime_env_text, encoding="utf-8")
    os.chmod(run_path / "runtime.env", 0o600)
    (run_path / "compose.yml").write_text(compose_text(project, run_id, ports, volumes), encoding="utf-8")
    _atomic_json(run_path / "run-manifest.json", manifest)

    env_file = Path(runtime["credential_env_file"])
    command = _compose_command(run_path, manifest, env_file)
    subprocess.run([*command, "up", "-d"], cwd=ROOT, check=True)
    try:
        ready = await _wait_owned_postgres(runtime | {"pytest": {"host": "127.0.0.1", "port": ports["pytest"], "user": values["POSTGRES_USER"], "database": "bob108_pytest"}}, values)
        identities = ready["postgres"]
        target = await _prepare_browser_database(
            runtime=runtime,
            selected_profile=profile,
            source=source,
            values=values,
            workspace=workspace,
        )
        manifest["service_identities"] = {
            "pytest": identities["pytest"],
            "browser_admin": identities["target"],
            "target": target["identity"],
        }
        manifest["migrations_applied"] = target["migrations_applied"]
        manifest["target_profile_readback"] = target["profile_readback"]
        manifest["embedding_service_url"] = target["embedding_service_url"]
        manifest["fake_embeddings_health"] = ready["fake_embeddings_health"]
        manifest["owned_resources"] = control_runtime._owned_resources(run_path, manifest)
        manifest["state"] = "awaiting_consent"
        _atomic_json(run_path / "run-manifest.json", manifest)
        return manifest
    except Exception as exc:
        manifest["state"] = "prepare_failed"
        manifest["failure_type"] = type(exc).__name__
        _atomic_json(run_path / "run-manifest.json", manifest)
        raise ProbeError("run-01 preparation failed; owned resources and evidence were preserved") from exc


def prepare_real(selection_path: str | Path, profile_path: str | Path, run_dir: str | Path) -> dict[str, Any]:
    result = asyncio.run(_prepare_real_async(selection_path, profile_path, run_dir))
    return {
        "run_id": result["run_id"],
        "state": result["state"],
        "database": result["runtime"]["database"],
        "ports": result["ports"],
    }


async def _seed_target_data(manifest: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    import asyncpg
    from core.cognitive_memory_api import CognitiveMemory, MemoryType

    runtime = manifest["runtime"]
    target = runtime["target"]
    seed_path = _absolute(runtime["seed_data_path"])
    if _digest(seed_path) != runtime["seed_data_sha256"]:
        raise ProbeError("selected seed-data.json changed after prepare-real")
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    memory_records = seed.get("memory_records")
    if not isinstance(memory_records, list) or not memory_records:
        raise ProbeError("the selected seed data has no memory records")
    pool = await asyncpg.create_pool(
        _dsn(target["host"], target["port"], target["database"], target["user"], values["POSTGRES_PASSWORD"]),
        min_size=1,
        max_size=2,
    )
    memory = CognitiveMemory(pool)
    created: list[dict[str, Any]] = []
    try:
        for record in memory_records:
            if record.get("type") != "semantic" or not isinstance(record.get("content"), str):
                raise ProbeError("the selected seed record must be a semantic memory with text content")
            memory_id = await memory.remember(
                record["content"],
                type=MemoryType.SEMANTIC,
                importance=0.8,
                source_attribution=record.get("source_attribution"),
            )
            created.append({"memory_id": str(memory_id), "type": "semantic", "seed_key": record.get("key")})
        evidence_source = seed.get("evidence_source")
        if not isinstance(evidence_source, dict):
            raise ProbeError("the selected seed evidence source is missing")
        evidence_path = _verify_file_ref(evidence_source)
        return {
            "seed_data_path": str(seed_path),
            "seed_data_sha256": _digest(seed_path),
            "memories": created,
            "evidence_source": {"path": str(evidence_path), "sha256": _digest(evidence_path)},
        }
    finally:
        await pool.close()


def _safe_consent_error_metadata(exc: Exception) -> dict[str, str]:
    """Classify only known validation failures; never persist exception text."""
    known_validation_errors = {
        "The model did not choose either consent or decline.": "decision_missing",
        "The model did not provide the required reason for its decision.": "reason_missing",
        "The model chose consent without providing the required signature.": "signature_missing",
    }
    classification = known_validation_errors.get(str(exc), "Unknown") if type(exc) is RuntimeError else "Unknown"
    stage = "response_validation" if classification != "Unknown" else "consent_flow"
    if classification == "Unknown":
        from core import llm
        sdk = llm.openai
        if sdk is not None and isinstance(exc, (sdk.APIConnectionError, sdk.APITimeoutError)):
            stage, classification = "provider_transport", "sdk_network_error"
        elif sdk is not None and isinstance(exc, sdk.AuthenticationError):
            stage, classification = "provider_http", "sdk_authentication_error"
        elif sdk is not None and isinstance(exc, sdk.APIStatusError):
            stage, classification = "provider_http", "sdk_http_error"
    return {
        "error_type": type(exc).__name__,
        "error_stage": stage,
        "error_classification": classification,
    }


def _assert_no_consent_runtime_history(path: Path, manifest: dict[str, Any]) -> None:
    evidence = path / "evidence"
    if manifest.get("api_process") is not None or manifest.get("ui_process") is not None or "readiness" in manifest:
        raise ProbeError("consent retry requires no API/UI process or readiness history")
    if any((evidence / name).exists() for name in ("api.log", "ui.log", "api-start.json", "ui-start.json", "readiness.json")) or (evidence / "cases").exists():
        raise ProbeError("consent retry requires no API/UI logs, startup, readiness, or case evidence")


def _first_failed_consent_summary(path: Path, manifest: dict[str, Any], profile: dict[str, Any], values: dict[str, str]) -> tuple[Path, dict[str, Any]]:
    if manifest.get("state") != "consent_error" or manifest.get("consent", {}).get("state") != "error":
        raise ProbeError("retry-consent only accepts the first consent_error state")
    count = manifest.get("consent_attempt_count", 1)
    if type(count) is not int or count != 1 or (path / "evidence" / "consent-retry-audit.json").exists():
        raise ProbeError("the authorised second consent attempt has already been consumed")
    _assert_no_consent_runtime_history(path, manifest)
    summary_path = path / "evidence" / "consent-summary.json"
    try:
        raw_text = summary_path.read_text(encoding="utf-8")
        summary = json.loads(raw_text)
    except (OSError, ValueError) as exc:
        raise ProbeError("the first consent error summary is missing or invalid") from exc
    allowed = {"run_id", "decision", "error_type", "error_stage", "error_classification", "provider", "model", "endpoint", "attempt_number"}
    if (
        not isinstance(summary, dict) or set(summary) - allowed
        or summary.get("run_id") != manifest["run_id"] or summary.get("decision") != "error"
        or not isinstance(summary.get("error_type"), str) or not summary["error_type"]
        or summary.get("attempt_number", 1) != 1
        or any(summary.get(key) != profile.get(key) for key in ("provider", "model", "endpoint"))
        or any(secret and secret in raw_text for key, secret in values.items() if key in SECRET_NAMES)
    ):
        raise ProbeError("the first consent summary does not prove a single matching safe failed attempt")
    archived = path / "evidence" / "consent-attempt-1.json"
    if archived.exists() and archived.read_bytes() != summary_path.read_bytes():
        raise ProbeError("the preserved first consent attempt differs from its original summary")
    if any((path / "evidence").glob("consent-attempt-[2-9]*")):
        raise ProbeError("additional consent attempt evidence already exists")
    return summary_path, summary


def _exclusive_json(path: Path, value: dict[str, Any]) -> None:
    try:
        with path.open("x", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            stream.write(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    except FileExistsError as exc:
        raise ProbeError("consent retry audit already exists; no additional request is allowed") from exc


def _preserve_first_consent_attempt(path: Path, manifest: dict[str, Any], profile: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    summary_path, _summary = _first_failed_consent_summary(path, manifest, profile, values)
    archive = path / "evidence" / "consent-attempt-1.json"
    if not archive.exists():
        try:
            with archive.open("xb") as stream:
                os.chmod(archive, 0o600)
                stream.write(summary_path.read_bytes())
        except FileExistsError:
            if archive.read_bytes() != summary_path.read_bytes():
                raise ProbeError("first consent summary preservation conflicted")
    source_record_path = path / "evidence" / "consent-attempt-1-source.json"
    if source_record_path.exists():
        record = json.loads(source_record_path.read_text(encoding="utf-8"))
        if record.get("run_id") != manifest["run_id"] or record.get("summary_sha256") != _digest(archive) or not record.get("source_fingerprints"):
            raise ProbeError("first consent source identity is invalid")
        return record
    record = {
        "run_id": manifest["run_id"], "attempt_number": 1,
        "summary": str(archive), "summary_sha256": _digest(archive),
        "manifest_sha256": _digest(path / "run-manifest.json"),
        "source_fingerprints": dict(manifest["source_fingerprints"]),
        "说明": "保留第一次已失败请求的原始摘要与当时登记的源码指纹；不推断远端是否收到或计费。",
    }
    _exclusive_json(source_record_path, record)
    return record


async def _validate_retry_services(path: Path, manifest: dict[str, Any], values: dict[str, str]) -> None:
    resources = control_runtime._owned_resources(path, manifest)
    if set(resources) != {"pytest-db", "browser-db", "fake-embeddings"} or any(
        not record.get("container_id") or record["container_id"] != manifest.get("owned_resources", {}).get(name, {}).get("container_id")
        for name, record in resources.items()
    ):
        raise ProbeError("consent retry requires the original owned service identities")
    target, pytest_binding = manifest["runtime"]["target"], manifest["runtime"]["pytest"]
    identities = manifest["service_identities"]
    if (
        target.get("host") != "127.0.0.1" or target.get("port") != manifest["ports"]["browser"]
        or target.get("database") != manifest["runtime"]["database"] or target.get("user") != values["POSTGRES_USER"]
        or pytest_binding.get("host") != "127.0.0.1" or pytest_binding.get("port") != manifest["ports"]["pytest"]
        or pytest_binding.get("database") != "bob108_pytest"
        or identities["target"].get("database") != target["database"]
        or identities["browser_admin"].get("database") != target["admin_database"]
        or identities["pytest"].get("database") != pytest_binding["database"]
        or not manifest.get("source_fingerprints")
        or not identities["target"].get("system_identifier")
        or identities["target"]["system_identifier"] != identities["browser_admin"].get("system_identifier")
        or identities["pytest"].get("system_identifier") == identities["target"]["system_identifier"]
    ):
        raise ProbeError("consent retry runtime or PostgreSQL identities are inconsistent")
    actual = await control_runtime._pg_identity(pytest_binding["host"], pytest_binding["port"], pytest_binding["database"], values["POSTGRES_USER"], values["POSTGRES_PASSWORD"])
    if any(actual.get(key) != identities["pytest"].get(key) for key in ("database", "system_identifier")):
        raise ProbeError("consent retry pytest PostgreSQL identity differs from its manifest")


async def _request_consent_async(run_dir: str | Path, *, retry: bool = False) -> dict[str, Any]:
    import asyncpg
    from core.init_api import run_consent_flow

    path, manifest, selected, _selection, values = load_run(run_dir)
    manifest_path = path / "run-manifest.json"
    if retry:
        _first_failed_consent_summary(path, manifest, selected["selected_profile"], values)
        await _validate_retry_services(path, manifest, values)
    elif manifest.get("state") != "awaiting_consent":
        raise ProbeError("request-consent requires this run to be prepared and awaiting a real model decision")
    elif manifest.get("consent_attempt_count", 0) != 0:
        raise ProbeError("request-consent cannot repeat a previously recorded attempt")
    profile = selected["selected_profile"]
    target = manifest["runtime"]["target"]
    conn = await asyncpg.connect(
        _dsn(target["host"], target["port"], target["database"], target["user"], values["POSTGRES_PASSWORD"]),
        timeout=5,
    )
    try:
        identity = await conn.fetchrow(
            "SELECT current_database() AS database,(SELECT system_identifier::text FROM pg_control_system()) AS system_identifier"
        )
        expected = manifest["service_identities"].get("target", {})
        if identity["database"] != manifest["runtime"]["database"] or identity["system_identifier"] != expected.get("system_identifier"):
            raise ProbeError("consent connection does not match the owned run-01 target database")
        existing = await conn.fetchrow(
            "SELECT id::text,decision,provider,model,endpoint,decided_at,response FROM consent_log "
            "WHERE provider=$1 AND model=$2 AND endpoint=$3 ORDER BY decided_at DESC LIMIT 1",
            profile["provider"], profile["model"], profile["endpoint"],
        )
        if existing is not None:
            raise ProbeError("target already has a matching consent row; refusing to issue a duplicate provider request")
        _assert_current_source_fingerprints(manifest)
        if retry:
            first_attempt = _preserve_first_consent_attempt(path, manifest, profile, values)
            _exclusive_json(path / "evidence" / "consent-retry-audit.json", {
                "run_id": manifest["run_id"], "attempt_number": 2, "max_total_attempts": 2,
                "授权": "用户明确授权额外一次真实 Qwen Plus consent 请求；不使用 override，不自动继续第三次请求。",
                "recorded_before_external_call": True, "first_attempt": first_attempt,
                "first_attempt_source_record_sha256": _digest(path / "evidence" / "consent-attempt-1-source.json"),
                "source_fingerprints": dict(manifest["source_fingerprints"]),
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
        manifest["consent_attempt_count"] = 2 if retry else 1
        llm_config = {
            "provider": profile["provider"],
            "model": profile["model"],
            "endpoint": profile["endpoint"],
            "api_key": values[profile["api_key_env"]],
        }
        manifest["state"] = "consent_requested"
        manifest["consent"] = {"state": "requesting", "provider": profile["provider"], "model": profile["model"], "endpoint": profile["endpoint"]}
        _atomic_json(manifest_path, manifest)
        try:
            _assert_current_source_fingerprints(manifest)
            flow = await run_consent_flow(conn, llm_config)
        except Exception as exc:
            error_metadata = _safe_consent_error_metadata(exc)
            manifest["state"] = "consent_error"
            manifest["consent"] = {"state": "error", **error_metadata}
            _atomic_json(manifest_path, manifest)
            _atomic_json(path / "evidence" / "consent-summary.json", {
                "run_id": manifest["run_id"], "decision": "error", "attempt_number": manifest["consent_attempt_count"], **error_metadata,
                "provider": profile["provider"], "model": profile["model"], "endpoint": profile["endpoint"],
            })
            raise ProbeError(f"real selected-provider consent request failed ({type(exc).__name__}); run stopped before API/UI") from exc

        consent_row = await conn.fetchrow(
            "SELECT id::text,decision,provider,model,endpoint,decided_at,response FROM consent_log "
            "WHERE provider=$1 AND model=$2 AND endpoint=$3 ORDER BY decided_at DESC LIMIT 1",
            profile["provider"], profile["model"], profile["endpoint"],
        )
        if consent_row is None:
            manifest["state"] = "consent_missing_record"
            _atomic_json(manifest_path, manifest)
            raise ProbeError("run_consent_flow returned without a target consent_log row")
        response_payload = _json_value(consent_row["response"])
        normalized = {
            "decision": consent_row["decision"],
            "provider": consent_row["provider"],
            "model": consent_row["model"],
            "endpoint": consent_row["endpoint"],
            "operator_override": bool(response_payload.get("operator_override")) if isinstance(response_payload, dict) else False,
        }
        try:
            validate_model_consent(normalized, profile)
            if flow.get("decision") != normalized["decision"]:
                raise ProbeError("consent flow decision differs from its durable target row")
        except ProbeError as exc:
            manifest["state"] = "consent_declined" if normalized["decision"] == "decline" else "consent_mismatch"
            manifest["consent"] = {"state": normalized["decision"], "consent_log_id": consent_row["id"]}
            _atomic_json(manifest_path, manifest)
            _atomic_json(path / "evidence" / "consent-summary.json", {
                "run_id": manifest["run_id"], "decision": normalized["decision"], "attempt_number": manifest["consent_attempt_count"],
                "consent_log_id": consent_row["id"], "provider": profile["provider"],
                "model": profile["model"], "endpoint": profile["endpoint"],
                "operator_override": normalized["operator_override"],
            })
            raise ProbeError(f"selected model consent was {normalized['decision']}; run stopped before API/UI") from exc

        consent_status = await conn.fetchval("SELECT get_agent_consent_status()")
        agent_status = _json_value(await conn.fetchval("SELECT get_agent_status()"))
        if consent_status != "consent" or not isinstance(agent_status, dict) or not agent_status.get("configured"):
            manifest["state"] = "consent_mismatch"
            manifest["consent"] = {"state": "readback_mismatch", "consent_log_id": consent_row["id"]}
            _atomic_json(manifest_path, manifest)
            raise ProbeError("target consent status/profile did not read back as active")

        try:
            seed = await _seed_target_data(manifest, values)
        except Exception as exc:
            manifest["state"] = "seed_failed"
            manifest["consent"] = {"state": "consent", "consent_log_id": consent_row["id"], "operator_override": False}
            manifest["seed_failure_type"] = type(exc).__name__
            _atomic_json(manifest_path, manifest)
            raise ProbeError("model consent was accepted but seed data failed; run and consent were preserved") from exc
        consent_summary = {
            "run_id": manifest["run_id"],
            "attempt_number": manifest["consent_attempt_count"],
            "decision": "consent",
            "consent_log_id": consent_row["id"],
            "decided_at": consent_row["decided_at"].isoformat(),
            "provider": profile["provider"],
            "model": profile["model"],
            "endpoint": profile["endpoint"],
            "operator_override": False,
            "consent_status_readback": consent_status,
            "agent_configured": True,
            "seed": seed,
        }
        _atomic_json(path / "evidence" / "consent-summary.json", consent_summary)
        manifest["state"] = "consent_accepted"
        manifest["consent"] = {"state": "consent", "consent_log_id": consent_row["id"], "decided_at": consent_row["decided_at"].isoformat(), "operator_override": False}
        manifest["seed"] = seed
        _atomic_json(manifest_path, manifest)
        return {"run_id": manifest["run_id"], "state": manifest["state"], "consent_log_id": consent_row["id"], "database": target["database"]}
    finally:
        await conn.close()


def request_consent(run_dir: str | Path) -> dict[str, Any]:
    return asyncio.run(_request_consent_async(run_dir))


def retry_consent(run_dir: str | Path) -> dict[str, Any]:
    return asyncio.run(_request_consent_async(run_dir, retry=True))


def _done_receipt_memory_uuid(action: dict[str, Any]) -> UUID | None:
    if action.get("tool_name") not in {"remember", "add_evidence"} or action.get("state") != "done":
        return None
    receipt = _json_value(action.get("result"))
    if not isinstance(receipt, dict) or receipt.get("state") != "done" or receipt.get("ok") is not True or receipt.get("error") is not None:
        return None
    data = receipt.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("memory_id"), str):
        raise ProbeError("done memory receipt lacks its authoritative data.memory_id")
    try:
        return UUID(data["memory_id"])
    except (ValueError, TypeError) as exc:
        raise ProbeError("done memory receipt data.memory_id is not a valid UUID") from exc


async def _bind_evidence_context_async(run_dir: str | Path, origin_session_id: str) -> dict[str, Any]:
    import asyncpg

    path, manifest, _selected, _selection, values = load_run(run_dir)
    if manifest.get("state") not in {"consent_accepted", "ready"}:
        raise ProbeError("bind-evidence-context requires an accepted, prepared target run")
    origin = UUID(origin_session_id)
    target = manifest["runtime"]["target"]
    conn = await asyncpg.connect(
        _dsn(target["host"], target["port"], target["database"], target["user"], values["POSTGRES_PASSWORD"]),
        timeout=5,
    )
    try:
        identity = await conn.fetchval("SELECT current_database()")
        if identity != manifest["runtime"]["database"]:
            raise ProbeError("evidence binding connection resolved to an unexpected target DB")
        if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM chat_sessions WHERE id=$1::uuid)", origin):
            raise ProbeError("selected add_evidence origin session does not exist in the target DB")
        rows = await conn.fetch(
            "SELECT command_id::text,tool_name,state,actual_call_id,result,origin_session_id::text "
            "FROM m5_actions WHERE origin_session_id=$1::uuid AND tool_name='remember' AND state='done' "
            "ORDER BY created_at DESC",
            origin,
        )
        if len(rows) != 1:
            raise ProbeError("add_evidence binding requires exactly one completed remember in this run session")
        action = dict(rows[0])
        memory_id = _done_receipt_memory_uuid(action)
        if memory_id is None:
            raise ProbeError("the completed remember receipt has no actual memory UUID")
        memory = await conn.fetchrow(
            "SELECT id::text,type,status,content FROM memories WHERE id=$1::uuid",
            memory_id,
        )
        if memory is None or UUID(str(memory["id"])) != memory_id or memory["type"] != "semantic" or memory["status"] != "active":
            raise ProbeError("the remember receipt does not identify an active semantic memory")
        seed = json.loads(Path(manifest["runtime"]["seed_data_path"]).read_text(encoding="utf-8"))
        source_reference = seed["evidence_source"]
        source_path = _verify_file_ref(source_reference)
        source = {
            "kind": "repository_document",
            "ref": str(source_path),
            "label": source_path.name,
        }
        context = {
            "run_id": manifest["run_id"],
            "database": identity,
            "origin_session_id": str(origin),
            "memory_id": str(memory_id),
            "memory_content": memory["content"],
            "remember_call_id": action["actual_call_id"],
            "target_memory": {"id": str(memory_id), "type": memory["type"], "status": memory["status"], "content": memory["content"]},
            "source": source,
            "source_sha256": _digest(source_path),
            "source_content": source_path.read_text(encoding="utf-8"),
            "scope": "本轮 target DB 中由 remember done receipt 与主库只读核验共同证明的宿主绑定上下文",
        }
        output = path / "evidence-context.json"
        _atomic_json(output, context)
        manifest["evidence_context"] = {"path": str(output), "memory_id": str(memory_id), "source_sha256": context["source_sha256"]}
        _atomic_json(path / "run-manifest.json", manifest)
        return {key: context[key] for key in ("run_id", "database", "origin_session_id", "memory_id", "remember_call_id", "source", "source_sha256")}
    finally:
        await conn.close()


def bind_evidence_context(run_dir: str | Path, origin_session_id: str) -> dict[str, Any]:
    return asyncio.run(_bind_evidence_context_async(run_dir, origin_session_id))


def _install_evidence_context_hydrator(run_dir: Path) -> None:
    """Add selected run context only to its explicitly bound M4 origin session."""
    import apps.voice_realtime_harness as harness

    if getattr(harness, "_bob108_context_wrapper_installed", False):
        return
    original_build_runtime = harness._build_runtime

    def build_runtime_with_run_context(*args: Any, **kwargs: Any):
        coordinator, summary = original_build_runtime(*args, **kwargs)
        context_path = run_dir / "evidence-context.json"
        if not context_path.is_file():
            return coordinator, summary
        context = json.loads(context_path.read_text(encoding="utf-8"))
        session_id = kwargs.get("session_id")
        if str(session_id) != context.get("origin_session_id"):
            return coordinator, summary
        original_hydrator = coordinator._hydrator

        async def hydrate_with_bound_context(identity: Any, message: str):
            hydrated = await original_hydrator(identity, message)
            if context.get("run_id") != os.environ.get("HEXIS_BOB108_TOOL_ACTIVITY_RUN_ID"):
                raise ProbeError("evidence context run identity differs from the active API process")
            source_path = _absolute(context["source"]["ref"])
            if _digest(source_path) != context["source_sha256"]:
                raise ProbeError("evidence context source changed after binding")
            from dataclasses import replace
            from services.voice_instructions import load_m5_instructions, render_per_turn

            facts = {
                "target_memory": context["target_memory"],
                "lookup_kind": "direct_database_id",
                "evidence_source": context["source"],
                "source_content": context["source_content"],
                "scope": "这是本轮 target DB 中由宿主按真实 remember done UUID 读取并绑定的目标记忆；source 正文来自已核 SHA-256 的缓存文件。",
            }
            memory_context = hydrated.memory_context + "\n\n## 本轮宿主绑定的目标语义记忆与证据来源\n" + json.dumps(facts, ensure_ascii=False)
            rendered = render_per_turn(
                load_m5_instructions(),
                now=hydrated.temporal_context,
                persona=hydrated.persona_context,
                continuity=hydrated.continuity_context,
                memory=memory_context,
            )
            return replace(
                hydrated,
                per_response_instructions=rendered.text,
                memory_context=memory_context,
                token_count=rendered.token_count,
                tokenizer=rendered.tokenizer,
                tokenizer_is_model_exact=rendered.tokenizer_is_model_exact,
            )

        coordinator._hydrator = hydrate_with_bound_context
        return coordinator, summary

    harness._build_runtime = build_runtime_with_run_context
    harness._bob108_context_wrapper_installed = True


def load_run(run_dir: str | Path) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, str]]:
    path = _absolute(run_dir)
    manifest_path, selected_path = path / "run-manifest.json", path / "selected-runtime.json"
    if not manifest_path.is_file() or not selected_path.is_file():
        raise ProbeError("run-01 manifest or selected-runtime file is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected = json.loads(selected_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "bob108-tool-activity-run-v1" or selected.get("schema") != "bob108-selected-runtime-v1":
        raise ProbeError("run-01 manifest schema is unsupported")
    if manifest.get("run_id") != selected.get("run_id") or manifest.get("run_directory") != str(path):
        raise ProbeError("run-01 selected and run manifest identities differ")
    selection_path = selected.get("config_selection_path")
    if not isinstance(selection_path, str) or not selection_path:
        raise ProbeError("run-01 selected config path is missing")
    selection, profile, values, _materials = load_selected_inputs(
        selection_path, selected["background_profile_path"]
    )
    if selection.get("_config_hashes") != selected.get("configuration_hashes"):
        raise ProbeError("selected configuration hashes changed after prepare-real")
    if profile != selected.get("selected_profile"):
        raise ProbeError("selected background profile changed after prepare-real")
    validate_target_database(manifest["runtime"]["database"], source_database=manifest["runtime"]["source"]["database"], run_id=manifest["run_id"])
    return path, manifest, selected, selection, values


def _child_environment(
    path: Path,
    manifest: dict[str, Any],
    selection: dict[str, Any],
    profile: dict[str, Any],
    values: dict[str, str],
) -> dict[str, str]:
    runtime, child = build_runtime_bindings(
        selection,
        profile,
        values,
        run_id=manifest["run_id"],
        ports=manifest["ports"],
    )
    expected = manifest["runtime"]
    if runtime["database"] != expected["database"] or runtime["target"]["port"] != expected["target"]["port"]:
        raise ProbeError("child environment database or port binding differs from run manifest")
    home = path / "runtime-home"
    if home.is_symlink() or home.resolve().parent != path.resolve() or not home.is_dir():
        raise ProbeError("run-01 runtime HOME must remain inside its owned directory")
    child.update({
        "HOME": str(home),
        "PYTHONPATH": str(ROOT),
        "HEXIS_BOB108_RUN_MANIFEST": str(path / "run-manifest.json"),
        "HEXIS_BOB108_TOOL_ACTIVITY_RUN_ID": str(manifest["run_id"]),
        "BOB108_RUNTIME_DIAGNOSTICS": "1",
        "BOB108_RUN_ID": str(manifest["run_id"]),
        "BOB108_EXPECTED_DATABASE": expected["database"],
        "BOB108_EXPECTED_SYSTEM_IDENTIFIER": str(manifest["service_identities"]["target"]["system_identifier"]),
        "NEXT_TELEMETRY_DISABLED": "1",
    })
    for key in ("PATH", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "WINDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        if key in os.environ:
            child[key] = os.environ[key]
    child["XDG_CACHE_HOME"] = str(path / "xdg-cache")
    for key in ("HEXIS_INSTANCE", "HEXIS_M5_ACCEPTANCE_RUN", "HEXIS_VOICE_BARGE_IN_ENV", "HEXIS_CONSENT_MOCK", "HEXIS_TEST_CONSENT_DECISION"):
        child.pop(key, None)
    if any(key in child for key in ("HEXIS_M5_ACCEPTANCE_RUN", "HEXIS_VOICE_BARGE_IN_ENV")):
        raise ProbeError("legacy M5 or M6 acceptance mode is not allowed in BOB-108 run-01")
    return child


def _assert_current_source_fingerprints(manifest: dict[str, Any]) -> None:
    for relative, expected in manifest.get("source_fingerprints", {}).items():
        path = ROOT / relative
        if not path.is_file() or _digest(path) != expected:
            raise ProbeError(f"run source fingerprint changed after prepare-real: {relative}")


def _api_child(run_dir: str | Path) -> None:
    path, manifest, _selected, _selection, _values = load_run(run_dir)
    if manifest.get("state") != "consent_accepted":
        raise ProbeError("API cannot start before a matching real consent is read back")
    _assert_current_source_fingerprints(manifest)
    import uvicorn
    import apps.hexis_api as hexis_api

    _install_evidence_context_hydrator(path)

    async def runtime_identity():
        if os.environ.get("HEXIS_BOB108_TOOL_ACTIVITY_RUN_ID") != manifest["run_id"] or hexis_api._pool is None:
            raise ProbeError("run identity or API DB pool is unavailable")
        async with hexis_api._pool.acquire() as conn:
            identity = await conn.fetchrow(
                "SELECT current_database() AS database,inet_server_addr()::text AS address,"
                "inet_server_port() AS port,(SELECT system_identifier::text FROM pg_control_system()) AS system_identifier"
            )
        return {
            "run_id": manifest["run_id"],
            "source_fingerprints": dict(manifest["source_fingerprints"]),
            "database_identity": dict(identity),
            "task_worker_ready": bool(hexis_api._m5_task_worker and hexis_api._m5_task_worker.running),
            "lightweight_worker_ready": bool(hexis_api._m5_lightweight_executor and hexis_api._m5_lightweight_executor.running),
        }

    async def task_state(task_id: str):
        if os.environ.get("HEXIS_BOB108_TOOL_ACTIVITY_RUN_ID") != manifest["run_id"] or hexis_api._pool is None:
            raise ProbeError("run identity or API DB pool is unavailable")
        try:
            task_uuid = UUID(task_id)
        except ValueError as exc:
            raise ProbeError("task_id must be a UUID") from exc
        async with hexis_api._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT t.id::text AS task_id,t.task_session_id::text,t.product_state,"
                "t.current_work_execution_id::text,w.id::text AS work_execution_id,w.status AS work_status "
                "FROM m5_tasks t LEFT JOIN LATERAL (SELECT id,status FROM m5_work_rounds WHERE task_id=t.id "
                "ORDER BY started_at DESC NULLS LAST,created_at DESC LIMIT 1) w ON TRUE WHERE t.id=$1::uuid",
                task_uuid,
            )
            if row is None:
                raise ProbeError("task_id is not present in the run target database")
            events = await conn.fetch(
                "SELECT event_type,event_id::text,session_id::text,task_id,call_id,stream_id,seq,"
                "payload->>'tool_name' AS tool_name,payload->>'success' AS success,payload->>'error' AS error,recorded_at "
                "FROM behavior_events WHERE task_id=$1::text AND event_type IN ('TOOL_START','TOOL_RESULT') "
                "ORDER BY recorded_at,producer_seq",
                str(task_uuid),
            )
        return {**dict(row), "tool_events": [{**dict(event), "recorded_at": event["recorded_at"].isoformat()} for event in events]}

    if not any(getattr(route, "path", None) == "/__test__/tool-activity/identity" for route in hexis_api.app.routes):
        hexis_api.app.add_api_route("/__test__/tool-activity/identity", runtime_identity, methods=["GET"])
        hexis_api.app.add_api_route("/__test__/tool-activity/task-state", task_state, methods=["GET"])
    port = manifest["ports"]["api"]
    uvicorn.run(hexis_api.app, host="127.0.0.1", port=port, log_level="info")


def _process_command(pid: int) -> str | None:
    completed = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return completed.stdout.strip() or None


def _owned_process_alive(process: Any, *, run_dir: Path, role: str) -> bool:
    if not isinstance(process, dict) or process.get("run_id") != json.loads((run_dir / "run-manifest.json").read_text())["run_id"]:
        return False
    pid = process.get("pid")
    command = _process_command(pid) if isinstance(pid, int) else None
    if not command:
        return False
    if role == "api":
        return "-m apps.bob108_tool_activity_probe _api-child" in command and str(run_dir) in command
    ui_script = str(run_dir / "ui-src/node_modules/next/dist/bin/next")
    return ui_script in command and f"--port {json.loads((run_dir / 'run-manifest.json').read_text())['ports']['ui']}" in command


def serve_api(run_dir: str | Path) -> dict[str, Any]:
    path, manifest, _selected, selection, values = load_run(run_dir)
    if manifest.get("state") != "consent_accepted":
        raise ProbeError("serve-api is gated on real matching provider consent")
    if manifest.get("api_process") and _owned_process_alive(manifest["api_process"], run_dir=path, role="api"):
        raise ProbeError("the run-owned API process is already running")
    _assert_current_source_fingerprints(manifest)
    environment = _child_environment(path, manifest, selection, manifest["runtime"]["provider_profile"], values)
    log_path = path / "evidence" / "api.log"
    log = log_path.open("ab")
    command = [sys.executable, "-m", "apps.bob108_tool_activity_probe", "_api-child", "--run-dir", str(path)]
    process = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    log.close()
    manifest["api_process"] = {"pid": process.pid, "run_id": manifest["run_id"], "command": command, "log": str(log_path)}
    try:
        _atomic_json(path / "run-manifest.json", manifest)
    except BaseException:
        _stop_run_process(manifest["api_process"], run_dir=path, role="api")
        raise
    return {"run_id": manifest["run_id"], "api_pid": process.pid, "api_url": manifest["runtime"]["api_url"]}


def _copy_run_ui(path: Path) -> Path:
    source, target = ROOT / "hexis-ui", path / "ui-src"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("node_modules", ".next", ".turbo", ".env", ".env.*", "playwright-report", "test-results"),
    )
    modules = target / "node_modules"
    if modules.exists() or modules.is_symlink():
        if not modules.is_symlink() or modules.resolve() != (source / "node_modules").resolve():
            raise ProbeError("run UI dependencies differ from the selected repository node_modules")
    else:
        modules.symlink_to(source / "node_modules", target_is_directory=True)
    return target


def serve_ui(run_dir: str | Path) -> dict[str, Any]:
    path, manifest, _selected, selection, values = load_run(run_dir)
    if manifest.get("state") != "consent_accepted":
        raise ProbeError("serve-ui is gated on real matching provider consent")
    if not manifest.get("api_process") or not _owned_process_alive(manifest["api_process"], run_dir=path, role="api"):
        raise ProbeError("serve-ui requires the run-owned API process to be running")
    if manifest.get("ui_process") and _owned_process_alive(manifest["ui_process"], run_dir=path, role="ui"):
        raise ProbeError("the run-owned UI process is already running")
    _assert_current_source_fingerprints(manifest)
    ui_root = _copy_run_ui(path)
    for relative, expected_hash in manifest.get("source_fingerprints", {}).items():
        if relative.startswith("hexis-ui/"):
            copied = ui_root / relative.removeprefix("hexis-ui/")
            if not copied.is_file() or _digest(copied) != expected_hash:
                raise ProbeError(f"isolated UI source copy differs from its manifest fingerprint: {relative}")
    environment = _child_environment(path, manifest, selection, manifest["runtime"]["provider_profile"], values)
    environment.pop(selection["credentials"]["background_ref"], None)
    environment["HEXIS_API_URL"] = manifest["runtime"]["api_url"]
    environment["HEXIS_DATABASE_URL"] = _dsn(
        manifest["runtime"]["target"]["host"], manifest["runtime"]["target"]["port"],
        manifest["runtime"]["target"]["database"], manifest["runtime"]["target"]["user"], values["POSTGRES_PASSWORD"],
    )
    environment["BOB108_EXPECTED_SYSTEM_IDENTIFIER"] = manifest["service_identities"]["target"]["system_identifier"]
    node = shutil.which("node")
    if not node:
        raise ProbeError("Node.js is not available for the selected UI runtime")
    command = [node, str(ui_root / "node_modules/next/dist/bin/next"), "dev", "--webpack", "--hostname", "127.0.0.1", "--port", str(manifest["ports"]["ui"])]
    log_path = path / "evidence" / "ui.log"
    log = log_path.open("ab")
    process = subprocess.Popen(command, cwd=ui_root, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    log.close()
    manifest["ui_process"] = {"pid": process.pid, "run_id": manifest["run_id"], "command": command, "log": str(log_path), "source_root": str(ui_root)}
    try:
        _atomic_json(path / "run-manifest.json", manifest)
    except BaseException:
        _stop_run_process(manifest["ui_process"], run_dir=path, role="ui")
        raise
    return {"run_id": manifest["run_id"], "ui_pid": process.pid, "ui_url": manifest["runtime"]["ui_url"]}


def _http_json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None, timeout: float = 5) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status < 200 or response.status >= 300:
            raise ProbeError(f"readiness endpoint returned HTTP {response.status}")
        value = json.loads(response.read().decode("utf-8"))
        if not isinstance(value, dict):
            raise ProbeError("readiness endpoint returned a non-object")
        return value


def _wait_json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None, timeout_seconds: float = 120) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return _http_json(url, method=method, payload=payload, timeout=4)
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    raise ProbeError(f"readiness endpoint did not become available ({type(last_error).__name__ if last_error else 'timeout'})")


async def _session_belongs_to_target(
    runtime: dict[str, Any], values: dict[str, str], session_id: str
) -> bool:
    import asyncpg

    target = runtime["target"]
    conn = await asyncpg.connect(
        _dsn(target["host"], target["port"], target["database"], target["user"], values["POSTGRES_PASSWORD"]),
        timeout=5,
    )
    try:
        return bool(await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM chat_sessions WHERE id=$1::uuid AND surface='web')", UUID(session_id)
        ))
    finally:
        await conn.close()


def _write_readiness_record(evidence_dir: Path, name: str, value: dict[str, Any], *, launcher_repaired: bool, e2e_repaired: bool = False, harness_retried: bool = False, cases_refreshed: bool = False, evidence_replayed: bool = False, final_verification_refreshed: bool = False, owned_resumed: bool = False) -> Path:
    output = evidence_dir / f"{name}.json"
    if output.exists():
        if json.loads(output.read_text(encoding="utf-8")) == value:
            return output
        if not launcher_repaired:
            raise ProbeError("readiness evidence changed without an audited launcher repair")
        suffix = "after-resume-owned" if owned_resumed else ("after-final-verification" if final_verification_refreshed else ("after-evidence-replay" if evidence_replayed else ("after-case-refresh" if cases_refreshed else ("after-harness-retry" if harness_retried else ("after-e2e-repair" if e2e_repaired else "after-launcher-repair")))))
        output = evidence_dir / f"{name}-{suffix}.json"
        if output.exists():
            if json.loads(output.read_text(encoding="utf-8")) == value:
                return output
            raise ProbeError("supplemental readiness evidence changed after launcher repair")
    _exclusive_json(output, value)
    return output


def _verified_repair_audit(manifest: dict[str, Any], field: str) -> dict[str, Any]:
    ref = manifest.get(field)
    if not isinstance(ref, dict):
        raise ProbeError("required prior repair audit is missing")
    _verify_file_ref(ref)
    audit = json.loads(Path(ref["path"]).read_text(encoding="utf-8"))
    if audit.get("run_id") != manifest["run_id"]:
        raise ProbeError("repair audit belongs to a different run")
    _verify_file_ref(audit["original_manifest"])
    for retained in audit["retained_original_evidence"].values():
        _verify_file_ref(retained)
    return audit


def check_readiness(run_dir: str | Path) -> dict[str, Any]:
    path, manifest, _selected, _selection, values = load_run(run_dir)
    if manifest.get("state") not in {"consent_accepted", "ready"}:
        raise ProbeError("readiness is gated on accepted real model consent")
    if not _owned_process_alive(manifest.get("api_process"), run_dir=path, role="api"):
        raise ProbeError("run-owned API process identity is not alive")
    if not _owned_process_alive(manifest.get("ui_process"), run_dir=path, role="ui"):
        raise ProbeError("run-owned UI process identity is not alive")
    _assert_current_source_fingerprints(manifest)
    runtime = manifest["runtime"]
    api_identity = _wait_json(f"{runtime['api_url']}/__test__/tool-activity/identity")
    expected = manifest["service_identities"]["target"]
    observed_api = api_identity.get("database_identity", {})
    if any(observed_api.get(key) != expected.get(key) for key in ("database", "system_identifier")):
        raise ProbeError("actual API pool identity differs from the run target database")
    if api_identity.get("run_id") != manifest["run_id"]:
        raise ProbeError("actual API process returned a different run_id")
    if manifest.get("launcher_repair"):
        audit = _verified_repair_audit(manifest, "launcher_repair")
        launcher_hash = audit.get("current_launcher_sha256")
        if manifest.get("e2e_repair"):
            e2e_audit = _verified_repair_audit(manifest, "e2e_repair")
            if e2e_audit["changes"]["apps/bob108_tool_activity_probe.py"]["previous"] != launcher_hash:
                raise ProbeError("E2E repair does not preserve the prior launcher baseline")
            expected_sources = dict(e2e_audit["previous_source_fingerprints"])
            for relative, change in e2e_audit["changes"].items():
                if relative not in {"apps/bob108_tool_activity_probe.py", "hexis-ui/e2e/tool-activity.spec.ts", "hexis-ui/lib/tool-activity-startup.test.ts"} or expected_sources.get(relative) != change["previous"]:
                    raise ProbeError("E2E repair source change exceeds its allowed scope")
                expected_sources[relative] = change["current"]
            if manifest.get("harness_retry"):
                retry_audit = _verified_repair_audit(manifest, "harness_retry")
                if retry_audit["previous_source_fingerprints"] != expected_sources:
                    raise ProbeError("harness retry does not preserve the previous E2E source baseline")
                for relative, change in retry_audit["changes"].items():
                    if relative not in {"apps/bob108_tool_activity_probe.py", "hexis-ui/e2e/tool-activity.spec.ts", "hexis-ui/lib/tool-activity-startup.test.ts"} or expected_sources.get(relative) != change["previous"]:
                        raise ProbeError("harness retry source change exceeds its allowed scope")
                    expected_sources[relative] = change["current"]
            if manifest.get("case_refresh"):
                case_audit = _verified_repair_audit(manifest, "case_refresh")
                if case_audit["previous_source_fingerprints"] != expected_sources:
                    raise ProbeError("case refresh does not preserve the previous harness source baseline")
                for relative, change in case_audit["changes"].items():
                    if relative not in {"apps/bob108_tool_activity_probe.py", "hexis-ui/e2e/tool-activity.spec.ts", "hexis-ui/lib/tool-activity-startup.test.ts"} or expected_sources.get(relative) != change["previous"]:
                        raise ProbeError("case refresh source change exceeds its allowed scope")
                    expected_sources[relative] = change["current"]
            if manifest.get("evidence_replay"):
                replay_audit = _verified_repair_audit(manifest, "evidence_replay")
                if replay_audit["previous_source_fingerprints"] != expected_sources:
                    raise ProbeError("evidence replay does not preserve the prior source baseline")
                for relative, change in replay_audit["changes"].items():
                    if relative not in {"apps/bob108_tool_activity_probe.py", "hexis-ui/e2e/tool-activity.spec.ts", "hexis-ui/lib/tool-activity-startup.test.ts"} or expected_sources.get(relative) != change["previous"]:
                        raise ProbeError("evidence replay source change exceeds its allowed scope")
                    expected_sources[relative] = change["current"]
            if manifest.get("final_verification_refresh"):
                final_audit = _verified_repair_audit(manifest, "final_verification_refresh")
                if final_audit["previous_source_fingerprints"] != expected_sources or set(final_audit["changes"]) != set(FINAL_VERIFICATION_SOURCE_PATHS):
                    raise ProbeError("final verification refresh source baseline or scope differs")
                for relative, change in final_audit["changes"].items():
                    if expected_sources.get(relative) != change["previous"]:
                        raise ProbeError("final verification refresh does not preserve its prior source hash")
                    expected_sources[relative] = change["current"]
            expected_sources = _sources_after_owned_resume(manifest, expected_sources)
            if expected_sources != manifest["source_fingerprints"]:
                raise ProbeError("E2E repair audit does not bind current sources")
        elif launcher_hash != manifest["source_fingerprints"].get("apps/bob108_tool_activity_probe.py"):
            raise ProbeError("launcher repair audit does not bind the current run source")
        if api_identity.get("source_fingerprints") != manifest["source_fingerprints"]:
            raise ProbeError("actual repaired API source baseline differs from the run manifest")
    if not api_identity.get("task_worker_ready") or not api_identity.get("lightweight_worker_ready"):
        raise ProbeError("the actual API process task/lightweight workers are not ready")

    ui_identity = _wait_json(f"{runtime['ui_url']}/api/__test__/tool-activity/identity")
    observed_ui = ui_identity.get("database_identity", {})
    if any(observed_ui.get(key) != expected.get(key) for key in ("database", "system_identifier")):
        raise ProbeError("actual UI Prisma identity differs from the run target database")
    if ui_identity.get("run_id") != manifest["run_id"]:
        raise ProbeError("actual UI process returned a different run_id")

    session_id = manifest.get("origin_session_id")
    if session_id is None:
        origin = _http_json(f"{runtime['ui_url']}/api/chat/session", method="POST")
        session_id = origin.get("session_id")
    if not isinstance(session_id, str):
        raise ProbeError("UI did not create an owned readiness origin session")
    if not asyncio.run(_session_belongs_to_target(runtime, values, session_id)):
        raise ProbeError("UI readiness origin session does not belong to the run target database")
    api_start = {
        "run_id": manifest["run_id"],
        "database_identity": observed_api,
        "task_worker_ready": api_identity["task_worker_ready"],
        "lightweight_worker_ready": api_identity["lightweight_worker_ready"],
        "source_fingerprints": manifest["source_fingerprints"],
    }
    ui_start = {"run_id": manifest["run_id"], "database_identity": observed_ui, "origin_session_id": session_id}
    launcher_repaired = bool(manifest.get("launcher_repair"))
    _write_readiness_record(path / "evidence", "api-start", api_start, launcher_repaired=launcher_repaired or bool(manifest.get("resume_owned")), e2e_repaired=bool(manifest.get("e2e_repair")), harness_retried=bool(manifest.get("harness_retry")), cases_refreshed=bool(manifest.get("case_refresh")), evidence_replayed=bool(manifest.get("evidence_replay")), final_verification_refreshed=bool(manifest.get("final_verification_refresh")), owned_resumed=bool(manifest.get("resume_owned")))
    _write_readiness_record(path / "evidence", "ui-start", ui_start, launcher_repaired=launcher_repaired or bool(manifest.get("resume_owned")), e2e_repaired=bool(manifest.get("e2e_repair")), harness_retried=bool(manifest.get("harness_retry")), cases_refreshed=bool(manifest.get("case_refresh")), evidence_replayed=bool(manifest.get("evidence_replay")), final_verification_refreshed=bool(manifest.get("final_verification_refresh")), owned_resumed=bool(manifest.get("resume_owned")))
    manifest["origin_session_id"] = session_id
    manifest["readiness"] = {"api": api_start, "ui": ui_start}
    manifest["state"] = "ready"
    _atomic_json(path / "run-manifest.json", manifest)
    return {"run_id": manifest["run_id"], "state": manifest["state"], "database": expected["database"], "origin_session_id": session_id}


async def _validate_launcher_repair_consent(manifest: dict[str, Any], values: dict[str, str], *, allow_tool_events: bool = False) -> None:
    import asyncpg

    target = manifest["runtime"]["target"]
    conn = await asyncpg.connect(_dsn(target["host"], target["port"], target["database"], target["user"], values["POSTGRES_PASSWORD"]), timeout=5)
    try:
        identity = await conn.fetchrow("SELECT current_database() AS database,(SELECT system_identifier::text FROM pg_control_system()) AS system_identifier")
        expected = manifest["service_identities"]["target"]
        if any(identity[key] != expected[key] for key in ("database", "system_identifier")):
            raise ProbeError("launcher repair reached a different target database")
        row = await conn.fetchrow("SELECT decision,provider,model,endpoint,response FROM consent_log WHERE id=$1::uuid", UUID(manifest["consent"]["consent_log_id"]))
        if row is None:
            raise ProbeError("launcher repair requires the original durable consent row")
        response = _json_value(row["response"])
        if not isinstance(response, dict):
            raise ProbeError("launcher repair consent response is invalid")
        validate_model_consent({**dict(row), "operator_override": bool(response.get("operator_override"))}, manifest["runtime"]["provider_profile"])
        if await conn.fetchval("SELECT get_agent_consent_status()") != "consent":
            raise ProbeError("launcher repair target consent is no longer active")
        if not allow_tool_events and await conn.fetchval("SELECT EXISTS(SELECT 1 FROM behavior_events WHERE event_type IN ('TOOL_START','TOOL_RESULT'))"):
            raise ProbeError("launcher repair is forbidden after real tool event evidence")
    finally:
        await conn.close()


def repair_ready_launcher(run_dir: str | Path, *, reason: str) -> dict[str, Any]:
    """仅在真实工具输入前审计 launcher 修复，原证据保留并只重启本轮 API。"""
    path, manifest, _selected, _selection, values = load_run(run_dir)
    evidence = path / "evidence"
    audit_path = evidence / "launcher-repair-audit.json"
    archive_path = evidence / "launcher-repair-original-manifest.json"
    if not isinstance(reason, str) or not reason.strip() or any(secret and secret in reason for key, secret in values.items() if key in SECRET_NAMES):
        raise ProbeError("launcher repair requires a credential-free reason")
    consent = manifest.get("consent", {})
    if manifest.get("state") != "ready" or consent.get("state") != "consent" or consent.get("operator_override") is not False or not consent.get("consent_log_id"):
        raise ProbeError("launcher repair requires a ready run with original real consent")
    if audit_path.exists() or archive_path.exists() or manifest.get("launcher_repair"):
        raise ProbeError("only one audited ready launcher repair is allowed")
    if any((evidence / "cases").glob("*.json")):
        raise ProbeError("launcher repair is forbidden after case evidence")
    launcher = "apps/bob108_tool_activity_probe.py"
    old = dict(manifest.get("source_fingerprints", {}))
    if launcher not in old:
        raise ProbeError("launcher repair source baseline is missing")
    for relative, expected in old.items():
        source = ROOT / relative
        if not source.is_file() or (relative != launcher and _digest(source) != expected):
            raise ProbeError(f"launcher repair rejects non-launcher source drift: {relative}")
        if relative.startswith("hexis-ui/"):
            copied = path / "ui-src" / relative.removeprefix("hexis-ui/")
            if not copied.is_file() or _digest(copied) != expected:
                raise ProbeError("launcher repair rejects isolated UI copy drift")
    current = _digest(ROOT / launcher)
    if current == old[launcher]:
        raise ProbeError("launcher repair has no launcher change to record")
    for role in ("api", "ui"):
        if not _owned_process_alive(manifest.get(f"{role}_process"), run_dir=path, role=role):
            raise ProbeError(f"launcher repair requires the original owned {role} process")
        url = manifest["runtime"][f"{role}_url"] + ("/__test__/tool-activity/identity" if role == "api" else "/api/__test__/tool-activity/identity")
        observed = _http_json(url)
        expected = manifest["service_identities"]["target"]
        if observed.get("run_id") != manifest["run_id"] or any(observed.get("database_identity", {}).get(key) != expected.get(key) for key in ("database", "system_identifier")):
            raise ProbeError(f"launcher repair observed a different {role} identity")
    retained = {}
    for name in ("consent-summary.json", "api-start.json", "ui-start.json"):
        artifact = evidence / name
        if not artifact.is_file():
            raise ProbeError("launcher repair original evidence is missing")
        retained[name] = {"path": str(artifact), "sha256": _digest(artifact)}
    api_start = json.loads((evidence / "api-start.json").read_text(encoding="utf-8"))
    if api_start.get("source_fingerprints") != old:
        raise ProbeError("launcher repair original API source baseline differs")
    asyncio.run(_validate_launcher_repair_consent(manifest, values))
    before = (path / "run-manifest.json").read_bytes()
    with archive_path.open("xb") as stream:
        os.chmod(archive_path, 0o600)
        stream.write(before)
    audit = {
        "run_id": manifest["run_id"], "reason": reason.strip(),
        "original_manifest": {"path": str(archive_path), "sha256": _digest(archive_path)},
        "previous_source_fingerprints": old, "current_launcher_sha256": current,
        "retained_original_evidence": retained, "previous_api_process": manifest["api_process"],
        "unchanged_ui_process": manifest["ui_process"],
        "provider_requested": False, "consent_written": False, "postgres_stopped": False,
        "说明": "仅修复本轮 launcher；原同意、原 API/UI 启动证据与源码指纹保留，重启精确身份的本轮 API 以加载新 launcher。",
    }
    _exclusive_json(audit_path, audit)
    _stop_run_process(manifest["api_process"], run_dir=path, role="api")
    manifest["source_fingerprints"][launcher] = current
    manifest["api_process"] = None
    manifest["state"] = "consent_accepted"
    manifest["launcher_repair"] = {"path": str(audit_path), "sha256": _digest(audit_path)}
    _atomic_json(path / "run-manifest.json", manifest)
    result = {**serve_api(path), "provider_requested": False, "ui_restarted": False, "postgres_stopped": False}
    _exclusive_json(evidence / "launcher-repair-result.json", result)
    return result


async def _validate_empty_e2e_repair_target(manifest: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    import asyncpg

    await _validate_launcher_repair_consent(manifest, values)
    target = manifest["runtime"]["target"]
    conn = await asyncpg.connect(_dsn(target["host"], target["port"], target["database"], target["user"], values["POSTGRES_PASSWORD"]), timeout=5)
    try:
        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM m5_actions UNION ALL SELECT 1 FROM m5_tasks UNION ALL SELECT 1 FROM m5_work_rounds UNION ALL SELECT 1 FROM chat_messages)"):
            raise ProbeError("E2E repair is forbidden after action, task, work or message evidence")
        sessions = await conn.fetch("SELECT id::text,surface,external_id,status,metadata,created_at,cleared_at FROM chat_sessions ORDER BY created_at")
        consent_at = datetime.fromisoformat(manifest["consent"]["decided_at"])
        if not sessions:
            raise ProbeError("E2E repair requires this run's empty web sessions")
        for row in sessions:
            metadata = _json_value(row["metadata"])
            if row["surface"] != "web" or row["external_id"] is not None or row["status"] != "active" or row["cleared_at"] is not None or row["created_at"] < consent_at or not isinstance(metadata, dict) or metadata.get("source") != "web" or metadata.get("created_by") != "user":
                raise ProbeError("E2E repair found a session outside this run's empty web session scope")
        if manifest.get("origin_session_id") not in {row["id"] for row in sessions}:
            raise ProbeError("E2E repair origin session does not belong to the verified empty sessions")
        return {"empty_session_ids": [row["id"] for row in sessions], "actions_tasks_messages_absent": True}
    finally:
        await conn.close()


def repair_ready_e2e(run_dir: str | Path, *, reason: str, expected_e2e_sha256: str, interrupted_trace: str | Path) -> dict[str, Any]:
    """一次性补充 E2E 修复审计；仅允许 spec 和本命令自身 launcher 变更。"""
    path, manifest, _selected, _selection, values = load_run(run_dir)
    evidence = path / "evidence"
    audit_path, archive_path = evidence / "e2e-repair-audit.json", evidence / "e2e-repair-original-manifest.json"
    if not isinstance(reason, str) or not reason.strip() or any(secret and secret in reason for key, secret in values.items() if key in SECRET_NAMES):
        raise ProbeError("E2E repair requires a credential-free reason")
    consent = manifest.get("consent", {})
    if manifest.get("state") != "ready" or consent.get("state") != "consent" or consent.get("operator_override") is not False:
        raise ProbeError("E2E repair requires a ready run with real consent")
    if manifest.get("e2e_repair") or audit_path.exists() or archive_path.exists():
        raise ProbeError("only one ready E2E repair is allowed")
    if any((evidence / "cases").glob("*.json")):
        raise ProbeError("E2E repair is forbidden after case evidence")
    first = _verified_repair_audit(manifest, "launcher_repair")
    launcher, spec = "apps/bob108_tool_activity_probe.py", "hexis-ui/e2e/tool-activity.spec.ts"
    startup = "hexis-ui/lib/tool-activity-startup.test.ts"
    old = dict(manifest.get("source_fingerprints", {}))
    if first.get("current_launcher_sha256") != old.get(launcher) or spec not in old:
        raise ProbeError("E2E repair prior source baseline is invalid")
    changes = {}
    for relative, expected in old.items():
        source = ROOT / relative
        if not source.is_file():
            raise ProbeError("E2E repair source file is missing")
        current = _digest(source)
        if relative in {launcher, spec}:
            changes[relative] = {"previous": expected, "current": current}
        elif current != expected:
            raise ProbeError(f"E2E repair rejects product source drift: {relative}")
        if relative.startswith("hexis-ui/"):
            copied = path / "ui-src" / relative.removeprefix("hexis-ui/")
            if not copied.is_file() or _digest(copied) != expected:
                raise ProbeError("E2E repair rejects isolated UI copy drift")
    if changes[spec]["current"] != expected_e2e_sha256 or changes[spec]["current"] == old[spec]:
        raise ProbeError("E2E repair requires the explicitly selected tested spec hash")
    startup_source = ROOT / startup
    if startup in old or not startup_source.is_file() or (path / "ui-src/lib/tool-activity-startup.test.ts").exists():
        raise ProbeError("E2E repair requires only the explicitly added startup regression file")
    changes[startup] = {"previous": None, "current": _digest(startup_source)}
    trace = _absolute(str(interrupted_trace))
    trace_archive = evidence / "e2e-interrupted-original-trace.zip"
    if not trace.is_file() or trace.suffix != ".zip" or trace_archive.exists():
        raise ProbeError("E2E repair requires the retained interrupted Playwright trace")
    trace_hash = _digest(trace)
    for role in ("api", "ui"):
        if not _owned_process_alive(manifest.get(f"{role}_process"), run_dir=path, role=role):
            raise ProbeError(f"E2E repair requires original owned {role} process")
        url = manifest["runtime"][f"{role}_url"] + ("/__test__/tool-activity/identity" if role == "api" else "/api/__test__/tool-activity/identity")
        observed = _http_json(url)
        expected = manifest["service_identities"]["target"]
        if observed.get("run_id") != manifest["run_id"] or any(observed.get("database_identity", {}).get(key) != expected.get(key) for key in ("database", "system_identifier")) or (role == "api" and observed.get("source_fingerprints") != old):
            raise ProbeError(f"E2E repair observed a different {role} runtime identity")
    retained = {}
    for name in ("consent-summary.json", "api-start.json", "ui-start.json", "launcher-repair-audit.json", "launcher-repair-original-manifest.json", "launcher-repair-result.json", "api-start-after-launcher-repair.json"):
        artifact = evidence / name
        if not artifact.is_file():
            raise ProbeError("E2E repair historical evidence is missing")
        retained[name] = {"path": str(artifact), "sha256": _digest(artifact)}
    empty_state = asyncio.run(_validate_empty_e2e_repair_target(manifest, values))
    with trace_archive.open("xb") as stream:
        os.chmod(trace_archive, 0o600)
        stream.write(trace.read_bytes())
    if _digest(trace_archive) != trace_hash:
        raise ProbeError("interrupted trace changed during evidence preservation")
    trace_ref = {"path": str(trace_archive), "sha256": trace_hash}
    retained[trace_archive.name] = trace_ref
    with archive_path.open("xb") as stream:
        os.chmod(archive_path, 0o600)
        stream.write((path / "run-manifest.json").read_bytes())
    audit = {
        "run_id": manifest["run_id"], "reason": reason.strip(), "changes": changes,
        "original_manifest": {"path": str(archive_path), "sha256": _digest(archive_path)},
        "previous_source_fingerprints": old, "retained_original_evidence": retained,
        "interrupted_trace": trace_ref, "interrupted_trace_original": {"path": str(trace), "sha256": trace_hash},
        "previous_api_process": manifest["api_process"], "unchanged_ui_process": manifest["ui_process"],
        "target_empty_state": empty_state, "provider_requested": False, "consent_written": False, "postgres_stopped": False,
        "说明": "登记已测试的 E2E spec 修复及本审计 CLI 自身变更；历史证据保留，仅重启本轮 API，UI/PG/provider 保持。",
    }
    _exclusive_json(audit_path, audit)
    _stop_run_process(manifest["api_process"], run_dir=path, role="api")
    for relative, change in changes.items():
        manifest["source_fingerprints"][relative] = change["current"]
    manifest["api_process"] = None
    manifest["state"] = "consent_accepted"
    manifest["e2e_repair"] = {"path": str(audit_path), "sha256": _digest(audit_path)}
    _atomic_json(path / "run-manifest.json", manifest)
    result = {**serve_api(path), "provider_requested": False, "ui_restarted": False, "postgres_stopped": False}
    _exclusive_json(evidence / "e2e-repair-result.json", result)
    return result


def _validate_harness_retry_events(events: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
    tools = [row for row in events if row["event_type"] in {"TOOL_START", "TOOL_RESULT"}]
    if len(tools) != 2 or [row["event_type"] for row in tools] != ["TOOL_START", "TOOL_RESULT"]:
        raise ProbeError("harness retry requires exactly the original recall start/result pair")
    call_ids = {row["payload"].get("call_id") for row in tools}
    if len(call_ids) != 1 or not next(iter(call_ids)) or any(row["session_id"] != session_id or row["payload"].get("tool_name") != "voice_recall" for row in tools) or tools[1]["payload"].get("success") is not True:
        raise ProbeError("harness retry recall identity or result differs")
    if any(row["session_id"] != session_id for row in events):
        raise ProbeError("harness retry found events outside the original failed session")
    after_recall = events[events.index(tools[1]) + 1:]
    if not any(row["event_type"] == "voice_speech_start" for row in after_recall) or not any(row["event_type"] == "speech_without_usable_transcript" for row in after_recall) or any(row["event_type"] in {"voice_input_committed", "voice_transcript_final"} for row in after_recall):
        raise ProbeError("harness retry remember attempt is not limited to failed speech input")
    ready = [row for row in events if row["event_type"] == "voice_session_ready"]
    if len(ready) != 1 or not ready[0]["payload"].get("qwen_session_id"):
        raise ProbeError("harness retry must account for exactly one prior realtime provider session")
    return {"tool_events": tools, "provider_realtime_attempts": [{"event_id": ready[0]["event_id"], "qwen_session_id": ready[0]["payload"]["qwen_session_id"]}],
            "remember_failure_code": "speech_not_recognized", "raw_behavior_events": events}


async def _validate_harness_retry_target(manifest: dict[str, Any], values: dict[str, str], session_id: str) -> dict[str, Any]:
    import asyncpg

    await _validate_launcher_repair_consent(manifest, values, allow_tool_events=True)
    target = manifest["runtime"]["target"]
    conn = await asyncpg.connect(_dsn(target["host"], target["port"], target["database"], target["user"], values["POSTGRES_PASSWORD"]), timeout=5)
    try:
        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM m5_actions UNION ALL SELECT 1 FROM m5_tasks UNION ALL SELECT 1 FROM m5_work_rounds)"):
            raise ProbeError("harness retry is forbidden after action, task or work evidence")
        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM chat_messages WHERE session_id <> $1::uuid)", UUID(session_id)):
            raise ProbeError("harness retry found messages outside the failed session")
        rows = await conn.fetch("SELECT event_id::text,session_id::text,event_type,payload,recorded_at FROM behavior_events ORDER BY recorded_at,producer_seq")
        events = [{**dict(row), "payload": _json_value(row["payload"]), "recorded_at": row["recorded_at"].isoformat()} for row in rows]
        return _validate_harness_retry_events(events, session_id)
    finally:
        await conn.close()


def _assert_harness_retry_owned_services(path: Path, manifest: dict[str, Any]) -> None:
    observed = control_runtime._owned_resources(path, manifest)
    expected = manifest.get("owned_resources", {})
    if set(observed) != {"pytest-db", "browser-db", "fake-embeddings"} or any(not row.get("container_id") or row["container_id"] != expected.get(name, {}).get("container_id") for name, row in observed.items()):
        raise ProbeError("harness retry database service ownership differs from the original run")


def repair_e2e_harness_retry(run_dir: str | Path, *, reason: str, expected_e2e_sha256: str, expected_startup_sha256: str, interrupted_trace: str | Path, session_id: str, _case_refresh: bool = False) -> dict[str, Any]:
    """归档一次部分真实验收，仅修复测试器顺序并准备唯一一次重跑。"""
    path, manifest, _selected, _selection, values = load_run(run_dir)
    evidence = path / "evidence"
    repair_field = "case_refresh" if _case_refresh else "harness_retry"
    record_name = "case-refresh" if _case_refresh else "harness-retry"
    archive = evidence / ("e2e-attempt-2" if _case_refresh else "e2e-attempt-1")
    audit_path = evidence / f"{record_name}-audit.json"
    UUID(session_id)
    if not isinstance(reason, str) or not reason.strip() or any(secret and secret in reason for key, secret in values.items() if key in SECRET_NAMES):
        raise ProbeError("harness retry requires a credential-free reason")
    consent = manifest.get("consent", {})
    if manifest.get("state") != "ready" or consent.get("state") != "consent" or consent.get("operator_override") is not False:
        raise ProbeError("harness retry requires original real consent and a ready run")
    if archive.exists() or audit_path.exists() or manifest.get(repair_field):
        raise ProbeError("only one audited harness retry is allowed")
    first = _verified_repair_audit(manifest, "launcher_repair")
    second = _verified_repair_audit(manifest, "e2e_repair")
    old = dict(manifest["source_fingerprints"])
    previous_audit = _verified_repair_audit(manifest, "harness_retry") if _case_refresh else second
    if previous_audit["changes"]["apps/bob108_tool_activity_probe.py"]["current"] != old.get("apps/bob108_tool_activity_probe.py"):
        raise ProbeError("harness retry prior launcher baseline differs")
    ui_baseline = json.loads(Path(first["original_manifest"]["path"]).read_text())["source_fingerprints"]
    startup = "hexis-ui/lib/tool-activity-startup.test.ts"
    allowed = {"apps/bob108_tool_activity_probe.py", "hexis-ui/e2e/tool-activity.spec.ts", startup}
    changes = {}
    for relative, expected in old.items():
        source = ROOT / relative
        if not source.is_file():
            raise ProbeError("harness retry source is missing")
        current = _digest(source)
        if relative in allowed:
            changes[relative] = {"previous": expected, "current": current}
        elif current != expected:
            raise ProbeError(f"harness retry rejects product or regression source drift: {relative}")
        if relative.startswith("hexis-ui/"):
            copied = path / "ui-src" / relative.removeprefix("hexis-ui/")
            baseline = ui_baseline.get(relative)
            if (baseline is None and copied.exists()) or (baseline is not None and (not copied.is_file() or _digest(copied) != baseline)):
                raise ProbeError("harness retry rejects isolated UI copy drift")
    spec = "hexis-ui/e2e/tool-activity.spec.ts"
    if changes[spec]["current"] != expected_e2e_sha256 or expected_e2e_sha256 == old[spec]:
        raise ProbeError("harness retry requires the explicitly selected tested new spec hash")
    if changes[startup]["current"] != expected_startup_sha256:
        raise ProbeError("harness retry startup regression hash differs from the tested selection")
    cases = evidence / "cases"
    expected_files = {"voice-recall.json", "remember.json", "browser-run.json", "voice-recall.png", "remember-failed.png"}
    if not cases.is_dir() or {file.name for file in cases.iterdir()} != expected_files:
        raise ProbeError("harness retry partial case files differ from the authorised attempt")
    case_data = {}
    for name, status in (("voice-recall", "PASS"), ("remember", "FAIL")):
        case = json.loads((cases / f"{name}.json").read_text())
        if case.get("case") != name or case.get("status") != status or case.get("run_id") != manifest["run_id"] or case.get("origin_session_id") != session_id or case.get("evidence", {}).get("source_fingerprints") != old:
            raise ProbeError("harness retry partial case identity, status or source differs")
        screenshot = {"path": case["evidence"]["screenshot"], "sha256": case["evidence"]["screenshot_sha256"]}
        if Path(screenshot["path"]).parent.resolve() != cases.resolve():
            raise ProbeError("harness retry screenshot is outside the original case directory")
        _verify_file_ref(screenshot)
        case_data[name] = case
    remember = case_data["remember"]
    frames = remember["evidence"]["browser_realtime_protocol"]["received"]
    if not isinstance(remember.get("reason"), str) or "Timed out waiting for actual Activity events" not in remember["reason"] or (not _case_refresh and not any(frame.get("type") == "error" and frame.get("code") == "speech_not_recognized" and frame.get("session_id") == session_id for frame in frames)):
        raise ProbeError("harness retry requires the confirmed speech_not_recognized failure evidence")
    browser = json.loads((cases / "browser-run.json").read_text())
    if browser.get("run_id") != manifest["run_id"] or browser.get("origin_session_id") != session_id or (not _case_refresh and not any(frame.get("type") == "error" and frame.get("code") == "speech_not_recognized" and frame.get("session_id") == session_id for frame in browser.get("received", []))):
        raise ProbeError("harness retry browser protocol does not confirm the original speech failure")
    trace = _absolute(str(interrupted_trace))
    if not trace.is_file() or trace.suffix != ".zip":
        raise ProbeError("harness retry interrupted trace is missing")
    _assert_harness_retry_owned_services(path, manifest)
    for role in ("api", "ui"):
        if not _owned_process_alive(manifest.get(f"{role}_process"), run_dir=path, role=role):
            raise ProbeError(f"harness retry requires original owned {role} process")
        url = manifest["runtime"][f"{role}_url"] + ("/__test__/tool-activity/identity" if role == "api" else "/api/__test__/tool-activity/identity")
        observed = _http_json(url)
        expected = manifest["service_identities"]["target"]
        if observed.get("run_id") != manifest["run_id"] or any(observed.get("database_identity", {}).get(key) != expected.get(key) for key in ("database", "system_identifier")) or (role == "api" and observed.get("source_fingerprints") != old):
            raise ProbeError(f"harness retry observed a different {role} runtime identity")
    prior = asyncio.run(_validate_case_refresh_target(manifest, values, session_id) if _case_refresh else _validate_harness_retry_target(manifest, values, session_id))
    if set(case_data["voice-recall"]["evidence"]["event_ids"]) != {row["event_id"] for row in prior["tool_events"]}:
        raise ProbeError("harness retry original case tool IDs differ from live target DB")
    files = {"run-manifest.json": path / "run-manifest.json", "trace.zip": trace, **{f"cases/{file.name}": file for file in cases.iterdir()}}
    frozen = {name: (source.read_bytes(), _digest(source)) for name, source in files.items()}
    archive.mkdir()
    retained = {}
    for name, (raw, sha256) in frozen.items():
        destination = archive / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            os.chmod(destination, 0o600)
            stream.write(raw)
        if _digest(destination) != sha256:
            raise ProbeError("harness retry evidence changed during archival")
        retained[name] = {"path": str(destination), "sha256": sha256}
    audit = {
        "run_id": manifest["run_id"], "reason": reason.strip(), "changes": changes,
        "original_manifest": retained["run-manifest.json"], "previous_source_fingerprints": old,
        "retained_original_evidence": retained, "prior_attempt": prior,
        "original_trace_path": str(trace), "original_trace_sha256": retained["trace.zip"]["sha256"],
        "previous_api_process": manifest["api_process"], "unchanged_ui_process": manifest["ui_process"],
        "provider_requested": False, "consent_requested": False, "postgres_stopped": False,
        "说明": "原部分真实验收完整归档；此前 realtime provider 会话及工具调用已记录，费用未知。" + ("仅一次逐案覆盖补充验收；依赖未满足的 add_evidence 必须 NOT_RUN，不能算通过。" if _case_refresh else "仅允许修复测试器后一次重跑，不重试 consent。"),
    }
    _exclusive_json(audit_path, audit)
    _stop_run_process(manifest["api_process"], run_dir=path, role="api")
    for relative, change in changes.items():
        manifest["source_fingerprints"][relative] = change["current"]
    manifest["api_process"] = None
    manifest["state"] = "consent_accepted"
    manifest[repair_field] = {"path": str(audit_path), "sha256": _digest(audit_path)}
    _atomic_json(path / "run-manifest.json", manifest)
    result = {**serve_api(path), "provider_requested": False, "ui_restarted": False, "postgres_stopped": False}
    _exclusive_json(evidence / f"{record_name}-result.json", result)
    return result


def _validate_case_refresh_events(events: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
    tools = [row for row in events if row["event_type"] in {"TOOL_START", "TOOL_RESULT"}]
    if len(tools) != 2 or [row["event_type"] for row in tools] != ["TOOL_START", "TOOL_RESULT"] or any(row["session_id"] != session_id or row["payload"].get("tool_name") != "voice_recall" for row in tools) or tools[0]["payload"].get("call_id") != tools[1]["payload"].get("call_id") or not tools[0]["payload"].get("call_id") or tools[1]["payload"].get("success") is not True:
        raise ProbeError("case refresh requires only the recorded successful recall pair")
    committed = [row for row in events if row["event_type"] == "voice_input_committed"]
    transcripts = [row for row in events if row["event_type"] == "voice_transcript_final"]
    if len(committed) != 2 or len(transcripts) != 2 or any(row["session_id"] != session_id for row in events):
        raise ProbeError("case refresh requires exactly the recall and remember inputs in the recorded session")
    input_ids = [row["payload"]["identity"]["input_turn_id"] for row in committed]
    if len(set(input_ids)) != 2 or set(input_ids) != {row["payload"].get("input_turn_id") for row in transcripts}:
        raise ProbeError("case refresh transcript and committed input identities differ")
    for committed_row in committed:
        identity = committed_row["payload"]["identity"]
        input_id = identity["input_turn_id"]
        started = [row for row in events if row["event_type"] == "voice_model_call_started" and row["payload"].get("identity", {}).get("input_turn_id") == input_id]
        ended = [row for row in events if row["event_type"] == "voice_model_call_ended" and row["payload"].get("identity", {}).get("input_turn_id") == input_id]
        response_ids = {row["payload"].get("response_id") for row in started}
        source_identity = f"voice:{session_id}:{input_id}:{identity.get('stimulus_revision', 0)}"
        complete = [row for row in events if row["event_type"] == "voice_turn_completed" and row["payload"].get("source_identity") == source_identity]
        if not response_ids or None in response_ids or response_ids != {row["payload"].get("response_id") for row in ended} or any(row["payload"].get("reason") != "completed" for row in ended) or len(complete) != 1 or complete[0]["payload"].get("playback_scope") != "complete" or set(complete[0]["payload"].get("response_ids", [])) != response_ids:
            raise ProbeError("case refresh requires complete matched model responses and playback for both inputs")
    ready = [row for row in events if row["event_type"] == "voice_session_ready"]
    if len(ready) != 1 or not ready[0]["payload"].get("qwen_session_id"):
        raise ProbeError("case refresh prior realtime provider session accounting differs")
    return {"tool_events": tools, "completed_input_ids": input_ids, "provider_realtime_attempts": [{"qwen_session_id": ready[0]["payload"]["qwen_session_id"], "event_id": ready[0]["event_id"]}], "raw_behavior_events": events}


async def _validate_case_refresh_target(manifest: dict[str, Any], values: dict[str, str], session_id: str) -> dict[str, Any]:
    import asyncpg

    prior_audit = _verified_repair_audit(manifest, "harness_retry")
    original_events = prior_audit["prior_attempt"]["raw_behavior_events"]
    original_sessions = {row["session_id"] for row in original_events}
    if session_id in original_sessions:
        raise ProbeError("case refresh must account for the separate latest real attempt")
    await _validate_launcher_repair_consent(manifest, values, allow_tool_events=True)
    target = manifest["runtime"]["target"]
    conn = await asyncpg.connect(_dsn(target["host"], target["port"], target["database"], target["user"], values["POSTGRES_PASSWORD"]), timeout=5)
    try:
        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM m5_actions UNION ALL SELECT 1 FROM m5_tasks UNION ALL SELECT 1 FROM m5_work_rounds)"):
            raise ProbeError("case refresh rejects any action, task or work evidence")
        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM chat_messages WHERE NOT (session_id::text=ANY($1::text[])))", sorted(original_sessions | {session_id})):
            raise ProbeError("case refresh found messages outside the two recorded real attempts")
        rows = await conn.fetch("SELECT event_id::text,session_id::text,event_type,payload,recorded_at FROM behavior_events ORDER BY recorded_at,producer_seq")
        events = [{**dict(row), "payload": _json_value(row["payload"]), "recorded_at": row["recorded_at"].isoformat()} for row in rows]
        preserved = [row for row in events if row["session_id"] in original_sessions]
        if preserved != original_events or any(row["session_id"] not in original_sessions | {session_id} for row in events):
            raise ProbeError("case refresh original event ledger changed or contains an unexplained session")
        result = _validate_case_refresh_events([row for row in events if row["session_id"] == session_id], session_id)
        result["prior_sessions_preserved"] = True
        return result
    finally:
        await conn.close()


def repair_e2e_case_refresh(run_dir: str | Path, **kwargs: Any) -> dict[str, Any]:
    return repair_e2e_harness_retry(run_dir, **kwargs, _case_refresh=True)


def _validate_evidence_replay_snapshot(snapshot: dict[str, Any], origin: str, task_id: str, task_session: str, memory_id: str) -> None:
    if snapshot.get("origin_session_id") != origin or task_session == origin:
        raise ProbeError("evidence replay origin/task session ownership differs")
    actions = snapshot.get("actions", [])
    remember = [row for row in actions if row.get("tool_name") == "remember"]
    submit = [row for row in actions if row.get("tool_name") == "submit"]
    if len(actions) != 2 or len(remember) != 1 or len(submit) != 1 or _done_receipt_memory_uuid(remember[0]) != UUID(memory_id):
        raise ProbeError("evidence replay requires only the actual done memory receipt and accepted submit")
    if not any(row.get("id") == memory_id and row.get("type") == "semantic" and row.get("status") == "active" for row in snapshot.get("memories", [])):
        raise ProbeError("evidence replay memory UUID does not match a real active semantic DB row")
    result = _json_value(submit[0].get("result"))
    if submit[0].get("state") != "accepted" or not isinstance(result, dict) or result.get("accepted") is not True or result.get("task_id") != task_id or result.get("task_session_id") != task_session:
        raise ProbeError("evidence replay submit does not authoritatively identify this task session")
    tasks, work = snapshot.get("tasks", []), snapshot.get("work", [])
    if len(tasks) != 1 or tasks[0].get("id") != task_id or tasks[0].get("task_session_id") != task_session or tasks[0].get("origin_session_id") != origin or tasks[0].get("product_state") != "idle" or len(work) != 1 or work[0].get("task_id") != task_id or work[0].get("id") != result.get("work_execution_id") or work[0].get("status") != "completed":
        raise ProbeError("evidence replay task/work ledger is not the recorded completed background task")
    tools = [row for row in snapshot["events"] if row["event_type"] in {"TOOL_START", "TOOL_RESULT"}]
    foreground = [row for row in tools if row["session_id"] == origin]
    background = [row for row in tools if row["session_id"] == task_session]
    if len(foreground) != 5 or len(background) != 6 or len(tools) != 11:
        raise ProbeError("evidence replay contains an unexplained tool event count or session")
    recall = [row for row in foreground if row["payload"].get("tool_name") == "voice_recall"]
    memory = [row for row in foreground if row["payload"].get("tool_name") == "remember"]
    if len(recall) != 2 or {row["event_type"] for row in recall} != {"TOOL_START", "TOOL_RESULT"} or len({row["payload"].get("call_id") for row in recall}) != 1 or len(memory) != 3 or any(row["payload"].get("call_id") != remember[0]["actual_call_id"] for row in memory) or {row["payload"].get("state") for row in memory if row["event_type"] == "TOOL_RESULT"} != {"accepted", "done"}:
        raise ProbeError("evidence replay foreground recall/remember call ledger differs")
    pairs = {}
    for row in background:
        payload = row["payload"]
        if row.get("task_id") != task_id or payload.get("execution_id") != work[0]["id"] or payload.get("tool_name") not in {"recall", "read_file"} or not payload.get("call_id"):
            raise ProbeError("evidence replay background tool identity differs")
        pairs.setdefault((payload["tool_name"], payload["call_id"]), []).append(row)
    if len(pairs) != 3 or sum(tool == "recall" for tool, _call in pairs) != 1 or any(len(rows) != 2 or {row["event_type"] for row in rows} != {"TOOL_START", "TOOL_RESULT"} for rows in pairs.values()):
        raise ProbeError("evidence replay background call pairs are incomplete")
    read_results = [row["payload"].get("success") for row in background if row["event_type"] == "TOOL_RESULT" and row["payload"]["tool_name"] == "read_file"]
    recall_results = [row["payload"].get("success") for row in background if row["event_type"] == "TOOL_RESULT" and row["payload"]["tool_name"] == "recall"]
    if any(not isinstance(value, bool) for value in read_results) or sorted(read_results) != [False, True] or recall_results != [True]:
        raise ProbeError("evidence replay must preserve separate successful and failed file reads")


async def _validate_evidence_replay_accounting(manifest: dict[str, Any], values: dict[str, str], origin: str, task_id: str, task_session: str, memory_id: str) -> dict[str, Any]:
    import asyncpg

    await _validate_launcher_repair_consent(manifest, values, allow_tool_events=True)
    snapshot = await _snapshot_async(manifest["run_directory"], origin)
    _validate_evidence_replay_snapshot(snapshot, origin, task_id, task_session, memory_id)
    previous = [*_verified_repair_audit(manifest, "harness_retry")["prior_attempt"]["raw_behavior_events"], *_verified_repair_audit(manifest, "case_refresh")["prior_attempt"]["raw_behavior_events"]]
    previous_sessions = {row["session_id"] for row in previous}
    target = manifest["runtime"]["target"]
    conn = await asyncpg.connect(_dsn(target["host"], target["port"], target["database"], target["user"], values["POSTGRES_PASSWORD"]), timeout=5)
    try:
        rows = await conn.fetch("SELECT event_id::text,session_id::text,event_type,payload,recorded_at FROM behavior_events ORDER BY recorded_at,producer_seq")
        events = [{**dict(row), "payload": _json_value(row["payload"]), "recorded_at": row["recorded_at"].isoformat()} for row in rows]
        observed_previous = [row for row in events if row["session_id"] in previous_sessions]
        if observed_previous != previous or any(row["session_id"] not in previous_sessions | {origin, task_session} for row in events):
            raise ProbeError("evidence replay historical ledger changed or contains an unexplained session")
        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM m5_actions WHERE origin_session_id IS DISTINCT FROM $1::uuid UNION ALL SELECT 1 FROM m5_tasks WHERE origin_session_id IS DISTINCT FROM $1::uuid UNION ALL SELECT 1 FROM m5_work_rounds WHERE task_id IS DISTINCT FROM $2::uuid)", UUID(origin), UUID(task_id)):
            raise ProbeError("evidence replay found an unexplained action/task/work ledger")
        return {"snapshot": snapshot, "historical_event_ledger_preserved": True, "actual_memory_id": memory_id, "background_task_session": task_session, "raw_behavior_events": events, "real_time_working_verified": False, "scope": "只读权威状态及历史回放；完成后的 idle/completed 不能证明事件当时 working/running。"}
    finally:
        await conn.close()


def repair_evidence_replay(run_dir: str | Path, *, reason: str, expected_e2e_sha256: str, expected_startup_sha256: str, interrupted_trace: str | Path, origin_session_id: str, task_id: str, task_session_id: str, memory_id: str) -> dict[str, Any]:
    path, manifest, _selected, _selection, values = load_run(run_dir)
    for value in (origin_session_id, task_id, task_session_id, memory_id):
        UUID(value)
    evidence, archive = path / "evidence", path / "evidence/evidence-replay-originals"
    audit_path = evidence / "evidence-replay-audit.json"
    if not reason.strip() or any(secret and secret in reason for key, secret in values.items() if key in SECRET_NAMES):
        raise ProbeError("evidence replay requires a credential-free reason")
    if manifest.get("state") != "ready" or manifest.get("consent", {}).get("state") != "consent" or manifest["consent"].get("operator_override") is not False:
        raise ProbeError("evidence replay requires original real consent and the owned ready run")
    if archive.exists() or audit_path.exists() or manifest.get("evidence_replay"):
        raise ProbeError("only one read-only evidence replay repair is allowed")
    first = _verified_repair_audit(manifest, "launcher_repair")
    _verified_repair_audit(manifest, "e2e_repair")
    _verified_repair_audit(manifest, "harness_retry")
    prior = _verified_repair_audit(manifest, "case_refresh")
    old = dict(manifest["source_fingerprints"])
    if prior["changes"]["apps/bob108_tool_activity_probe.py"]["current"] != old["apps/bob108_tool_activity_probe.py"]:
        raise ProbeError("evidence replay prior launcher source baseline differs")
    baseline = json.loads(Path(first["original_manifest"]["path"]).read_text())["source_fingerprints"]
    expected = {"hexis-ui/e2e/tool-activity.spec.ts": expected_e2e_sha256, "hexis-ui/lib/tool-activity-startup.test.ts": expected_startup_sha256}
    changes = {}
    for relative, sha256 in old.items():
        source = ROOT / relative
        if not source.is_file():
            raise ProbeError("evidence replay source is missing")
        current = _digest(source)
        if relative == "apps/bob108_tool_activity_probe.py" or relative in expected:
            if relative in expected and expected[relative] != current:
                raise ProbeError("evidence replay tested source hash differs from its exact selection")
            changes[relative] = {"previous": sha256, "current": current}
        elif current != sha256:
            raise ProbeError(f"evidence replay rejects product source drift: {relative}")
        if relative.startswith("hexis-ui/"):
            copied = path / "ui-src" / relative.removeprefix("hexis-ui/")
            if (relative not in baseline and copied.exists()) or (relative in baseline and (not copied.is_file() or _digest(copied) != baseline[relative])):
                raise ProbeError("evidence replay rejects isolated UI copy drift")
    for name, status in (("voice-recall", "PASS"), ("remember", "FAIL"), ("add-evidence", "NOT_RUN"), ("background-tools", "FAIL")):
        raw = json.loads((evidence / f"cases/{name}.json").read_text())
        if raw.get("case") != name or raw.get("status") != status or raw.get("run_id") != manifest["run_id"] or raw.get("origin_session_id") != origin_session_id or raw.get("evidence", {}).get("source_fingerprints") != old:
            raise ProbeError("evidence replay original root case identity/status/source differs")
    _assert_harness_retry_owned_services(path, manifest)
    for role in ("api", "ui"):
        if not _owned_process_alive(manifest.get(f"{role}_process"), run_dir=path, role=role):
            raise ProbeError(f"evidence replay requires the exact owned {role} process")
        url = manifest["runtime"][f"{role}_url"] + ("/__test__/tool-activity/identity" if role == "api" else "/api/__test__/tool-activity/identity")
        observed = _http_json(url)
        identity = manifest["service_identities"]["target"]
        if observed.get("run_id") != manifest["run_id"] or any(observed.get("database_identity", {}).get(key) != identity.get(key) for key in ("database", "system_identifier")) or (role == "api" and observed.get("source_fingerprints") != old):
            raise ProbeError("evidence replay observed a different running service identity")
    trace = _absolute(str(interrupted_trace))
    if not trace.is_file() or trace.suffix != ".zip":
        raise ProbeError("evidence replay current original trace is missing")
    accounting = asyncio.run(_validate_evidence_replay_accounting(manifest, values, origin_session_id, task_id, task_session_id, memory_id))
    files = {"run-manifest.json": path / "run-manifest.json", "trace.zip": trace, **{f"evidence/{file.relative_to(evidence)}": file for file in evidence.rglob("*") if file.is_file()}}
    frozen = {name: source.read_bytes() for name, source in files.items()}
    archive.mkdir()
    retained = {}
    for name, raw in frozen.items():
        destination = archive / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            os.chmod(destination, 0o600)
            stream.write(raw)
        retained[name] = {"path": str(destination), "sha256": hashlib.sha256(raw).hexdigest()}
    audit = {"run_id": manifest["run_id"], "reason": reason.strip(), "changes": changes,
             "original_manifest": retained["run-manifest.json"], "retained_original_evidence": retained, "previous_source_fingerprints": old,
             "accounting": accounting, "previous_api_process": manifest["api_process"], "unchanged_ui_process": manifest["ui_process"],
             "provider_requested": False, "consent_requested": False, "postgres_stopped": False, "real_time_working_verified": False,
             "说明": "本次仅修复证据解析与只读历史回放。原case、trace、日志、manifest和历次归档原字节全部冻结；API新启动仅同步诊断源码，禁止新WAV/provider验收。"}
    _exclusive_json(audit_path, audit)
    _stop_run_process(manifest["api_process"], run_dir=path, role="api")
    for relative, change in changes.items():
        manifest["source_fingerprints"][relative] = change["current"]
    manifest["api_process"] = None
    manifest["state"] = "consent_accepted"
    manifest["evidence_replay"] = {"path": str(audit_path), "sha256": _digest(audit_path)}
    _atomic_json(path / "run-manifest.json", manifest)
    result = {**serve_api(path), "provider_requested": False, "ui_restarted": False, "postgres_stopped": False, "real_time_working_verified": False}
    _exclusive_json(evidence / "evidence-replay-result.json", result)
    return result


async def _validate_final_verification_target(manifest: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    import asyncpg

    await _validate_launcher_repair_consent(manifest, values, allow_tool_events=True)
    target = manifest["runtime"]["target"]
    conn = await asyncpg.connect(_dsn(target["host"], target["port"], target["database"], target["user"], values["POSTGRES_PASSWORD"]), timeout=5)
    try:
        profile = _json_value(await conn.fetchval("SELECT value FROM config WHERE key='llm.chat'"))
        if not isinstance(profile, dict) or any(profile.get(key) != manifest["runtime"]["provider_profile"].get(key) for key in PROFILE_REQUIRED):
            raise ProbeError("final verification target provider configuration differs from its selected real consent")
        pending = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM m5_work_rounds WHERE status IN ('claimed','running') UNION ALL SELECT 1 FROM m5_tasks WHERE product_state='working' UNION ALL SELECT 1 FROM m5_actions WHERE tool_name IN ('remember','add_evidence') AND state='accepted')")
        if pending:
            raise ProbeError("final verification refresh cannot restart API with pending tool or provider work")
        return {"real_consent_active": True, "selected_provider_unchanged": True, "pending_work": False}
    finally:
        await conn.close()


def refresh_final_verification(run_dir: str | Path, *, expected_source_hashes: dict[str, str], reason: str, trace_path: str | Path | None = None) -> dict[str, Any]:
    path, manifest, _selected, _selection, values = load_run(run_dir)
    evidence, archive = path / "evidence", path / "evidence/final-verification-originals"
    audit_path = evidence / "final-verification-audit.json"
    if set(expected_source_hashes) != set(FINAL_VERIFICATION_SOURCE_PATHS) or any(not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha) for sha in expected_source_hashes.values()):
        raise ProbeError("final verification refresh permits only four exact source selections")
    if not reason.strip() or any(secret and secret in reason for key, secret in values.items() if key in SECRET_NAMES):
        raise ProbeError("final verification refresh requires a credential-free reason")
    if manifest.get("state") not in {"ready", "verification_incomplete"} or manifest.get("consent", {}).get("state") != "consent" or manifest["consent"].get("operator_override") is not False:
        raise ProbeError("final verification refresh requires original real consent and the existing owned run")
    if archive.exists() or audit_path.exists() or manifest.get("final_verification_refresh"):
        raise ProbeError("only one final verification source refresh is allowed")
    first = _verified_repair_audit(manifest, "launcher_repair")
    for field in ("e2e_repair", "harness_retry", "case_refresh", "evidence_replay"):
        _verified_repair_audit(manifest, field)
    old = dict(manifest["source_fingerprints"])
    baseline = json.loads(Path(first["original_manifest"]["path"]).read_text())["source_fingerprints"]
    for relative, sha in old.items():
        if relative not in expected_source_hashes and (not (ROOT / relative).is_file() or _digest(ROOT / relative) != sha):
            raise ProbeError(f"final verification refresh rejects product source drift: {relative}")
        if relative.startswith("hexis-ui/"):
            copied = path / "ui-src" / relative.removeprefix("hexis-ui/")
            if (relative not in baseline and copied.exists()) or (relative in baseline and (not copied.is_file() or _digest(copied) != baseline[relative])):
                raise ProbeError("final verification refresh rejects isolated UI copy drift")
    changes = {}
    for relative, sha in expected_source_hashes.items():
        if not (ROOT / relative).is_file() or _digest(ROOT / relative) != sha:
            raise ProbeError("final verification refresh source differs from its exact selected hash")
        changes[relative] = {"previous": old.get(relative), "current": sha, "changed": old.get(relative) != sha}
    matrix = evidence / "verification-matrix.json"
    if not matrix.is_file() or json.loads(matrix.read_text()).get("run_id") != manifest["run_id"]:
        raise ProbeError("final verification refresh current matrix identity is missing or differs")
    case_records = {}
    for name in ("voice-recall", "remember", "add-evidence", "background-tools"):
        artifact = evidence / f"cases/{name}.json"
        raw = json.loads(artifact.read_text())
        sources = raw.get("evidence", {}).get("source_fingerprints")
        if raw.get("case") != name or raw.get("run_id") != manifest["run_id"] or raw.get("status") not in {"PASS", "FAIL", "NOT_RUN"} or not isinstance(sources, dict) or not sources:
            raise ProbeError("final verification refresh original case identity or producer sources differ")
        product_keys = set(old) - set(FINAL_VERIFICATION_SOURCE_PATHS)
        if set(sources) - set(FINAL_VERIFICATION_SOURCE_PATHS) != product_keys or any(sources[key] != old[key] for key in product_keys):
            raise ProbeError("final verification refresh original case product source lineage differs")
        case_records[name] = {"original_path": str(artifact), "sha256": _digest(artifact), "producer_source_fingerprints": sources}
    _assert_harness_retry_owned_services(path, manifest)
    for role in ("api", "ui"):
        if not _owned_process_alive(manifest.get(f"{role}_process"), run_dir=path, role=role):
            raise ProbeError("final verification refresh requires exact owned running API/UI processes")
        url = manifest["runtime"][f"{role}_url"] + ("/__test__/tool-activity/identity" if role == "api" else "/api/__test__/tool-activity/identity")
        observed = _http_json(url)
        expected = manifest["service_identities"]["target"]
        if observed.get("run_id") != manifest["run_id"] or any(observed.get("database_identity", {}).get(key) != expected.get(key) for key in ("database", "system_identifier")) or (role == "api" and observed.get("source_fingerprints") != old):
            raise ProbeError("final verification refresh running service/database identity differs")
    target_gate = asyncio.run(_validate_final_verification_target(manifest, values))
    files = {"run-manifest.json": path / "run-manifest.json", **{f"evidence/{file.relative_to(evidence)}": file for file in evidence.rglob("*") if file.is_file()}}
    traces = [Path(trace_path)] if trace_path is not None else sorted((ROOT / "hexis-ui/test-results").rglob("trace.zip"))
    if len(traces) != 1 or not traces[0].is_file():
        raise ProbeError("final verification refresh requires one explicitly selected original trace")
    files["trace.zip"] = traces[0].resolve()
    browser_path = evidence / "cases/browser-run.json"
    browser = json.loads(browser_path.read_text())
    if browser.get("run_id") != manifest["run_id"]:
        raise ProbeError("final verification refresh original browser artifact belongs to a different run")
    frozen = {name: source.read_bytes() for name, source in files.items()}
    archive.mkdir()
    retained = {}
    for name, raw in frozen.items():
        destination = archive / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            os.chmod(destination, 0o600)
            stream.write(raw)
        retained[name] = {"path": str(destination), "sha256": hashlib.sha256(raw).hexdigest()}
    for name, record in case_records.items():
        record["archive_path"] = retained[f"evidence/cases/{name}.json"]["path"]
        if retained[f"evidence/cases/{name}.json"]["sha256"] != record["sha256"]:
            raise ProbeError("final verification case changed during archival")
    artifact_lineage = {name: {"original_path": str(files[name]), "sha256": ref["sha256"], "archive_path": ref["path"]} for name, ref in retained.items()}
    for name, record in case_records.items():
        artifact_lineage[f"evidence/cases/{name}.json"]["producer_source_fingerprints"] = record["producer_source_fingerprints"]
    companion_maps = [record["producer_source_fingerprints"] for name, record in case_records.items() if json.loads((evidence / f"cases/{name}.json").read_text()).get("origin_session_id") == browser.get("origin_session_id")]
    for name in ("trace.zip", "evidence/cases/browser-run.json"):
        artifact_lineage[name]["producer_source_maps_from_companion_cases"] = companion_maps
        artifact_lineage[name]["source_attribution"] = "来自同run/browser origin的已冻结case；原trace/browser未自行记录源码字段，保留此限制。"
    audit = {"run_id": manifest["run_id"], "reason": reason.strip(), "changes": changes, "previous_source_fingerprints": old,
             "original_manifest": retained["run-manifest.json"], "retained_original_evidence": retained, "case_artifacts": case_records, "artifact_source_lineage": artifact_lineage,
             "cli_implementation_prior_review": {"apps/bob108_tool_activity_probe.py": "4ec4858bdbb11cee700e9a74950f9b88f9694bbc2cc9db8120d2dec840828742", "tests/services/test_bob108_tool_activity_probe.py": "aeb1c5874301777b11989750daabb71d91ef82baa4071e817b0f28cc458593d5"},
             "target_gate": target_gate, "previous_api_process": manifest["api_process"], "unchanged_ui_process": manifest["ui_process"],
             "provider_requested": False, "real_e2e_rerun": False, "postgres_stopped": False, "real_time_working_verified": False,
             "说明": "只刷新最终checker/test source基线；历史attempt原件及其生成源码保留，评估源码单独登记，不冒充新源码provider执行。最终pytest/Vitest receipts必须匹配current map。"}
    _exclusive_json(audit_path, audit)
    _stop_run_process(manifest["api_process"], run_dir=path, role="api")
    for relative, change in changes.items():
        manifest["source_fingerprints"][relative] = change["current"]
    manifest["api_process"] = None
    manifest["state"] = "consent_accepted"
    manifest["final_verification_refresh"] = {"path": str(audit_path), "sha256": _digest(audit_path)}
    _atomic_json(path / "run-manifest.json", manifest)
    result = {**serve_api(path), "provider_requested": False, "real_e2e_rerun": False, "ui_restarted": False, "postgres_stopped": False}
    _exclusive_json(evidence / "final-verification-result.json", result)
    return result


def _resume_configuration_hashes(path: Path, manifest: dict[str, Any], selected: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    if selected.get("run_id") != manifest["run_id"] or selected.get("configuration_hashes") != selection.get("_config_hashes"):
        raise ProbeError("resume selected configuration hashes or run identity differ")
    profile = manifest["runtime"]["provider_profile"]
    if any(selected.get("selected_profile", {}).get(key) != profile.get(key) for key in PROFILE_REQUIRED):
        raise ProbeError("resume selected provider profile differs")
    compose = path / "compose.yml"
    expected_compose = compose_text(manifest["project"], manifest["run_id"], manifest["ports"], manifest["volumes"]).encode()
    if not compose.is_file() or _digest(compose) != hashlib.sha256(expected_compose).hexdigest():
        raise ProbeError("resume original Compose configuration is missing or changed")
    runtime = manifest["runtime"]
    expected_env = {"POSTGRES_HOST": runtime["target"]["host"], "POSTGRES_PORT": str(runtime["target"]["port"]), "POSTGRES_USER": runtime["target"]["user"], "POSTGRES_DB": runtime["target"]["database"], "POSTGRES_ADMIN_DB": runtime["target"]["admin_database"], "POSTGRES_PASSWORD_ENV": "POSTGRES_PASSWORD", "DASHSCOPE_API_KEY_ENV": profile["api_key_env"], "HEXIS_API_URL": runtime["api_url"], "HEXIS_UI_ORIGIN": runtime["ui_url"], "EMBEDDING_SERVICE_URL": runtime["embedding_url"]}
    env_file = path / "runtime.env"
    if not env_file.is_file() or dict(dotenv_values(env_file)) != expected_env:
        raise ProbeError("resume original runtime.env bindings are missing or changed")
    summary = path / "evidence/consent-summary.json"
    if not summary.is_file():
        raise ProbeError("resume original real consent summary is missing")
    consent = json.loads(summary.read_text())
    if consent.get("run_id") != manifest["run_id"] or consent.get("decision") != "consent" or consent.get("operator_override") is not False or consent.get("consent_log_id") != manifest["consent"].get("consent_log_id") or any(consent.get(key) != profile.get(key) for key in ("provider", "model", "endpoint")):
        raise ProbeError("resume original consent UUID/provider/profile differ")
    for relative, sha in manifest["source_fingerprints"].items():
        if relative.startswith("hexis-ui/") and relative not in RESUME_HARNESS_FILES:
            copied = path / "ui-src" / relative.removeprefix("hexis-ui/")
            if not copied.is_file() or _digest(copied) != sha:
                raise ProbeError("resume isolated UI runtime copy changed")
    return {"selected_configuration": selected["configuration_hashes"], "compose_sha256": _digest(compose), "runtime_env_sha256": _digest(env_file), "consent_summary_sha256": _digest(summary)}


def _sources_after_owned_resume(manifest: dict[str, Any], previous: dict[str, str]) -> dict[str, str]:
    if not manifest.get("resume_owned"):
        return previous
    audit = _verified_repair_audit(manifest, "resume_owned")
    explicit = set(audit["source_files"])
    if explicit - RESUME_HARNESS_FILES or audit["previous_source_fingerprints"] != previous or audit.get("acceptance_modes") != {"add-evidence": "text_chat"}:
        raise ProbeError("resume-owned prior source/acceptance identity differs")
    expected = dict(previous)
    for relative, change in audit["changes"].items():
        if relative not in explicit | RESUME_INTRINSIC_FILES or expected.get(relative) != change["previous"]:
            raise ProbeError("resume-owned source refresh exceeds its approved exact scope")
        expected[relative] = change["current"]
    return expected


def _inspect_resume_resources(path: Path, manifest: dict[str, Any], values: dict[str, str], *, service_names: list[str] | None = None, include_volumes: bool = True) -> dict[str, Any]:
    expected = manifest["owned_resources"]
    names = manifest["services"] if service_names is None else service_names
    ids = [expected[name]["container_id"] for name in names]
    rows = json.loads(subprocess.run(["docker", "inspect", *ids], cwd=ROOT, check=True, capture_output=True, text=True).stdout)
    if len(rows) != len(names):
        raise ProbeError("resume original container set is incomplete")
    services = {}
    for row in rows:
        labels = row.get("Config", {}).get("Labels") or {}
        name = labels.get("com.docker.compose.service")
        original = expected.get(name, {})
        if name in services or name not in names or row.get("Id") != original.get("container_id") or labels.get("com.docker.compose.project") != manifest["project"] or labels.get("hexis.bob108.run_id") != manifest["run_id"] or row.get("Image") != original.get("image_id") or row.get("Config", {}).get("Image") != original.get("image_ref"):
            raise ProbeError("resume container ID/image/project/run identity changed")
        config_hash = labels.get("com.docker.compose.config-hash")
        if not isinstance(config_hash, str) or not config_hash:
            raise ProbeError("resume container has no original Compose configuration identity")
        mounts = [{"name": mount.get("Name"), "destination": mount.get("Destination")} for mount in row.get("Mounts", []) if mount.get("Type") == "volume"]
        if mounts != original.get("mounts", []):
            raise ProbeError("resume container original volume mounts changed")
        if name in {"pytest-db", "browser-db"}:
            logical = "pytest" if name == "pytest-db" else "browser"
            physical = f"{manifest['project']}_{manifest['volumes'][logical]}"
            if {"name": physical, "destination": "/var/lib/postgresql/data"} not in mounts:
                raise ProbeError("resume PostgreSQL is not mounted on its original owned volume")
            env = dict(value.split("=", 1) for value in row.get("Config", {}).get("Env", []) if "=" in value)
            if env.get("POSTGRES_USER") != values["POSTGRES_USER"] or env.get("POSTGRES_PASSWORD") != values["POSTGRES_PASSWORD"] or env.get("POSTGRES_DB") != ("bob108_pytest" if logical == "pytest" else "postgres"):
                raise ProbeError("resume PostgreSQL container credentials/database binding changed")
        port_name = {"pytest-db": "pytest", "browser-db": "browser", "fake-embeddings": "embeddings"}[name]
        internal = "5432/tcp" if name != "fake-embeddings" else "45435/tcp"
        bindings = row.get("HostConfig", {}).get("PortBindings", {}).get(internal)
        if bindings != [{"HostIp": "127.0.0.1", "HostPort": str(manifest["ports"][port_name])}]:
            raise ProbeError("resume original loopback container port binding changed")
        services[name] = {**original, "state": row.get("State", {}).get("Status"), "config_hash": config_hash}
    volume_names = [f"{manifest['project']}_{manifest['volumes'][name]}" for name in ("pytest", "browser")]
    if not include_volumes:
        return {"services": services, "volumes": {}}
    volumes = json.loads(subprocess.run(["docker", "volume", "inspect", *volume_names], cwd=ROOT, check=True, capture_output=True, text=True).stdout)
    if {row.get("Name") for row in volumes} != set(volume_names):
        raise ProbeError("resume original PostgreSQL volume set changed")
    for row in volumes:
        labels = row.get("Labels") or {}
        if labels.get("hexis.bob108.run_id") != manifest["run_id"] or labels.get("com.docker.compose.project") != manifest["project"] or not row.get("CreatedAt"):
            raise ProbeError("resume original PostgreSQL volume ownership differs")
    return {"services": services, "volumes": {row["Name"]: {key: row.get(key) for key in ("Name", "Driver", "CreatedAt", "Labels")} for row in volumes}}


async def _validate_resumed_runtime(manifest: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    runtime = manifest["runtime"]
    # 与 prepare-real 相同，pytest 用户来自原已选配置；不写入或改造 manifest/config。
    pytest_binding = {**runtime["pytest"], "user": values["POSTGRES_USER"]}
    ready = await _wait_owned_postgres({**runtime, "pytest": pytest_binding}, values)
    for observed_name, expected_name in (("pytest", "pytest"), ("target", "browser_admin")):
        actual, expected = ready["postgres"][observed_name], manifest["service_identities"][expected_name]
        if any(actual.get(key) != expected.get(key) for key in ("database", "system_identifier")):
            raise ProbeError("resume original PostgreSQL database/system identifier changed")
    control_runtime.validate_embedding_health(ready["fake_embeddings_health"])
    target = runtime["target"]
    actual = await control_runtime._pg_identity(target["host"], target["port"], target["database"], target["user"], values["POSTGRES_PASSWORD"])
    expected = manifest["service_identities"]["target"]
    if any(actual.get(key) != expected.get(key) for key in ("database", "system_identifier")):
        raise ProbeError("resume actual target database/system identifier changed")
    consent = await _validate_final_verification_target(manifest, values)
    return {"postgres": ready["postgres"], "target": actual, "embedding_healthy": True, "consent": consent, "real_consent_active": True}


def _safe_resume_failure(exc: BaseException, stage: str) -> dict[str, Any]:
    result: dict[str, Any] = {"error_stage": stage, "error_classification": "controlled_operation_failed"}
    if isinstance(exc, ProbeError) and str(exc) == "owned run-01 PostgreSQL or fake embedding service did not become ready":
        result["error_classification"] = "database_or_embedding_timeout"
    cause = exc.__cause__
    if cause is not None:
        result["cause_type"] = type(cause).__name__
        if isinstance(cause, KeyError) and cause.args and cause.args[0] in {"host", "port", "database", "admin_database", "user"}:
            result["missing_binding_field"] = cause.args[0]
    return result


def _failed_resume_retry_evidence(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    first = _verified_repair_audit(manifest, "resume_owned")
    first_ref = manifest["resume_owned"]
    result_path = path / "evidence/resume-owned-result.json"
    if Path(first_ref["path"]).resolve() != (path / "evidence/resume-owned-audit.json").resolve() or not result_path.is_file():
        raise ProbeError("resume retry requires the original first failed resume audit/result")
    result = json.loads(result_path.read_text())
    if result.get("run_id") != manifest["run_id"] or result.get("state") != "stopped" or result.get("error_type") != "ProbeError" or result.get("rollback_failures") != [] or result.get("provider_requested") is not False or result.get("consent_requested") is not False or result.get("volumes_deleted") is not False:
        raise ProbeError("resume retry requires a fully rolled-back first failure without provider/consent activity")
    return {"first_audit": first_ref, "first_result": {"path": str(result_path), "sha256": _digest(result_path)}, "first_original_manifest": first["original_manifest"], "max_total_resume_attempts": 2}


def resume_owned(run_dir: str | Path, *, source_files: list[str] = (), reason: str, expected_e2e_sha256: str | None = None, expected_startup_sha256: str | None = None, retry_failed_once: bool = False) -> dict[str, Any]:
    path, original, selected, selection, values = load_run(run_dir)
    if path.resolve() != (MATERIAL_ROOT / "runs/run-01").resolve() or original.get("run_directory") != str(path) or original.get("state") != "stopped":
        raise ProbeError("resume-owned accepts only this exact stopped run-01 directory")
    if not reason.strip() or any(secret and secret in reason for key, secret in values.items() if key in SECRET_NAMES):
        raise ProbeError("resume-owned requires a credential-free recorded reason")
    requested = set(source_files)
    if requested - RESUME_HARNESS_FILES:
        raise ProbeError("resume-owned explicit source-file is outside the exact approved E2E harness paths")
    consent = original.get("consent", {})
    try:
        UUID(consent["consent_log_id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ProbeError("resume-owned original real consent UUID is invalid") from exc
    if consent.get("state") != "consent" or consent.get("operator_override") is not False or original["runtime"]["provider_profile"].get("model") != "qwen-plus":
        raise ProbeError("resume-owned requires original non-override qwen-plus consent")
    record_name = "resume-owned-retry" if retry_failed_once else "resume-owned"
    audit_path, archive = path / f"evidence/{record_name}-audit.json", path / f"evidence/{record_name}-originals"
    prior_failure = _failed_resume_retry_evidence(path, original) if retry_failed_once else None
    if audit_path.exists() or archive.exists() or (not retry_failed_once and original.get("resume_owned")):
        raise ProbeError("resume-owned is single use; prior attempt evidence is preserved")
    identities = original["service_identities"]
    runtime = original["runtime"]
    if runtime["target"]["host"] != "127.0.0.1" or runtime["pytest"]["host"] != "127.0.0.1" or runtime["target"]["port"] != original["ports"]["browser"] or runtime["pytest"]["port"] != original["ports"]["pytest"] or runtime["target"]["database"] != runtime["database"] or identities["target"].get("database") != runtime["database"] or identities["pytest"].get("database") != runtime["pytest"]["database"] or not identities["target"].get("system_identifier") or identities["target"]["system_identifier"] != identities["browser_admin"].get("system_identifier") or identities["pytest"].get("system_identifier") == identities["target"]["system_identifier"]:
        raise ProbeError("resume-owned target/two original PostgreSQL identity bindings differ")
    for role in ("api", "ui"):
        process = original.get(f"{role}_process")
        if process is not None and (process.get("run_id") != original["run_id"] or not isinstance(process.get("pid"), int) or _process_command(process["pid"])):
            raise ProbeError("resume-owned recorded PID is running or has changed identity")
    configuration = _resume_configuration_hashes(path, original, selected, selection)
    old = dict(original["source_fingerprints"])
    changes = {}
    for relative in set(old) | requested | RESUME_INTRINSIC_FILES:
        source = ROOT / relative
        if not source.is_file():
            raise ProbeError("resume-owned required selected source is missing")
        current = _digest(source)
        if relative not in requested | RESUME_INTRINSIC_FILES and current != old.get(relative):
            raise ProbeError(f"resume-owned refuses product/unapproved test source drift: {relative}")
        if relative in requested | RESUME_INTRINSIC_FILES:
            changes[relative] = {"previous": old.get(relative), "current": current}
    for relative, expected_sha in (("hexis-ui/e2e/tool-activity.spec.ts", expected_e2e_sha256), ("hexis-ui/lib/tool-activity-startup.test.ts", expected_startup_sha256)):
        if expected_sha is not None and _digest(ROOT / relative) != expected_sha:
            raise ProbeError("resume-owned exact selected E2E hash differs")
    before = _inspect_resume_resources(path, original, values)
    if set(before["services"]) != {"pytest-db", "browser-db", "fake-embeddings"} or any(row.get("container_id") != original["owned_resources"].get(name, {}).get("container_id") or row.get("state") not in {"exited", "created"} for name, row in before["services"].items()):
        raise ProbeError("resume-owned original container identity/state is not fully stopped")
    files = {"run-manifest.json": path / "run-manifest.json", **{f"evidence/{file.relative_to(path / 'evidence')}": file for file in (path / "evidence").rglob("*") if file.is_file()}}
    frozen = {name: file.read_bytes() for name, file in files.items()}
    archive.mkdir()
    retained = {}
    for name, raw in frozen.items():
        dest = archive / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("xb") as stream:
            os.chmod(dest, 0o600)
            stream.write(raw)
        retained[name] = {"path": str(dest), "sha256": hashlib.sha256(raw).hexdigest()}
    voice_refs = [ref for name, ref in retained.items() if name.endswith("cases/add-evidence.json") or name.endswith("add-evidence-voice-original.json")]
    voice_refs = [ref for ref in voice_refs if json.loads(Path(ref["path"]).read_text()).get("schema") != "bob108-add-evidence-text-case-v1"]
    if not voice_refs:
        raise ProbeError("resume-owned immutable prior voice add_evidence artifact is missing")
    audit = {"run_id": original["run_id"], "reason": reason.strip(), "changes": changes, "source_files": sorted(requested), "previous_source_fingerprints": old,
             "original_manifest": retained["run-manifest.json"], "retained_original_evidence": retained, "configuration_hashes": configuration, "original_resources": before,
             "acceptance_modes": {"add-evidence": "text_chat"}, "prior_voice_attempts": voice_refs, "provider_requested": False, "consent_requested": False, "volumes_deleted": False,
             "说明": "用户将add_evidence验收改为前台Text Chat；仅start原停止容器，保留全部历史voice证据，禁止provider/consent调用或容器重建。"}
    if prior_failure is not None:
        audit["prior_failed_resume"] = prior_failure
        audit["attempt_number"] = 2
    _exclusive_json(audit_path, audit)
    manifest = json.loads(json.dumps(original))
    manifest["resume_owned"] = {"path": str(audit_path), "sha256": _digest(audit_path)}
    start_invoked = False
    stage = "compose_start"
    try:
        command = _compose_command(path, original, _compose_environment_file(selection))
        start_invoked = True
        subprocess.run([*command, "start", "pytest-db", "browser-db", "fake-embeddings"], cwd=ROOT, check=True, capture_output=True, text=True)
        stage = "container_check"
        after = _inspect_resume_resources(path, original, values)
        if before["volumes"] != after["volumes"] or any({key: value for key, value in after["services"][name].items() if key != "state"} != {key: value for key, value in row.items() if key != "state"} or after["services"][name].get("state") != "running" for name, row in before["services"].items()):
            raise ProbeError("resume-owned original container/volume configuration changed during start")
        stage = "runtime_check"
        runtime_check = asyncio.run(_validate_resumed_runtime(manifest, values))
        for relative, change in changes.items():
            manifest["source_fingerprints"][relative] = change["current"]
        manifest["state"] = "consent_accepted"
        manifest["api_process"] = None
        manifest["ui_process"] = None
        manifest["acceptance_modes"] = {**manifest.get("acceptance_modes", {}), "add-evidence": "text_chat"}
        _atomic_json(path / "run-manifest.json", manifest)
        stage = "api_start"
        api = serve_api(path)
        stage = "ui_start"
        ui = serve_ui(path)
        stage = "readiness_1"
        first_ready = check_readiness(path)
        stage = "readiness_2"
        readiness = [first_ready, check_readiness(path)]
        result = {"run_id": manifest["run_id"], "state": "ready", "api": api, "ui": ui, "readiness": readiness, "runtime_check": runtime_check, "provider_requested": False, "consent_requested": False, "volumes_deleted": False}
        _exclusive_json(path / f"evidence/{record_name}-result.json", result)
        return result
    except BaseException as exc:
        failures = []
        current = json.loads((path / "run-manifest.json").read_text())
        for role in ("ui", "api"):
            process = current.get(f"{role}_process")
            if process is not None and process != original.get(f"{role}_process"):
                try:
                    _stop_run_process(process, run_dir=path, role=role)
                except Exception:
                    failures.append(f"{role}_identity_or_stop")
        if start_invoked:
            for name in before["services"]:
                try:
                    observed = _inspect_resume_resources(path, original, values, service_names=[name], include_volumes=False)
                    row = observed["services"][name]
                    if row.get("state") in {"running", "restarting", "paused"} and row["container_id"] == before["services"][name]["container_id"]:
                        subprocess.run(["docker", "stop", row["container_id"]], cwd=ROOT, check=True, capture_output=True, text=True)
                except Exception:
                    failures.append(f"{name}_identity_or_stop")
        stopped = json.loads(json.dumps(original))
        stopped["resume_owned"] = manifest["resume_owned"]
        stopped["state"] = "resume_failed" if failures else "stopped"
        _atomic_json(path / "run-manifest.json", stopped)
        failure = {"run_id": original["run_id"], "state": stopped["state"], "error_type": type(exc).__name__, **_safe_resume_failure(exc, stage), "rollback_failures": failures, "provider_requested": False, "consent_requested": False, "volumes_deleted": False}
        _exclusive_json(path / f"evidence/{record_name}-result.json", failure)
        raise ProbeError("resume-owned failed; only newly started verified owned resources were stopped, volumes and audit were retained") from exc


def pytest_command(run_dir: str | Path, pytest_args: list[str]) -> int:
    path, manifest, _selected, _selection, values = load_run(run_dir)
    if manifest.get("state") not in {"awaiting_consent", "consent_error", "consent_accepted", "ready"}:
        raise ProbeError("run-01 pytest requires prepared owned database services")
    runtime = manifest["runtime"]
    pytest_binding = runtime["pytest"]
    if (
        pytest_binding.get("host") != "127.0.0.1"
        or pytest_binding.get("port") != manifest["ports"]["pytest"]
        or pytest_binding.get("database") != "bob108_pytest"
    ):
        raise ProbeError("pytest runtime binding differs from the owned run manifest")
    identities = manifest["service_identities"]
    system_ids = []
    for name, database in (
        ("pytest", pytest_binding["database"]),
        ("browser_admin", runtime["target"]["admin_database"]),
    ):
        identity = identities.get(name)
        if not isinstance(identity, dict) or identity.get("database") != database or not identity.get("system_identifier"):
            raise ProbeError(f"{name} PostgreSQL identity differs from the owned run manifest")
        system_ids.append(str(identity["system_identifier"]))
    if len(set(system_ids)) != len(system_ids):
        raise ProbeError("pytest and browser PostgreSQL services share a system identifier")
    for service in ("pytest-db", "browser-db", "fake-embeddings"):
        expected = manifest.get("owned_resources", {}).get(service, {}).get("container_id")
        if not expected:
            raise ProbeError(f"run-owned {service} identity is missing")
    current = control_runtime._owned_resources(path, manifest)
    for service, record in current.items():
        if record.get("container_id") != manifest["owned_resources"].get(service, {}).get("container_id"):
            raise ProbeError(f"run-owned {service} container changed after prepare-real")
    target = runtime["target"]
    if (
        target.get("host") != "127.0.0.1"
        or target.get("port") != manifest["ports"]["browser"]
        or target.get("database") != runtime["database"]
        or target.get("user") != values["POSTGRES_USER"]
    ):
        raise ProbeError("browser runtime binding differs from the owned run manifest")
    browser_actual = asyncio.run(control_runtime._pg_identity(
        target["host"], target["port"], target["database"], target["user"], values["POSTGRES_PASSWORD"],
    ))
    expected_target = identities.get("target", {})
    if (
        expected_target.get("system_identifier") != identities["browser_admin"]["system_identifier"]
        or any(browser_actual.get(key) != expected_target.get(key) for key in ("database", "system_identifier"))
    ):
        raise ProbeError("browser PostgreSQL identity differs from the owned run manifest")
    pytest_runtime = {
        "pytest_host": pytest_binding["host"],
        "pytest_port": pytest_binding["port"],
        "pytest_user": values["POSTGRES_USER"],
        "pytest_db": pytest_binding["database"],
        "pytest_admin_db": pytest_binding["admin_database"],
        "embedding_host": "127.0.0.1",
        "embedding_port": manifest["ports"]["embeddings"],
        "embedding_dimension": 768,
        "fake_embeddings_url": f"http://127.0.0.1:{manifest['ports']['embeddings']}/api/embed",
        "embedding_model_id": "bob108-fake-768",
    }
    actual = asyncio.run(control_runtime._pg_identity(
        pytest_runtime["pytest_host"], pytest_runtime["pytest_port"], pytest_runtime["pytest_db"],
        pytest_runtime["pytest_user"], values["POSTGRES_PASSWORD"],
    ))
    if any(actual.get(key) != manifest["service_identities"]["pytest"].get(key) for key in ("database", "system_identifier")):
        raise ProbeError("pytest PostgreSQL identity differs from the owned run manifest")
    environment = control_runtime.build_pytest_environment(runtime_env=pytest_runtime, password=values["POSTGRES_PASSWORD"])
    args = pytest_args[1:] if pytest_args and pytest_args[0] == "--" else pytest_args
    return subprocess.run([sys.executable, "-m", "pytest", *args], cwd=ROOT, env=environment).returncode


def _stop_run_process(process: dict[str, Any] | None, *, run_dir: Path, role: str) -> None:
    if process is None:
        return
    if not _owned_process_alive(process, run_dir=run_dir, role=role):
        pid = process.get("pid") if isinstance(process, dict) else None
        if isinstance(pid, int) and _process_command(pid):
            raise ProbeError(f"refusing to stop a {role} PID that does not match this run")
        return
    try:
        os.killpg(int(process["pid"]), signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _process_command(int(process["pid"])):
        time.sleep(0.25)
    if _process_command(int(process["pid"])):
        raise ProbeError(f"run-owned {role} process did not stop cleanly")


def stop_owned(run_dir: str | Path) -> dict[str, Any]:
    path, manifest, _selected, selection, _values = load_run(run_dir)
    command = _compose_command(path, manifest, _compose_environment_file(selection))
    running = subprocess.run(
        [*command, "ps", "--status", "running", "-q"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    if running:
        current = control_runtime._owned_resources(path, manifest)
        for service, record in current.items():
            expected = manifest.get("owned_resources", {}).get(service, {}).get("container_id")
            if not expected or record.get("container_id") != expected:
                raise ProbeError(f"refusing to stop a container whose {service} identity changed")
    # Validate both process groups before signaling either one.
    for role, process in (("api", manifest.get("api_process")), ("ui", manifest.get("ui_process"))):
        if process is not None and not _owned_process_alive(process, run_dir=path, role=role):
            pid = process.get("pid") if isinstance(process, dict) else None
            if isinstance(pid, int) and _process_command(pid):
                raise ProbeError(f"refusing to stop an external {role} process")
    _stop_run_process(manifest.get("api_process"), run_dir=path, role="api")
    _stop_run_process(manifest.get("ui_process"), run_dir=path, role="ui")
    if running:
        try:
            subprocess.run([*command, "stop"], cwd=ROOT, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise ProbeError(f"owned Compose stop failed ({exc.returncode}); volumes were retained") from exc
    manifest["state"] = "stopped"
    manifest["stopped_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _atomic_json(path / "run-manifest.json", manifest)
    return {"run_id": manifest["run_id"], "state": "stopped", "volumes_retained": list(manifest["volumes"].values())}


def _persisted_event_matches(db_event: Any, event: dict[str, Any]) -> bool:
    if not isinstance(db_event, dict):
        return False
    keys = ("event_id", "session_id", "stream_id", "seq", "event_type", "payload")
    if any(db_event.get(key) != event.get(key) for key in keys):
        return False
    if "ts" not in db_event or "ts" not in event:
        return False
    if db_event["ts"] is None or event["ts"] is None:
        return db_event["ts"] is None and event["ts"] is None
    try:
        return datetime.fromisoformat(str(db_event["ts"]).replace("Z", "+00:00")) == datetime.fromisoformat(str(event["ts"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return False


def _canonical_message_json(value: Any) -> str:
    """与 E2E 的 UTF-16 key 顺序和 JSON.stringify 数字格式保持一致。"""
    if isinstance(value, dict):
        def key_order(key: str) -> tuple[int, Any]:
            if re.fullmatch(r"0|[1-9][0-9]*", key) and int(key) < 2**32 - 1:
                return 0, int(key)  # JSON.stringify 优先按数值枚举 array-index 属性。
            return 1, key.encode("utf-16-be", "surrogatepass")

        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False) + ":" + _canonical_message_json(value[key])
            for key in sorted(value, key=key_order)
        ) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_canonical_message_json(item) for item in value) + "]"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers cannot prove raw message preservation")
        if value == 0:
            return "0"
        number = Decimal(repr(value))
        if -6 <= number.adjusted() < 21:
            fixed = format(number, "f")
            return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
        mantissa, exponent = format(number.normalize(), "e").split("e")
        exponent_number = int(exponent)
        return f"{mantissa}e{'+' if exponent_number >= 0 else ''}{exponent_number}"
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _original_messages_summary(messages: Any, session_id: str) -> dict[str, Any]:
    fields = {"id", "session_id", "ordinal", "role", "content", "content_blocks", "source_message_id", "metadata"}
    if not isinstance(messages, list) or not messages:
        raise ValueError("original DB messages are missing or empty")
    for row in messages:
        if (
            not isinstance(row, dict) or set(row) != fields
            or row["session_id"] != session_id
            or not isinstance(row["ordinal"], int) or isinstance(row["ordinal"], bool)
            or not isinstance(row["role"], str) or not isinstance(row["content"], str)
            or not isinstance(row["content_blocks"], list) or not isinstance(row["metadata"], dict)
            or (row["source_message_id"] is not None and not isinstance(row["source_message_id"], str))
        ):
            raise ValueError("original DB message fields are incomplete or invalid")
        UUID(row["id"])
    if len({row["id"] for row in messages}) != len(messages) or len({row["ordinal"] for row in messages}) != len(messages):
        raise ValueError("original DB message identity or ordinal is duplicated")
    has_call = any(any(isinstance(block, dict) and block.get("type") == "toolCall" for block in row["content_blocks"]) for row in messages)
    has_result = any(row["role"] == "toolResult" or any(isinstance(block, dict) and block.get("type") == "toolResult" for block in row["content_blocks"]) for row in messages)
    if not has_call or not has_result:
        raise ValueError("original DB snapshot lacks toolCall or toolResult messages")
    canonical = _canonical_message_json(sorted(messages, key=lambda row: row["ordinal"]))
    return {"message_count": len(messages), "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def _original_tool_messages_error(evidence: dict[str, Any]) -> str | None:
    try:
        original = evidence["original_tool_messages"]
        task_session_id = evidence["task"]["task_session_id"]
        UUID(task_session_id)
        computed = []
        for side in ("before", "after"):
            captured = original[side]
            summary = _original_messages_summary(original[f"{side}_messages"], task_session_id)
            if isinstance(captured.get("message_count"), bool) or any(captured.get(key) != summary[key] for key in ("message_count", "sha256")):
                return "original tool messages declared count/hash differs from raw DB rows"
            computed.append(summary)
        final_messages = [row for row in evidence["snapshot"]["chat_messages"] if row.get("session_id") == task_session_id]
        final = _original_messages_summary(final_messages, task_session_id)
        if original.get("unchanged") is not True or computed[0] != computed[1] or computed[1] != final:
            return "original tool messages changed or differ from the final DB snapshot"
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return "original tool messages before/after raw DB evidence is missing or invalid"
    return None


def _historical_case_lineage(path: Path, manifest: dict[str, Any], name: str, raw: dict[str, Any]) -> dict[str, Any]:
    audit = _verified_repair_audit(manifest, "final_verification_refresh")
    if set(audit["changes"]) != set(FINAL_VERIFICATION_SOURCE_PATHS):
        raise ProbeError("historical assessment source change exceeds its exact approved scope")
    expected_current = dict(audit["previous_source_fingerprints"])
    for relative, change in audit["changes"].items():
        if expected_current.get(relative) != change["previous"]:
            raise ProbeError("historical assessment prior source baseline differs")
        expected_current[relative] = change["current"]
    expected_current = _sources_after_owned_resume(manifest, expected_current)
    if expected_current != manifest["source_fingerprints"]:
        raise ProbeError("historical assessment current source map differs from its audit")
    _assert_current_source_fingerprints(manifest)
    record = audit["case_artifacts"][name]
    original = path / f"evidence/cases/{name}.json"
    if Path(record["original_path"]).resolve() != original.resolve() or _digest(original) != record["sha256"]:
        raise ProbeError("historical case original bytes changed after its immutable audit")
    archived = Path(record["archive_path"])
    if _digest(archived) != record["sha256"] or json.loads(original.read_text()) != raw:
        raise ProbeError("historical case archive bytes or parsed ownership differs")
    producer = raw.get("evidence", {}).get("source_fingerprints")
    if producer != record["producer_source_fingerprints"]:
        raise ProbeError("historical case producer source map differs from its archive")
    product_keys = set(manifest["source_fingerprints"]) - set(FINAL_VERIFICATION_SOURCE_PATHS)
    if not isinstance(producer, dict) or set(producer) - set(FINAL_VERIFICATION_SOURCE_PATHS) != product_keys or any(producer[key] != manifest["source_fingerprints"][key] for key in product_keys):
        raise ProbeError("historical case product source differs from the current live product")
    return {"classification": "historical_attempt", "original_artifact": {"path": str(original), "sha256": record["sha256"]},
            "archive_artifact": {"path": str(archived), "sha256": record["sha256"]}, "producer_source_fingerprints": producer,
            "assessment_source_fingerprints": manifest["source_fingerprints"], "provider_rerun": False,
            "scope": "原provider attempt的冻结证据由新checker只读评估；不是当前test/harness源码的新执行。"}


def _case_source_error(path: Path, manifest: dict[str, Any], name: str, raw: dict[str, Any]) -> str | None:
    fingerprints = raw.get("evidence", {}).get("source_fingerprints")
    current_text = name == "add-evidence" and manifest.get("acceptance_modes", {}).get(name) == "text_chat" and raw.get("schema") == "bob108-add-evidence-text-case-v1" and raw.get("input_mode") == "text_chat"
    if manifest.get("final_verification_refresh") and not current_text:
        try:
            _historical_case_lineage(path, manifest, name, raw)
            return None
        except (ProbeError, KeyError, TypeError, ValueError, OSError):
            return "historical case byte ownership, source lineage or current product source is invalid"
    if not isinstance(fingerprints, dict) or not fingerprints or fingerprints != manifest.get("source_fingerprints"):
        return "case source hashes are missing or differ from the run manifest"
    for relative, expected in fingerprints.items():
        source = ROOT / relative
        if not source.is_file() or _digest(source) != expected:
            return "case source hash differs from the current source"
    return None


def _passing_text_add_evidence_error(path: Path, manifest: dict[str, Any], raw: dict[str, Any]) -> str | None:
    if raw.get("schema") != "bob108-add-evidence-text-case-v1" or raw.get("input_mode") != "text_chat":
        return "text add_evidence cannot reuse a voice WAV artifact"
    evidence = raw.get("evidence")
    if not isinstance(evidence, dict):
        return "text add_evidence lacks actual tool evidence"
    error = _case_source_error(path, manifest, "add-evidence", raw)
    if error:
        return error
    snapshot = evidence.get("snapshot")
    identity = manifest["service_identities"]["target"]
    origin = raw.get("origin_session_id")
    if not origin or not isinstance(snapshot, dict) or snapshot.get("run_id") != manifest["run_id"] or snapshot.get("origin_session_id") != origin or snapshot.get("database") != manifest["runtime"]["database"] or snapshot.get("system_identifier") != identity.get("system_identifier") or evidence.get("activity_session_id") != origin:
        return "text add_evidence DB/API session ownership differs"
    prompt = evidence.get("text_request")
    if not isinstance(prompt, str) or not prompt.strip() or hashlib.sha256(prompt.encode("utf-8")).hexdigest() != evidence.get("text_request_sha256"):
        return "text add_evidence original UTF-8 request hash differs"
    try:
        memory_id = str(UUID(evidence["memory_id"]))
        source = evidence["source"]
        if source.get("kind") != "repository_document":
            return "text add_evidence source kind is not repository_document"
        _verify_file_ref({"path": source["ref"], "sha256": source["sha256"]})
        screenshot = _absolute(evidence["screenshot"])
        if not screenshot.resolve().is_relative_to((path / "evidence").resolve()):
            return "text add_evidence screenshot is outside its owned evidence directory"
        _verify_file_ref({"path": str(screenshot), "sha256": evidence["screenshot_sha256"]})
    except (ProbeError, KeyError, TypeError, ValueError):
        return "text add_evidence UUID/source/screenshot hash is invalid"
    if memory_id not in prompt or source["ref"] not in prompt or "add_evidence" not in str(evidence.get("activity_dom", "")):
        return "text add_evidence request or Activity DOM lacks the actual target tool/source"
    events = evidence.get("activity_events")
    if not isinstance(events, list):
        return "text add_evidence API events are missing"
    call_id = evidence.get("call_id")
    if not call_id:
        return "text add_evidence lacks a persisted request/result call pair"
    if events:
        pair = [row for row in events if row.get("payload", {}).get("tool_name") == "add_evidence"]
        if len(pair) != 2 or {row.get("event_type") for row in pair} != {"TOOL_START", "TOOL_RESULT"} or any(row.get("session_id") != origin or row["payload"].get("call_id") != call_id for row in pair):
            return "text add_evidence persisted request/result call pair differs"
        db_events = {row.get("event_id"): row for row in snapshot.get("events", [])}
        cursor = evidence.get("api_cursor", {})
        for event in pair:
            if not _persisted_event_matches(db_events.get(event.get("event_id")), event) or not isinstance(event.get("seq"), int) or isinstance(event.get("seq"), bool) or not isinstance(cursor.get(event.get("stream_id")), int) or cursor[event["stream_id"]] < event["seq"]:
                return "text add_evidence API/DB event identity or cursor differs"
        start = next(row for row in pair if row["event_type"] == "TOOL_START")
        result = next(row for row in pair if row["event_type"] == "TOOL_RESULT")
    else:
        start = evidence.get("tool_call")
        result = evidence.get("tool_result")
        if not isinstance(start, dict) or not isinstance(result, dict):
            return "text add_evidence lacks its declared persisted tool call/result"
        if start.get("session_id") != origin or start.get("event_type") != "TOOL_START" or start.get("payload", {}).get("call_id") != call_id:
            return "text add_evidence persisted toolCall identity differs"
        if result.get("session_id") != origin or result.get("event_type") != "TOOL_RESULT" or result.get("payload", {}).get("call_id") != call_id:
            return "text add_evidence persisted toolResult identity differs"
        if result.get("payload", {}).get("tool_name") != "add_evidence":
            return "text add_evidence persisted toolResult tool name differs"
    if evidence.get("tool_call") != start or evidence.get("tool_result") != result or (events and evidence.get("event_ids") != [row["event_id"] for row in pair]):
        return "text add_evidence declared raw call/result/event IDs differ"
    persisted_result = evidence.get("tool_result_message")
    persisted_tool_result = persisted_result.get("metadata", {}).get("tool_result") if isinstance(persisted_result, dict) else None
    if not isinstance(persisted_tool_result, dict) or persisted_tool_result.get("isError") is not False:
        return "text add_evidence persisted toolResult is missing or failed"
    arguments = start["payload"].get("arguments", {})
    if (
        arguments.get("memory_id") != memory_id
        or arguments.get("stance") != "supports"
        or arguments.get("source") != {"kind": source["kind"], "ref": source["ref"]}
        or result["payload"].get("success") is not True
        or result["payload"].get("error") is not None
        or result["payload"].get("state") in {"accepted", "unknown", "failed"}
    ):
        return "text add_evidence tool target/source or real success result differs"
    messages = [row for row in snapshot.get("chat_messages", []) if row.get("session_id") == origin]
    call_messages = [
        row for row in messages
        if row.get("role") == "assistant"
        and any(
            block.get("type") == "toolCall"
            and block.get("id") == call_id
            and block.get("name") == "add_evidence"
            for block in row.get("content_blocks", [])
        )
    ]
    if len(call_messages) != 1 or call_messages[0] != evidence.get("tool_call_message"):
        return "text add_evidence lacks its actual persisted toolCall chat message"
    chat_call_blocks = [
        block for block in call_messages[0].get("content_blocks", [])
        if block.get("type") == "toolCall" and block.get("id") == call_id
    ]
    if len(chat_call_blocks) != 1 or chat_call_blocks[0].get("arguments") != arguments:
        return "text add_evidence persisted toolCall arguments differ"
    result_messages = [
        row for row in messages
        if row.get("role") == "toolResult"
        and row.get("metadata", {}).get("tool_result", {}).get("toolCallId") == call_id
        and row.get("metadata", {}).get("tool_result", {}).get("toolName") == "add_evidence"
    ]
    if len(result_messages) != 1 or result_messages[0] != evidence.get("tool_result_message"):
        return "text add_evidence lacks its actual persisted toolResult chat message"
    persisted_tool_result = result_messages[0]["metadata"]["tool_result"]
    if persisted_tool_result.get("isError") is not False:
        return "text add_evidence persisted toolResult is failed"
    memory = evidence.get("memory")
    if not isinstance(memory, dict) or memory.get("id") != memory_id or memory.get("type") != "semantic" or memory.get("status") != "active" or memory not in snapshot.get("memories", []):
        return "text add_evidence UUID does not identify the actual active semantic DB memory"
    revision = evidence.get("revision_audit")
    revision_evidence = revision.get("evidence") if isinstance(revision, dict) else None
    if (
        not isinstance(revision, dict)
        or revision.get("memory_id") != memory_id
        or revision not in snapshot.get("belief_revision_audit", [])
        or not isinstance(revision_evidence, dict)
        or revision_evidence.get("kind") != source["kind"]
        or revision_evidence.get("ref") != source["ref"]
    ):
        return "text add_evidence actual memory/source revision row is missing or differs"
    return None


def _passing_case_evidence_error(path: Path, manifest: dict[str, Any], name: str, raw: dict[str, Any]) -> str | None:
    """Cross-check persisted identities and artifacts before accepting a case claim."""
    if name == "add-evidence" and manifest.get("acceptance_modes", {}).get(name) == "text_chat":
        return _passing_text_add_evidence_error(path, manifest, raw)
    evidence = raw.get("evidence")
    if not isinstance(evidence, dict):
        return "passing case lacks evidence"
    required_tools = {
        "voice-recall": {"voice_recall"}, "remember": {"remember"},
        "add-evidence": {"add_evidence"}, "background-tools": {"recall", "read_file"},
    }[name]
    snapshot_value = evidence.get("snapshot")
    expected_identity = manifest["service_identities"]["target"]
    if (
        not isinstance(snapshot_value, dict)
        or not expected_identity.get("system_identifier")
        or snapshot_value.get("run_id") != manifest["run_id"]
        or snapshot_value.get("database") != manifest["runtime"]["database"]
        or snapshot_value.get("system_identifier") != expected_identity.get("system_identifier")
        or snapshot_value.get("origin_session_id") != raw.get("origin_session_id")
        or raw.get("run", {}).get("database") != manifest["runtime"]["database"]
    ):
        return "passing case DB ownership differs from the run manifest"
    source_error = _case_source_error(path, manifest, name, raw)
    if source_error:
        return source_error
    try:
        screenshot = _absolute(evidence.get("screenshot", ""))
        if not screenshot.resolve().is_relative_to((path / "evidence").resolve()):
            return "passing case screenshot is outside its owned evidence directory"
        _verify_file_ref({"path": str(screenshot), "sha256": evidence.get("screenshot_sha256")})
        _verify_file_ref(evidence.get("wav", {}))
    except (ProbeError, KeyError, TypeError, ValueError):
        return "passing case screenshot or WAV hash evidence is missing or invalid"
    dom = evidence.get("activity_dom")
    if not isinstance(dom, str) or not dom.strip() or any(tool not in dom for tool in required_tools):
        return "passing case lacks expected tool Activity DOM evidence"
    owner_session = raw.get("origin_session_id")
    if name == "background-tools":
        task = evidence.get("task")
        if not isinstance(task, dict) or not task.get("task_id") or not task.get("task_session_id"):
            return "background case lacks task ownership"
        owner_session = task["task_session_id"]
        if owner_session == raw.get("origin_session_id"):
            return "background case Activity belongs to the origin session"
    if not owner_session or evidence.get("activity_session_id") != owner_session:
        return "passing case Activity session ownership is missing or invalid"
    events = evidence.get("task_activity") if name == "background-tools" else evidence.get("activity_events")
    if not isinstance(events, list) or not events:
        return "passing case lacks raw tool events"
    db_events = snapshot_value.get("events")
    if not isinstance(db_events, list):
        return "passing case lacks raw DB event rows"
    db_by_id = {event.get("event_id"): event for event in db_events if isinstance(event, dict)}
    pairs: dict[tuple[str, str], set[str]] = {}
    event_ids: list[str] = []
    call_ids: set[str] = set()
    cursor = evidence.get("api_cursor", evidence.get("cursor"))
    if not isinstance(cursor, dict) or not cursor:
        return "passing case lacks API cursor evidence"
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
            return "passing case raw event shape is invalid"
        event_id, payload = event.get("event_id"), event["payload"]
        call_id, tool = payload.get("call_id"), payload.get("tool_name")
        if (
            not isinstance(event_id, str) or not event_id
            or not isinstance(call_id, str) or not call_id
            or tool not in required_tools or event.get("session_id") != owner_session
            or event.get("event_type") not in {"TOOL_START", "TOOL_RESULT"}
            or not _persisted_event_matches(db_by_id.get(event_id), event)
        ):
            return "passing case raw event/call ID or DB/session association is invalid"
        stream, seq = event.get("stream_id"), event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or not isinstance(cursor.get(stream), int) or cursor[stream] < seq:
            return "passing case API cursor does not cover raw events"
        event_ids.append(event_id)
        call_ids.add(call_id)
        if payload.get("state") != "accepted":
            pairs.setdefault((tool, call_id), set()).add(event["event_type"])
    if (
        len(set(event_ids)) != len(event_ids) or evidence.get("event_ids") != event_ids
        or not isinstance(evidence.get("call_ids"), list)
        or not all(isinstance(call, str) for call in evidence["call_ids"])
        or set(evidence["call_ids"]) != call_ids
    ):
        return "passing case declared event/call IDs do not match raw events"
    complete = [tool for (tool, _call), kinds in pairs.items() if kinds == {"TOOL_START", "TOOL_RESULT"}]
    if not required_tools.issubset(complete) or any(kinds != {"TOOL_START", "TOOL_RESULT"} for kinds in pairs.values()):
        return "passing case lacks a request and terminal result for each expected tool call"
    if name in {"remember", "add-evidence"}:
        receipt = evidence.get("receipt")
        if not isinstance(receipt, dict):
            return "passing memory case lacks an authoritative action receipt"
        try:
            memory_id = _done_receipt_memory_uuid(receipt)
        except ProbeError:
            return "passing memory case receipt lacks a valid data.memory_id UUID"
        tool_name = "remember" if name == "remember" else "add_evidence"
        if memory_id is None or receipt.get("tool_name") != tool_name or receipt.get("actual_call_id") not in call_ids or receipt.get("origin_session_id") != raw["origin_session_id"]:
            return "passing memory case receipt is not the matching authoritative done result"
        if not any(action.get("command_id") == receipt.get("command_id") and action.get("actual_call_id") == receipt.get("actual_call_id") and action.get("tool_name") == tool_name and action.get("state") == "done" and action.get("result") == receipt.get("result") for action in snapshot_value.get("actions", []) if isinstance(action, dict)):
            return "passing memory case receipt differs from the raw target DB action"
        memory = next((row for row in snapshot_value.get("memories", []) if row.get("id") == str(memory_id) and row.get("type") == "semantic" and row.get("status") == "active"), None)
        if memory is None:
            return "passing memory case data UUID lacks its real active semantic DB memory"
        if name == "remember" and evidence.get("memory") != memory:
            return "remember case memory differs from its actual target DB row"
        if name == "add-evidence":
            if evidence.get("bound_context", {}).get("memory_id") != str(memory_id):
                return "add-evidence receipt data UUID differs from the bound target memory"
            revision = evidence.get("revision_audit")
            if not isinstance(revision, dict) or revision.get("memory_id") != str(memory_id) or revision not in snapshot_value.get("belief_revision_audit", []):
                return "add-evidence case lacks its actual target UUID belief revision row"
    if name == "add-evidence":
        context = evidence.get("bound_context")
        try:
            if not isinstance(context, dict) or context.get("run_id") != manifest["run_id"] or not context.get("memory_id"):
                return "add-evidence case lacks bound memory/source identity"
            _verify_file_ref({"path": context["source"]["ref"], "sha256": context.get("source_sha256")})
        except (ProbeError, KeyError, TypeError, ValueError):
            return "add-evidence case source hash is missing or invalid"
    if name == "background-tools":
        read_results = [event for event in events if event["event_type"] == "TOOL_RESULT" and event["payload"]["tool_name"] == "read_file"]
        if complete.count("read_file") < 2 or not any(event["payload"].get("success") is True for event in read_results) or not any(event["payload"].get("success") is False for event in read_results):
            return "background case lacks separate read_file success and failure call pairs"
        observations = evidence.get("observed_event_state_snapshots")
        if not isinstance(observations, list):
            return "background case lacks event-time working state evidence"
        observed = {item.get("event", {}).get("event_id"): item for item in observations if isinstance(item, dict) and isinstance(item.get("event"), dict)}
        for event in events:
            if event["event_type"] != "TOOL_START":
                continue
            item = observed.get(event["event_id"], {})
            state = item.get("snapshot", {})
            if (
                item.get("event") != event or not item.get("observed_at")
                or state.get("run_id") != manifest["run_id"]
                or state.get("origin_session_id") != raw.get("origin_session_id")
                or state.get("database") != manifest["runtime"]["database"]
                or state.get("system_identifier") != expected_identity.get("system_identifier")
                or not any(row.get("id") == task["task_id"] and row.get("task_session_id") == owner_session and row.get("product_state") == "working" for row in state.get("tasks", []) if isinstance(row, dict))
                or not any(row.get("id") == event["payload"].get("execution_id") and row.get("task_id") == task["task_id"] and row.get("status") == "running" for row in state.get("work", []) if isinstance(row, dict))
            ):
                return "background request lacks matching event-time working/running ownership"
        original_error = _original_tool_messages_error(evidence)
        if original_error:
            return original_error
    return None


def _coverage_source(test_id: str) -> dict[str, Any]:
    relative, selector = test_id.split("::", 1)
    source = ROOT / relative
    result: dict[str, Any] = {
        "test_id": test_id,
        "path": str(source),
        "status": "NOT_RUN",
    }
    if source.is_file():
        lines = source.read_text(encoding="utf-8").splitlines()
        pattern = (
            rf"\bdef {re.escape(selector)}\("
            if relative.endswith(".py")
            else rf"\bit\([\"']{re.escape(selector)}[\"']"
        )
        line = next(
            (
                index
                for index, content in enumerate(lines, 1)
                if re.search(pattern, content)
            ),
            None,
        )
        result.update({"sha256": _digest(source), "line": line})
        if line:
            result["status"] = "SOURCE"
    return result


def _native_test_results(report: Path, framework: str) -> dict[str, list[str]]:
    results: dict[str, list[str]] = {}
    if framework == "pytest":
        for case in ET.fromstring(report.read_text(encoding="utf-8")).iter("testcase"):
            relative = (
                case.get("file") or case.get("classname", "").replace(".", "/") + ".py"
            )
            name = case.get("name", "").split("[", 1)[0]
            status = (
                "FAIL"
                if case.find("failure") is not None or case.find("error") is not None
                else "NOT_RUN" if case.find("skipped") is not None else "PASS"
            )
            results.setdefault(f"{relative}::{name}", []).append(status)
    elif framework == "vitest":
        raw = json.loads(report.read_text(encoding="utf-8"))
        for suite in raw["testResults"]:
            source = _absolute(suite["name"]).resolve()
            relative = str(source.relative_to(ROOT.resolve()))
            for assertion in suite["assertionResults"]:
                status = {"passed": "PASS", "failed": "FAIL"}.get(
                    assertion["status"], "NOT_RUN"
                )
                results.setdefault(f"{relative}::{assertion['title']}", []).append(
                    status
                )
    else:
        raise ProbeError("unsupported coverage test report framework")
    if not results:
        raise ProbeError("coverage test report has no individual test results")
    return results


def _coverage_test_report(
    receipt_path: str | Path, manifest: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    record: dict[str, Any] = {"path": str(receipt_path), "status": "FAIL"}
    try:
        path = _absolute(receipt_path)
        receipt = json.loads(path.read_text(encoding="utf-8"))
        record["sha256"] = _digest(path)
        if (
            receipt.get("schema") != "bob108-tool-activity-test-report-v1"
            or receipt.get("run_id") != manifest["run_id"]
        ):
            raise ProbeError("coverage test receipt schema or run identity differs")
        framework = receipt["framework"]
        if framework == "pytest":
            identity = receipt.get("database_identity", {})
            expected = manifest.get("service_identities", {}).get("pytest", {})
            if not expected.get("system_identifier") or any(
                identity.get(key) != expected.get(key)
                for key in ("database", "system_identifier")
            ):
                raise ProbeError(
                    "pytest coverage receipt is not bound to the owned isolated database"
                )
            record["database_identity"] = identity
        report = _verify_file_ref(receipt["report"])
        fingerprints = receipt.get("source_fingerprints", {})
        if not manifest.get("source_fingerprints") or any(
            fingerprints.get(key) != value
            for key, value in manifest["source_fingerprints"].items()
        ):
            raise ProbeError(
                "coverage test receipt does not bind the current run source fingerprints"
            )
        for relative, expected_hash in fingerprints.items():
            source = (ROOT / relative).resolve()
            if (
                not source.is_relative_to(ROOT.resolve())
                or not source.is_file()
                or _digest(source) != expected_hash
            ):
                raise ProbeError("coverage test source hash is missing or has drifted")
        results = _native_test_results(report, framework)
        if any(test_id.split("::", 1)[0] not in fingerprints for test_id in results):
            raise ProbeError(
                "coverage report contains a test without its execution source hash"
            )
        statuses = [status for items in results.values() for status in items]
        record.update(
            {
                "status": "TEST",
                "framework": framework,
                "run_id": receipt["run_id"],
                "report": dict(receipt["report"]),
                "source_fingerprints": fingerprints,
                "execution_counts": {
                    status: statuses.count(status)
                    for status in ("PASS", "FAIL", "NOT_RUN")
                },
                "execution_status": (
                    "FAIL"
                    if "FAIL" in statuses
                    else "NOT_RUN" if "NOT_RUN" in statuses else "PASS"
                ),
            }
        )
        return record, results
    except (
        ProbeError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        ET.ParseError,
    ):
        record["reason"] = (
            "test execution receipt, report hash, source hash, or isolated DB identity is invalid"
        )
        return record, {}


def _owned_control_artifact(evidence_dir: Path, value: str) -> dict[str, str]:
    path = Path(value)
    if not path.is_absolute():
        path = evidence_dir / path
    if (
        not path.resolve().is_relative_to(evidence_dir.resolve())
        or not path.is_file()
        or not path.stat().st_size
    ):
        raise ProbeError(
            "controlled artifact is missing or outside its evidence directory"
        )
    # Legacy summaries did not freeze hashes at capture time; this is a current read hash.
    return {"path": str(path), "sha256": _digest(path)}


def _controlled_coverage(
    run_dir: str | Path,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    record: dict[str, Any] = {"path": str(run_dir), "status": "FAIL", "summaries": []}
    by_row: dict[str, list[dict[str, Any]]] = {}
    try:
        path = _absolute(run_dir)
        manifest_path = path / "run-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        run_id = manifest["run_id"]
        identity = manifest["service_identities"]["browser"]
        if (
            not isinstance(run_id, str)
            or not run_id
            or not identity.get("system_identifier")
            or identity.get("database") != manifest["runtime"]["browser_db"]
        ):
            raise ProbeError("controlled manifest identity is invalid")
        record.update(
            {
                "run_id": run_id,
                "manifest": {
                    "path": str(manifest_path),
                    "sha256": _digest(manifest_path),
                },
                "database_identity": identity,
                "hash_observed_at_verification": True,
                "source_fingerprints": manifest.get("source_fingerprints", {}),
            }
        )
        evidence_dir = path / "evidence"
        scenarios = {
            "sprint1-browser-chat-activity.json": ("original-tool-messages-unchanged",),
            "sprint2-voice-recall-replay.json": ("refresh-replay",),
            "sprint3-background-task-activity.json": (
                "origin-task-isolation",
                "refresh-replay",
            ),
        }
        for name, rows in scenarios.items():
            summary = evidence_dir / name
            if not summary.is_file():
                continue
            item: dict[str, Any] = {
                "status": "FAIL",
                "summary": _owned_control_artifact(evidence_dir, str(summary)),
            }
            try:
                raw = json.loads(summary.read_text(encoding="utf-8"))
                claimed_run = raw.get("run_id", raw.get("runtime", {}).get("run_id"))
                if claimed_run is not None and claimed_run != run_id:
                    raise ProbeError("controlled summary run identity differs")
                item["run_id_source"] = (
                    "summary" if claimed_run is not None else "owning_manifest_only"
                )
                if name.startswith("sprint1"):
                    if (
                        raw["database_identity"]
                        != {
                            key: identity[key]
                            for key in ("database", "system_identifier")
                        }
                        or not raw["session_id"]
                        or not raw["tool_call_id"]
                        or raw["activity_dom"].get("rows_separate") is not True
                    ):
                        raise ProbeError(
                            "controlled text Chat summary lacks identity or DOM observations"
                        )
                    screenshots = [raw["screenshot"]]
                elif name.startswith("sprint2"):
                    events = raw["events"]
                    replay = raw["browser_replay_after_reload"]
                    if (
                        not raw["session_id"]
                        or not raw["call_id"]
                        or len(events) != 2
                        or len({event["event_id"] for event in events}) != 2
                        or {event["event_type"] for event in events}
                        != {"TOOL_START", "TOOL_RESULT"}
                        or any(
                            not event["stream_id"] or not isinstance(event["seq"], int)
                            for event in events
                        )
                        or replay.get("duplicate_rows") is not False
                        or replay.get("recent_events") != 2
                        or replay.get("request_visible") is not True
                        or replay.get("result_visible") is not True
                    ):
                        raise ProbeError(
                            "controlled replay summary lacks event identity or replay observations"
                        )
                    screenshots = [raw["screenshot"]]
                else:
                    task_a, task_b = raw["task_a"], raw["task_b"]
                    sessions = {
                        raw["origin_session_id"],
                        task_a["task_session_id"],
                        task_b["task_session_id"],
                    }
                    replay = raw["browser_replay_after_session_switch"]
                    api = raw["api_replay"]
                    before = raw["barrier"]["before_release"]
                    if (
                        raw["runtime"]["browser_database"] != identity["database"]
                        or len(sessions) != 3
                        or not task_a["call_id"]
                        or task_a["request_event_id"] == task_a["result_event_id"]
                        or task_a["task_id"] == task_b["task_id"]
                        or before.get("task_product_state") != "working"
                        or before.get("work_round_status") != "running"
                        or replay.get("duplicates") is not False
                        or replay.get("task_a_rows_after_reopen") != 2
                        or any(
                            replay.get(key) != 0
                            for key in (
                                "task_b_tool_activity_count",
                                "origin_tool_activity_count",
                            )
                        )
                        or api["task_a_session"].get("tool_events") != 2
                        or any(
                            api[key].get("tool_events") != 0
                            for key in ("task_b_session", "origin_session")
                        )
                    ):
                        raise ProbeError(
                            "controlled task summary lacks isolation or working-state observations"
                        )
                    screenshots = list(raw["screenshots"].values())
                    if not screenshots:
                        raise ProbeError("controlled task summary has no screenshots")
                item.update(
                    {
                        "status": "CONTROLLED",
                        "screenshots": [
                            _owned_control_artifact(evidence_dir, value)
                            for value in screenshots
                        ],
                        "scope": "historical controlled Browser summary; not raw DB/API/DOM or real-provider proof",
                    }
                )
            except (
                ProbeError,
                OSError,
                KeyError,
                TypeError,
                ValueError,
                AttributeError,
            ):
                item["reason"] = (
                    "controlled summary identity, required structure, or artifact ownership is invalid"
                )
            record["summaries"].append(item)
            for row in rows:
                by_row.setdefault(row, []).append(item)
        record["status"] = (
            "FAIL"
            if any(item["status"] == "FAIL" for item in record["summaries"])
            else "CONTROLLED" if record["summaries"] else "NOT_RUN"
        )
    except (ProbeError, OSError, KeyError, TypeError, ValueError, AttributeError):
        record["reason"] = "controlled run manifest or evidence ownership is invalid"
    return record, by_row


def verification_coverage(
    manifest: dict[str, Any],
    *,
    coverage_run_dirs: list[str | Path] = (),
    coverage_test_reports: list[str | Path] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read explicit inputs only; SOURCE/TEST/CONTROLLED never imply S4 product PASS."""
    inputs: dict[str, Any] = {"controlled_runs": [], "test_reports": []}
    test_results: dict[str, list[dict[str, Any]]] = {}
    controlled: dict[str, list[dict[str, Any]]] = {}
    for receipt in coverage_test_reports:
        record, results = _coverage_test_report(receipt, manifest)
        inputs["test_reports"].append(record)
        for test_id, statuses in results.items():
            test_results.setdefault(test_id, []).append(
                {"receipt": str(receipt), "statuses": statuses}
            )
    for run_dir in coverage_run_dirs:
        record, rows = _controlled_coverage(run_dir)
        inputs["controlled_runs"].append(record)
        for name, items in rows.items():
            controlled.setdefault(name, []).extend(items)
    coverage: dict[str, Any] = {}
    for name, selectors in COVERAGE_TESTS.items():
        sources = [_coverage_source(test_id) for test_id in selectors]
        tests = [
            {"test_id": test_id, "execution": test_results.get(test_id, [])}
            for test_id in selectors
        ]
        statuses = [
            status
            for test in tests
            for report in test["execution"]
            for status in report["statuses"]
        ]
        complete = all(test["execution"] for test in tests) and all(
            source["status"] == "SOURCE" for source in sources
        )
        status = (
            "FAIL"
            if "FAIL" in statuses
            else (
                "PASS"
                if complete and statuses and all(item == "PASS" for item in statuses)
                else "NOT_RUN"
            )
        )
        reason = (
            "current source-bound regression execution"
            if status == "PASS"
            else (
                "regression failed"
                if status == "FAIL"
                else "current source-bound regression execution is missing, skipped, or incomplete"
            )
        )
        if name == "original-tool-messages-unchanged" and status != "FAIL":
            status, reason = (
                "NOT_RUN",
                "message shape/rendering tests do not prove original content unchanged; before/after raw message evidence is required",
            )
        coverage[name] = {
            "status": status,
            "reason": reason,
            "scope": "S1-S3 regression coverage; separate from S4 real-provider cases",
            "source_coverage": sources,
            "test_execution": tests,
            "controlled_evidence": controlled.get(name, []),
        }
    return coverage, inputs


def _not_run_case_evidence_error(path: Path, manifest: dict[str, Any], name: str, raw: dict[str, Any]) -> str | None:
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return "NOT_RUN case requires a nonempty recorded reason"
    evidence = raw.get("evidence")
    if not isinstance(evidence, dict) or _case_source_error(path, manifest, name, raw):
        return "NOT_RUN case source hashes are missing or differ from the run manifest"
    snapshot = evidence.get("snapshot")
    identity = manifest["service_identities"]["target"]
    if not isinstance(snapshot, dict) or snapshot.get("run_id") != manifest["run_id"] or snapshot.get("database") != manifest["runtime"]["database"] or snapshot.get("system_identifier") != identity.get("system_identifier") or not raw.get("origin_session_id") or snapshot.get("origin_session_id") != raw["origin_session_id"]:
        return "NOT_RUN case requires a source snapshot from its owned target session"
    if name == "add-evidence":
        if reason.strip() != "本轮 remember 没有真实 done memory UUID，add_evidence 依赖未满足。":
            return "NOT_RUN add_evidence dependency reason differs from the recorded remember requirement"
        actions, events = snapshot.get("actions"), snapshot.get("events")
        if not isinstance(actions, list) or not isinstance(events, list):
            return "NOT_RUN add_evidence snapshot must include raw actions and events"
        for action in actions:
            if action.get("tool_name") == "remember" and action.get("state") == "done":
                memory_id = _done_receipt_memory_uuid(action)
                if memory_id is not None:
                    return "NOT_RUN add_evidence dependency already has a real remember done UUID"
            if action.get("tool_name") == "add_evidence":
                return "NOT_RUN add_evidence snapshot contains an executed dependency tool"
        if any(event.get("event_type") in {"TOOL_START", "TOOL_RESULT"} and event.get("payload", {}).get("tool_name") == "add_evidence" for event in events):
            return "NOT_RUN add_evidence snapshot contains an actual tool event"
        dependency_file = path / "evidence/cases/remember.json"
        if not dependency_file.is_file():
            return "NOT_RUN add_evidence dependency case evidence is missing"
        dependency = json.loads(dependency_file.read_text(encoding="utf-8"))
        if dependency.get("run_id") != manifest["run_id"] or dependency.get("origin_session_id") != raw["origin_session_id"] or dependency.get("status") not in {"FAIL", "NOT_RUN"}:
            return "NOT_RUN add_evidence dependency case does not confirm an unmet remember result"
    return None


def verify_run(
    run_dir: str | Path,
    *,
    coverage_run_dirs: list[str | Path] = (),
    coverage_test_reports: list[str | Path] = (),
) -> dict[str, Any]:
    path, manifest, _selected, _selection, values = load_run(run_dir)
    cases_dir = path / "evidence" / "cases"
    expected_cases = ("voice-recall", "remember", "add-evidence", "background-tools")
    cases: dict[str, dict[str, Any]] = {}
    for name in expected_cases:
        case_file = cases_dir / f"{name}.json"
        if not case_file.is_file():
            cases[name] = {
                "status": "NOT_RUN",
                "reason": "case evidence file is missing",
            }
            continue
        try:
            raw = json.loads(case_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            cases[name] = {
                "status": "FAIL",
                "reason": "case evidence file is not valid JSON",
            }
            continue
        text_mode = name == "add-evidence" and manifest.get("acceptance_modes", {}).get(name) == "text_chat"
        expected_schema = "bob108-add-evidence-text-case-v1" if text_mode else "bob108-tool-activity-case-v1"
        if not isinstance(raw, dict) or raw.get("schema") != expected_schema or (text_mode and raw.get("input_mode") != "text_chat"):
            cases[name] = {
                "status": "FAIL",
                "reason": "case evidence schema is invalid",
            }
            continue
        if raw.get("run_id") != manifest["run_id"] or raw.get("case") != name:
            cases[name] = {
                "status": "FAIL",
                "reason": "case identity differs from run manifest",
            }
            continue
        status = raw.get("status")
        if status not in {"PASS", "FAIL", "NOT_RUN"}:
            cases[name] = {"status": "FAIL", "reason": "case status is invalid"}
            continue
        cases[name] = {
            "status": status,
            **({"reason": raw.get("reason")} if raw.get("reason") else {}),
        }
        if manifest.get("final_verification_refresh") and not text_mode:
            try:
                cases[name]["source_lineage"] = _historical_case_lineage(path, manifest, name, raw)
            except (ProbeError, KeyError, TypeError, ValueError, OSError):
                cases[name] = {"status": "FAIL", "reason": "historical case immutable bytes, source lineage or ownership differs"}
                continue
        if text_mode:
            cases[name]["input_mode"] = "text_chat"
        if status == "PASS":
            try:
                reason = _passing_case_evidence_error(path, manifest, name, raw)
            except (KeyError, TypeError, ValueError, AttributeError, OSError):
                reason = "passing case evidence shape is invalid or an artifact cannot be read"
            if reason:
                cases[name].update({"status": "FAIL", "reason": reason})
        elif status == "NOT_RUN":
            try:
                reason = _not_run_case_evidence_error(path, manifest, name, raw)
            except (KeyError, TypeError, ValueError, AttributeError, OSError):
                reason = "NOT_RUN case reason, dependency or source snapshot is invalid"
            if reason:
                cases[name].update({"status": "FAIL", "reason": reason})
    secrets = [values.get("POSTGRES_PASSWORD", ""), *(values.get(key, "") for key in set(SECRET_NAMES))]
    leaks: list[str] = []
    evidence_dir = path / "evidence"
    for file in evidence_dir.rglob("*"):
        if not file.is_file() or file.suffix.lower() in {".png", ".jpg", ".jpeg", ".wav", ".webm"}:
            continue
        try:
            content = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(secret and secret in content for secret in secrets):
            leaks.append(str(file))
    if leaks:
        raise ProbeError("secret material was found in run evidence; affected paths are preserved for review")
    coverage, coverage_inputs = verification_coverage(
        manifest, coverage_run_dirs=coverage_run_dirs, coverage_test_reports=coverage_test_reports,
    )
    if cases["background-tools"]["status"] == "PASS":
        coverage["original-tool-messages-unchanged"].update({
            "status": "PASS", "evidence_level": "DB",
            "reason": "raw case task-session DB messages before/after and final snapshot have identical canonical hashes and counts",
            "case_evidence_file": str(cases_dir / "background-tools.json"),
        })
    result = {
        "run_id": manifest["run_id"], "state": manifest["state"], "cases": cases, "secrets_found": False,
        "case_evidence_files": {name: str(cases_dir / f"{name}.json") for name in expected_cases if (cases_dir / f"{name}.json").is_file()},
        "coverage": coverage, "coverage_inputs": coverage_inputs,
    }
    if manifest.get("resume_owned"):
        resume_audit = _verified_repair_audit(manifest, "resume_owned")
        result["archived_voice_add_evidence_attempts"] = resume_audit["prior_voice_attempts"]
    manifest["verification"] = result
    if (
        all(case["status"] == "PASS" for case in cases.values())
        and all(row["status"] == "PASS" for row in coverage.values())
        and not any(item["status"] == "FAIL" for items in coverage_inputs.values() for item in items)
        and not any(item.get("execution_status") == "FAIL" for item in coverage_inputs["test_reports"])
    ):
        manifest["state"] = "verified"
        result["state"] = "verified"
    else:
        manifest["state"] = "verification_incomplete"
        result["state"] = "verification_incomplete"
    _atomic_json(path / "evidence" / "verification-matrix.json", result)
    _atomic_json(path / "run-manifest.json", manifest)
    return result


async def _snapshot_async(run_dir: str | Path, session_id: str, *, memory_ids: list[str] = ()) -> dict[str, Any]:
    import asyncpg

    path, manifest, _selected, _selection, values = load_run(run_dir)
    try:
        explicit_memory_ids = {UUID(value) for value in memory_ids}
    except (ValueError, TypeError, AttributeError) as exc:
        raise ProbeError("snapshot explicit memory_id must be a UUID") from exc
    try:
        origin_id = UUID(session_id)
    except ValueError as exc:
        raise ProbeError("snapshot session_id must be a UUID") from exc
    target = manifest["runtime"]["target"]
    conn = await asyncpg.connect(
        _dsn(target["host"], target["port"], target["database"], target["user"], values["POSTGRES_PASSWORD"]),
        timeout=5,
    )
    try:
        await conn.set_type_codec("jsonb", schema="pg_catalog", encoder=json.dumps, decoder=json.loads)
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            identity = await conn.fetchrow(
                "SELECT current_database() AS database,(SELECT system_identifier::text FROM pg_control_system()) AS system_identifier"
            )
            if identity["database"] != manifest["runtime"]["database"] or identity["system_identifier"] != manifest["service_identities"]["target"]["system_identifier"]:
                raise ProbeError("snapshot connection does not match the owned run-01 target database")
            if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM chat_sessions WHERE id=$1::uuid)", origin_id):
                raise ProbeError("snapshot session does not exist in the owned target database")
            tasks = [dict(row) for row in await conn.fetch(
                "SELECT id::text,task_session_id::text,product_state,origin_session_id::text FROM m5_tasks WHERE origin_session_id=$1::uuid ORDER BY created_at",
                origin_id,
            )]
            task_sessions = [UUID(task["task_session_id"]) for task in tasks]
            chat_messages = [dict(row) for row in await conn.fetch(
                "SELECT id::text,session_id::text,ordinal,role,content,content_blocks,source_message_id,metadata "
                "FROM chat_messages WHERE session_id=$1::uuid OR session_id=ANY($2::uuid[]) ORDER BY session_id,ordinal",
                origin_id, task_sessions,
            )]
            events = [dict(row) for row in await conn.fetch(
                "SELECT event_id::text,session_id::text,stream_id,seq,event_type,payload,ts,recorded_at,task_id "
                "FROM behavior_events WHERE session_id=$1::uuid OR session_id=ANY($2::uuid[]) ORDER BY ts,seq",
                origin_id, task_sessions,
            )]
            actions = [dict(row) for row in await conn.fetch(
                "SELECT command_id::text,actual_call_id,tool_name,state,arguments,result,result->'error' AS error,origin_session_id::text,created_at,updated_at "
                "FROM m5_actions WHERE origin_session_id=$1::uuid ORDER BY created_at",
                origin_id,
            )]
            work = [dict(row) for row in await conn.fetch(
                "SELECT w.id::text,w.task_id::text,w.id::text AS execution_id,w.status,w.started_at,w.finished_at AS completed_at "
                "FROM m5_work_rounds w JOIN m5_tasks t ON t.id=w.task_id WHERE t.origin_session_id=$1::uuid ORDER BY w.started_at",
                origin_id,
            )]
            memories: list[dict[str, Any]] = []
            memory_ids = sorted(explicit_memory_ids | {memory_id for action in actions if (memory_id := _done_receipt_memory_uuid(action)) is not None})
            if memory_ids:
                memories = [dict(row) for row in await conn.fetch(
                    "SELECT id::text,type,status,content,metadata,created_at,updated_at FROM memories WHERE id=ANY($1::uuid[]) ORDER BY created_at",
                    memory_ids,
                )]
                if {UUID(str(row["id"])) for row in memories} != set(memory_ids):
                    raise ProbeError("done receipt memory UUID does not resolve to the real target DB memory row")
            evidence = [dict(row) for row in await conn.fetch(
                "SELECT memory_id::text,evidence,prior_confidence,posterior_confidence,created_at "
                "FROM belief_revision_audit WHERE memory_id=ANY($1::uuid[]) ORDER BY created_at",
                memory_ids,
            )]
        return json.loads(json.dumps({
            "run_id": manifest["run_id"],
            "database": identity["database"],
            "system_identifier": identity["system_identifier"],
            "origin_session_id": str(origin_id),
            "chat_messages": chat_messages,
            "tasks": tasks,
            "work": work,
            "actions": actions,
            "events": events,
            "memories": memories,
            "belief_revision_audit": evidence,
        }, default=str))
    finally:
        await conn.close()


def snapshot(run_dir: str | Path, session_id: str, *, memory_ids: list[str] = ()) -> dict[str, Any]:
    return asyncio.run(_snapshot_async(run_dir, session_id, memory_ids=memory_ids))


async def _refresh_launcher_fingerprints_async(
    run_dir: str | Path, *, source_files: list[str] = (), reason: str | None = None,
) -> dict[str, Any]:
    import asyncpg

    path, manifest, selected, _selection, values = load_run(run_dir)
    old = dict(manifest.get("source_fingerprints", {}))
    explicit = list(dict.fromkeys(source_files))
    if explicit and (not isinstance(reason, str) or not reason.strip()):
        raise ProbeError("explicit product source refresh requires a recorded reason")
    if reason and any(secret and secret in reason for key, secret in values.items() if key in SECRET_NAMES):
        raise ProbeError("source refresh reason must not contain selected credentials")
    for relative in explicit:
        source = Path(relative)
        if source.is_absolute() or ".." in source.parts or str(source) != relative or relative not in old or not (ROOT / source).resolve().is_relative_to(ROOT.resolve()):
            raise ProbeError("explicit source refresh only accepts existing manifest-relative source files")
    profile = selected.get("selected_profile", manifest.get("consent", {}))
    failed_first_attempt = manifest.get("state") == "consent_error"
    if failed_first_attempt:
        _first_failed_consent_summary(path, manifest, profile, values)
        retry_sources = {"apps/bob108_tool_activity_probe.py", "hexis-ui/e2e/tool-activity.spec.ts", "hexis-ui/app/chat/page.tsx"}
        if any(relative not in retry_sources for relative in explicit):
            raise ProbeError("post-error source refresh is limited to the authorised probe/page/E2E sources")
    elif manifest.get("state") != "awaiting_consent" or manifest.get("consent", {}).get("state") != "required":
        raise ProbeError("launcher fingerprints may only be refreshed before consent and service startup")
    if manifest.get("api_process") is not None or manifest.get("ui_process") is not None:
        raise ProbeError("launcher fingerprints cannot be refreshed after an API or UI process was recorded")
    evidence_dir = path / "evidence"
    if (
        (evidence_dir / "api.log").exists()
        or (evidence_dir / "ui.log").exists()
        or (evidence_dir / "api-start.json").exists()
        or (evidence_dir / "ui-start.json").exists()
        or any((evidence_dir / "cases").glob("*.json"))
    ):
        raise ProbeError("launcher fingerprints cannot be refreshed after API/UI execution or case evidence")
    target = manifest["runtime"]["target"]
    conn = await asyncpg.connect(
        _dsn(target["host"], target["port"], target["database"], target["user"], values["POSTGRES_PASSWORD"]),
        timeout=5,
    )
    try:
        identity = await conn.fetchrow(
            "SELECT current_database() AS database,(SELECT system_identifier::text FROM pg_control_system()) AS system_identifier"
        )
        if identity["database"] != target["database"] or identity["system_identifier"] != manifest["service_identities"]["target"]["system_identifier"]:
            raise ProbeError("fingerprint refresh did not reach this run's owned target PostgreSQL database")
        if await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM consent_log WHERE (provider=$1 AND model=$2 AND endpoint=$3) OR decision='consent')",
            profile["provider"], profile["model"], profile["endpoint"],
        ):
            raise ProbeError("target consent already exists; launcher refresh must precede the real consent request")
    finally:
        await conn.close()
    launcher_paths = ("apps/bob108_tool_activity_probe.py", "hexis-ui/e2e/tool-activity.spec.ts")
    refreshed_paths = tuple(dict.fromkeys((*launcher_paths, *explicit)))
    for relative, expected in old.items():
        if relative in refreshed_paths:
            continue
        source_path = ROOT / relative
        if not source_path.is_file() or _digest(source_path) != expected:
            raise ProbeError(f"product source fingerprint changed before launcher refresh: {relative}")
    current: dict[str, str] = {}
    for relative in refreshed_paths:
        source_path = ROOT / relative
        if not source_path.is_file():
            raise ProbeError(f"required run launcher source is missing: {relative}")
        current[relative] = _digest(source_path)
    first_attempt = _preserve_first_consent_attempt(path, manifest, profile, values) if failed_first_attempt else None
    manifest.setdefault("source_fingerprints", {}).update(current)
    record = {
        "run_id": manifest["run_id"],
        "refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "reason": reason.strip() if explicit else "真实模型同意请求与服务启动前刷新 Sprint 4 探针及可见 Playwright 验收入口的源码指纹；其余产品源码指纹保持一致。",
        "explicit_source_files": explicit,
        "previous": {relative: old.get(relative) for relative in refreshed_paths},
        "current": current,
        "unchanged_product_fingerprints": sorted(relative for relative in old if relative not in refreshed_paths),
        "provider_requested": False,
        "consent_written": False,
        "api_or_ui_started": False,
    }
    if first_attempt is not None:
        record["first_attempt"] = first_attempt
        record["historical_attempt_preserved"] = True
    output = evidence_dir / "launcher-fingerprint-refresh.json"
    history: list[dict[str, Any]] = []
    if output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        history = existing.get("history", [existing]) if isinstance(existing, dict) else []
    if history:
        record["historical_record_note"] = "本条中文刷新记录补充既有历史记录；历史记录保持原样，未改写或删除。"
    history.append(record)
    _atomic_json(output, {"run_id": manifest["run_id"], "history": history})
    _atomic_json(path / "run-manifest.json", manifest)
    return record


def refresh_launcher_fingerprints(
    run_dir: str | Path, *, source_files: list[str] = (), reason: str | None = None,
) -> dict[str, Any]:
    return asyncio.run(_refresh_launcher_fingerprints_async(run_dir, source_files=source_files, reason=reason))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare-real", help="创建全新隔离 run-01 服务、目标库和非 secret manifest")
    prepare.add_argument("--selection", required=True)
    prepare.add_argument("--background-profile", required=True)
    prepare.add_argument("--run-dir", required=True)
    for name in ("request-consent", "retry-consent", "serve-api", "serve-ui", "check-readiness", "stop-owned"):
        commands.add_parser(name).add_argument("--run-dir", required=True)
    refresh = commands.add_parser("refresh-launcher-fingerprints")
    refresh.add_argument("--run-dir", required=True)
    refresh.add_argument("--source-file", action="append", default=[])
    refresh.add_argument("--reason")
    repair = commands.add_parser("repair-ready-launcher")
    repair.add_argument("--run-dir", required=True)
    repair.add_argument("--reason", required=True)
    e2e_repair = commands.add_parser("repair-ready-e2e")
    e2e_repair.add_argument("--run-dir", required=True)
    e2e_repair.add_argument("--reason", required=True)
    e2e_repair.add_argument("--expected-e2e-sha256", required=True)
    e2e_repair.add_argument("--interrupted-trace", required=True)
    harness_retry = commands.add_parser("repair-e2e-harness-retry")
    harness_retry.add_argument("--run-dir", required=True)
    harness_retry.add_argument("--reason", required=True)
    harness_retry.add_argument("--expected-e2e-sha256", required=True)
    harness_retry.add_argument("--expected-startup-sha256", required=True)
    harness_retry.add_argument("--interrupted-trace", required=True)
    harness_retry.add_argument("--session-id", required=True)
    case_refresh = commands.add_parser("repair-e2e-case-refresh")
    case_refresh.add_argument("--run-dir", required=True)
    case_refresh.add_argument("--reason", required=True)
    case_refresh.add_argument("--expected-e2e-sha256", required=True)
    case_refresh.add_argument("--expected-startup-sha256", required=True)
    case_refresh.add_argument("--interrupted-trace", required=True)
    case_refresh.add_argument("--session-id", required=True)
    replay = commands.add_parser("repair-evidence-replay")
    replay.add_argument("--run-dir", required=True)
    replay.add_argument("--reason", required=True)
    replay.add_argument("--expected-e2e-sha256", required=True)
    replay.add_argument("--expected-startup-sha256", required=True)
    replay.add_argument("--interrupted-trace", required=True)
    replay.add_argument("--origin-session-id", required=True)
    replay.add_argument("--task-id", required=True)
    replay.add_argument("--task-session-id", required=True)
    replay.add_argument("--memory-id", required=True)
    final_refresh = commands.add_parser("refresh-final-verification")
    final_refresh.add_argument("--run-dir", required=True)
    final_refresh.add_argument("--reason", required=True)
    final_refresh.add_argument("--expected-probe-sha256", required=True)
    final_refresh.add_argument("--expected-probe-tests-sha256", required=True)
    final_refresh.add_argument("--expected-e2e-sha256", required=True)
    final_refresh.add_argument("--expected-startup-sha256", required=True)
    final_refresh.add_argument("--trace-path", required=True)
    resume = commands.add_parser("resume-owned")
    resume.add_argument("--run-dir", required=True)
    resume.add_argument("--reason", required=True)
    resume.add_argument("--source-file", action="append", default=[])
    resume.add_argument("--expected-e2e-sha256", required=True)
    resume.add_argument("--expected-startup-sha256", required=True)
    resume.add_argument("--retry-failed-once", action="store_true")
    verify = commands.add_parser("verify")
    verify.add_argument("--run-dir", required=True)
    verify.add_argument("--coverage-run-dir", action="append", default=[])
    verify.add_argument("--coverage-test-report", action="append", default=[])
    snapshot_parser = commands.add_parser("snapshot", help="只读回读本轮 session/task/Activity/receipt/memory 状态")
    snapshot_parser.add_argument("--run-dir", required=True)
    snapshot_parser.add_argument("--session-id", required=True)
    snapshot_parser.add_argument("--memory-id", action="append", default=[])
    bind = commands.add_parser("bind-evidence-context")
    bind.add_argument("--run-dir", required=True)
    bind.add_argument("--origin-session-id", required=True)
    tests = commands.add_parser("pytest")
    tests.add_argument("--run-dir", required=True)
    tests.add_argument("pytest_args", nargs=argparse.REMAINDER)
    api_child = commands.add_parser("_api-child", help=argparse.SUPPRESS)
    api_child.add_argument("--run-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare-real":
            result = prepare_real(args.selection, args.background_profile, args.run_dir)
        elif args.command == "request-consent":
            result = request_consent(args.run_dir)
        elif args.command == "retry-consent":
            result = retry_consent(args.run_dir)
        elif args.command == "serve-api":
            result = serve_api(args.run_dir)
        elif args.command == "serve-ui":
            result = serve_ui(args.run_dir)
        elif args.command == "check-readiness":
            result = check_readiness(args.run_dir)
        elif args.command == "pytest":
            return pytest_command(args.run_dir, args.pytest_args)
        elif args.command == "bind-evidence-context":
            result = bind_evidence_context(args.run_dir, args.origin_session_id)
        elif args.command == "verify":
            result = verify_run(args.run_dir, coverage_run_dirs=args.coverage_run_dir, coverage_test_reports=args.coverage_test_report)
        elif args.command == "stop-owned":
            result = stop_owned(args.run_dir)
        elif args.command == "snapshot":
            result = snapshot(args.run_dir, args.session_id, memory_ids=args.memory_id)
        elif args.command == "refresh-launcher-fingerprints":
            result = refresh_launcher_fingerprints(args.run_dir, source_files=args.source_file, reason=args.reason)
        elif args.command == "repair-ready-launcher":
            result = repair_ready_launcher(args.run_dir, reason=args.reason)
        elif args.command == "repair-ready-e2e":
            result = repair_ready_e2e(args.run_dir, reason=args.reason, expected_e2e_sha256=args.expected_e2e_sha256, interrupted_trace=args.interrupted_trace)
        elif args.command == "repair-e2e-harness-retry":
            result = repair_e2e_harness_retry(args.run_dir, reason=args.reason, expected_e2e_sha256=args.expected_e2e_sha256, expected_startup_sha256=args.expected_startup_sha256, interrupted_trace=args.interrupted_trace, session_id=args.session_id)
        elif args.command == "repair-e2e-case-refresh":
            result = repair_e2e_case_refresh(args.run_dir, reason=args.reason, expected_e2e_sha256=args.expected_e2e_sha256, expected_startup_sha256=args.expected_startup_sha256, interrupted_trace=args.interrupted_trace, session_id=args.session_id)
        elif args.command == "repair-evidence-replay":
            result = repair_evidence_replay(args.run_dir, reason=args.reason, expected_e2e_sha256=args.expected_e2e_sha256, expected_startup_sha256=args.expected_startup_sha256, interrupted_trace=args.interrupted_trace, origin_session_id=args.origin_session_id, task_id=args.task_id, task_session_id=args.task_session_id, memory_id=args.memory_id)
        elif args.command == "refresh-final-verification":
            result = refresh_final_verification(args.run_dir, reason=args.reason, expected_source_hashes=dict(zip(FINAL_VERIFICATION_SOURCE_PATHS, (args.expected_probe_sha256, args.expected_probe_tests_sha256, args.expected_e2e_sha256, args.expected_startup_sha256))), trace_path=args.trace_path)
        elif args.command == "resume-owned":
            result = resume_owned(args.run_dir, reason=args.reason, source_files=args.source_file, expected_e2e_sha256=args.expected_e2e_sha256, expected_startup_sha256=args.expected_startup_sha256, retry_failed_once=args.retry_failed_once)
        elif args.command == "_api-child":
            _api_child(args.run_dir)
            return 0
        else:
            raise ProbeError("unsupported run-01 command")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except ProbeError as exc:
        print(str(exc))
        return 1
    except Exception as exc:
        print(f"run-01 {args.command} failed ({type(exc).__name__}); inspect the preserved run manifest and evidence log")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
