"""BOB-108 隔离测试环境控制器。

该入口只服务于本轮受控验收。它不读取应用的 ambient DB URL；pytest 子进程只
接收本轮 owned PostgreSQL 与 fake-embedding 配置。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote


REPO_ROOT = Path(__file__).resolve().parents[1]
CACHE_ROOT = Path.home() / ".cache" / "hexis" / "bob108-20261009"
MATERIAL_ROOT = CACHE_ROOT
MOCK_DATA = MATERIAL_ROOT / "mock-data.json"
MOCK_SHA256 = "3cb6ab4dc0519f4d7571d1c30b2ac95523f5b5f17e6051e4f873ac7fc3bb185f"
DEFAULT_PORTS = {"pytest": 45816, "browser": 45815, "embeddings": 45435, "api": 44819, "provider": 44818, "ui": 3577}


def validate_run_dir(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("run-dir must be an absolute cache path")
    resolved = path.resolve()
    allowed = CACHE_ROOT.resolve()
    if resolved != allowed and allowed not in resolved.parents:
        raise ValueError(f"run-dir must be inside {allowed}")
    return resolved


def validate_repo_environment(*, repo_root: Path, run_dir: Path) -> None:
    if (run_dir / ".env").exists():
        raise ValueError("unexpected .env inside run directory")
    if repo_root.resolve() == run_dir.resolve() or repo_root.resolve() in run_dir.resolve().parents:
        raise ValueError("run directory must be outside the repository")


def port_is_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def require_available_ports(ports: list[int]) -> None:
    occupied = [port for port in ports if not port_is_available(port)]
    if occupied:
        raise RuntimeError(f"port(s) unavailable: {', '.join(map(str, occupied))}")


def control_volume_names(project: str) -> dict[str, str]:
    return {"pytest": f"{project}-pytest-brain-pgdata", "browser": f"{project}-browser-brain-pgdata"}


def validate_control_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("run_id") != "control-01":
        raise ValueError("run_id does not identify the owned control run")
    if manifest.get("project") != "hexis-bob108-control":
        raise ValueError("compose project does not identify the owned control run")
    if manifest.get("source_root") != str(REPO_ROOT):
        raise ValueError("source root does not match the current repository")
    volumes = manifest.get("volumes")
    if not isinstance(volumes, dict) or not all(
        isinstance(volumes.get(key), str) and volumes[key] for key in ("pytest", "browser")
    ):
        raise ValueError("control manifest has invalid volume names")
    if volumes["pytest"] == volumes["browser"]:
        raise ValueError("pytest and browser PostgreSQL volume names must be distinct")
    ports = manifest.get("ports")
    if not isinstance(ports, dict):
        raise ValueError("control manifest has invalid ports")
    required = ("pytest", "browser", "embeddings")
    selected = [ports.get(key) for key in required]
    selected.extend(value for key, value in ports.items() if key not in required)
    if any(not isinstance(port, int) or not 1024 <= port <= 65535 for port in selected):
        raise ValueError("control manifest has invalid service ports")
    if len(set(selected)) != len(selected):
        raise ValueError("control manifest has duplicate service ports")


def validate_service_identities(identities: dict[str, Any]) -> None:
    expected = {"pytest": "bob108_pytest", "browser": "bob108_browser"}
    system_ids: list[str] = []
    for service, database in expected.items():
        identity = identities.get(service)
        if not isinstance(identity, dict) or identity.get("database") != database:
            raise RuntimeError(f"{service} PostgreSQL database identity mismatch")
        system_id = identity.get("system_identifier")
        if not system_id:
            raise RuntimeError(f"{service} PostgreSQL system identity is missing")
        system_ids.append(str(system_id))
    if len(set(system_ids)) != len(system_ids):
        raise RuntimeError("pytest and browser PostgreSQL services share a system identifier")


def validate_embedding_health(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise RuntimeError("fake embedding health response is invalid")


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def build_pytest_environment(*, runtime_env: dict[str, Any], password: str) -> dict[str, str]:
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP") if key in os.environ}
    env.update(
        {
            "POSTGRES_HOST": str(runtime_env["pytest_host"]),
            "POSTGRES_PORT": str(runtime_env["pytest_port"]),
            "POSTGRES_USER": str(runtime_env["pytest_user"]),
            "POSTGRES_PASSWORD": password,
            "POSTGRES_DB": str(runtime_env["pytest_db"]),
            "POSTGRES_ADMIN_DB": str(runtime_env["pytest_admin_db"]),
            "FAKE_EMBEDDINGS_HOST": "127.0.0.1",
            "FAKE_EMBEDDINGS_PORT": str(runtime_env["embedding_port"]),
            "EMBEDDING_DIMENSION": str(runtime_env["embedding_dimension"]),
            "EMBEDDING_SERVICE_URL": str(runtime_env["fake_embeddings_url"]),
            "EMBEDDING_MODEL_ID": str(runtime_env["embedding_model_id"]),
            "PYTHONPATH": str(REPO_ROOT),
        }
    )
    return env


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(mode)
    temporary.replace(path)


def _repository_postgres_config() -> dict[str, Any]:
    """读取仓库 DB 镜像配置，避免加载 ambient env 或把业务服务带入隔离栈。"""
    compose_file = REPO_ROOT / "docker-compose.yml"
    if not compose_file.is_file():
        raise FileNotFoundError(f"missing repository PostgreSQL compose config: {compose_file}")
    completed = subprocess.run(
        ["docker", "compose", "--env-file", os.devnull, "-f", str(compose_file), "config", "--format", "json"],
        cwd=REPO_ROOT,
        env={key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ},
        check=True,
        capture_output=True,
        text=True,
    )
    service = json.loads(completed.stdout).get("services", {}).get("db", {})
    image = service.get("image")
    build = service.get("build")
    if not isinstance(image, str) or not image or not isinstance(build, dict):
        raise ValueError("repository PostgreSQL service requires an image and build config")
    context = build.get("context")
    dockerfile = build.get("dockerfile")
    if not isinstance(context, str) or Path(context).resolve() != REPO_ROOT:
        raise ValueError("repository PostgreSQL build context differs from the current source root")
    if not isinstance(dockerfile, str) or not (REPO_ROOT / dockerfile).is_file():
        raise FileNotFoundError("repository PostgreSQL Dockerfile is missing")
    return {"image": image, "context": context, "dockerfile": dockerfile}


def _compose_text(project: str, ports: dict[str, int], volumes: dict[str, str]) -> str:
    database = _repository_postgres_config()
    database_image = json.dumps(database["image"])
    build_context = json.dumps(database["context"])
    dockerfile = json.dumps(database["dockerfile"])
    return f'''name: {project}
services:
  pytest-db:
    image: {database_image}
    build:
      context: {build_context}
      dockerfile: {dockerfile}
    environment:
      POSTGRES_USER: bob108
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
      hexis.bob108.run_id: control-01
  browser-db:
    image: {database_image}
    build:
      context: {build_context}
      dockerfile: {dockerfile}
    environment:
      POSTGRES_USER: bob108
      POSTGRES_PASSWORD: ${{POSTGRES_PASSWORD}}
      POSTGRES_DB: bob108_browser
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
      hexis.bob108.run_id: control-01
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
      - "{REPO_ROOT / 'ops/ci/fake_embeddings.py'}:/app/fake_embeddings.py:ro"
    labels:
      hexis.bob108.run_id: control-01
volumes:
  {volumes['pytest']}:
    labels:
      hexis.bob108.run_id: control-01
  {volumes['browser']}:
    labels:
      hexis.bob108.run_id: control-01
'''


def _config_selection() -> dict[str, Any]:
    path = MATERIAL_ROOT / "config-selection.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _current_source_fingerprints() -> dict[str, str]:
    manifest = json.loads((MATERIAL_ROOT / "materials-manifest.json").read_text(encoding="utf-8"))
    return {
        relative: _sha256(REPO_ROOT / relative)
        for relative in sorted(manifest.get("source_fingerprints", {}))
    }


def prepare_control(run_dir: Path) -> dict[str, Any]:
    validate_repo_environment(repo_root=REPO_ROOT, run_dir=run_dir)
    superseded_volumes: list[str] = []
    if run_dir.exists() and any(run_dir.iterdir()):
        previous = _load_manifest(run_dir)
        validate_control_manifest(previous)
        for role, process in previous.get("controlled_processes", {}).items():
            if _controlled_process_is_alive(process, run_dir=run_dir, role=role):
                raise FileExistsError(f"refusing to overwrite control files while the owned {role} process is running")
        superseded_volumes = list(previous.get("superseded_volumes", []))
        for old_volume in previous.get("volumes", {}).values():
            if old_volume not in superseded_volumes:
                superseded_volumes.append(old_volume)
        credentials_path = Path(_config_selection()["credential_env_file"])
        inspect = subprocess.run(
            [
                "docker",
                "compose",
                "--project-name",
                previous["project"],
                "--env-file",
                str(credentials_path),
                "-f",
                str(run_dir / "compose.yml"),
                "ps",
                "-q",
                "--status",
                "running",
            ],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        if inspect.stdout.strip():
            raise FileExistsError("refusing to overwrite control files while owned containers exist")
    source = MOCK_DATA
    if _sha256(source) != MOCK_SHA256:
        raise ValueError("mock-data.json SHA-256 does not match the execution plan")
    project = "hexis-bob108-control"
    volumes = control_volume_names(project)
    superseded_volumes = [name for name in superseded_volumes if name not in volumes.values()]
    ports = dict(DEFAULT_PORTS)
    config = _config_selection()
    credentials_path = Path(config["credential_env_file"]).expanduser().resolve()
    if credentials_path != REPO_ROOT / ".env":
        raise ValueError("credential source differs from the selected .env file")
    credentials = _read_dotenv(credentials_path)
    password = credentials.get("POSTGRES_PASSWORD")
    if not password:
        raise ValueError("selected credential source has no POSTGRES_PASSWORD")
    if not Path("/var/run/docker.sock").exists() and shutil.which("docker") is None:
        raise RuntimeError("Docker is unavailable")
    run_dir.mkdir(parents=True, exist_ok=True)
    fixture = REPO_ROOT / "tests/fixtures/tool_activity.json"
    if fixture.exists() and _sha256(fixture) != MOCK_SHA256:
        raise ValueError("existing repo fixture differs from the approved BOB-108 fixture")
    if not fixture.exists():
        fixture.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, fixture)
    if _sha256(fixture) != MOCK_SHA256:
        raise ValueError("repo fixture SHA-256 verification failed")

    env_values = {
        "POSTGRES_PASSWORD": password,
        "pytest_host": "127.0.0.1",
        "pytest_port": ports["pytest"],
        "pytest_user": "bob108",
        "pytest_db": "bob108_pytest",
        "pytest_admin_db": "postgres",
        "browser_host": "127.0.0.1",
        "browser_port": ports["browser"],
        "browser_user": "bob108",
        "browser_db": "bob108_browser",
        "browser_admin_db": "postgres",
        "embedding_host": "127.0.0.1",
        "embedding_port": ports["embeddings"],
        "embedding_dimension": 768,
        "fake_embeddings_url": f"http://127.0.0.1:{ports['embeddings']}/api/embed",
        "embedding_model_id": "bob108-fake-768",
        "api_url": f"http://127.0.0.1:{ports['api']}",
        "ui_url": f"http://127.0.0.1:{ports['ui']}",
        "provider_url": f"http://127.0.0.1:{ports['provider']}",
        "provider_port": ports["provider"],
    }
    runtime_env = {key: value for key, value in env_values.items() if key != "POSTGRES_PASSWORD"}
    runtime_env["credential_env_file"] = str(credentials_path)
    runtime_env["credential_password_env"] = "POSTGRES_PASSWORD"
    _atomic_write(run_dir / "runtime.env", json.dumps(runtime_env, ensure_ascii=False, indent=2) + "\n")
    (run_dir / "evidence").mkdir(exist_ok=True)
    _atomic_write(run_dir / "compose.yml", _compose_text(project, ports, volumes), mode=0o644)
    manifest = {
        "run_id": "control-01",
        "project": project,
        "compose_file": str(run_dir / "compose.yml"),
        "ports": ports,
        "volumes": volumes,
        "superseded_volumes": superseded_volumes,
        "superseded_physical_volumes": [f"{project}_{name}" for name in superseded_volumes],
        "services": ["pytest-db", "browser-db", "fake-embeddings"],
        "fixture": {"path": str(fixture), "sha256": MOCK_SHA256},
        "source_root": str(REPO_ROOT),
        "head": _git_head(),
        "source_fingerprints": _current_source_fingerprints(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runtime": {key: value for key, value in env_values.items() if key != "POSTGRES_PASSWORD"},
        "service_identities": {},
        "ready": False,
    }
    _atomic_write(run_dir / "run-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def _git_head() -> str:
    completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _process_command(pid: int) -> str | None:
    if not isinstance(pid, int) or pid <= 0:
        return None
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    command = result.stdout.strip()
    return command or None


def _controlled_process_is_alive(process: dict[str, Any], *, run_dir: Path, role: str) -> bool:
    pid = process.get("pid")
    command = _process_command(pid) if isinstance(pid, int) else None
    if not command:
        return False
    if process.get("run_id") != "control-01":
        raise RuntimeError(f"{role} process record has an unexpected run identity")
    arguments = shlex.split(command)
    if role == "api_provider":
        expected_module = ["-m", "tests.fixtures.bob108_controlled_runtime"]
        expected_run = ["--run-dir", str(run_dir)]
        matches = all(
            any(arguments[index:index + 2] == expected for index in range(len(arguments) - 1))
            for expected in (expected_module, expected_run)
        )
    else:
        matches = str(run_dir / "ui-src/node_modules/next/dist/bin/next") in arguments
    if not matches:
        raise RuntimeError(f"refusing to manage {role} PID {pid}; its command does not match this run")
    return True


def _stop_controlled_process(process: dict[str, Any], *, run_dir: Path, role: str) -> None:
    if not _controlled_process_is_alive(process, run_dir=run_dir, role=role):
        return
    pid = int(process["pid"])
    os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _process_command(pid) is None:
            return
        time.sleep(0.2)
    raise RuntimeError(f"owned {role} process {pid} did not stop after SIGTERM")


def _load_manifest(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "run-manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"missing run manifest: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


async def _pg_identity(host: str, port: int, database: str, user: str, password: str) -> dict[str, Any]:
    import asyncpg

    conn = await asyncpg.connect(host=host, port=port, database=database, user=user, password=password, timeout=5)
    try:
        return dict(await conn.fetchrow("SELECT current_database() AS database, inet_server_addr()::text AS address, inet_server_port() AS port, (SELECT system_identifier FROM pg_control_system())::text AS system_identifier"))
    finally:
        await conn.close()


async def bootstrap_controlled_database(manifest: dict[str, Any], password: str) -> dict[str, Any]:
    """Bind the owned browser DB to the local scripted provider and test consent."""
    import asyncpg

    runtime = manifest["runtime"]
    conn = await asyncpg.connect(
        host=runtime["browser_host"],
        port=runtime["browser_port"],
        database=runtime["browser_db"],
        user=runtime["browser_user"],
        password=password,
        timeout=5,
    )
    try:
        identity = await conn.fetchrow(
            "SELECT current_database() AS database, (SELECT system_identifier FROM pg_control_system())::text AS system_identifier"
        )
        expected = manifest.get("service_identities", {}).get("browser", {})
        if identity["database"] != runtime["browser_db"] or identity["system_identifier"] != expected.get("system_identifier"):
            raise RuntimeError("refusing to configure a browser database with an unexpected identity")

        profile = {
            "provider": "openai-chat-completions-endpoint",
            "model": "bob108-scripted",
            "endpoint": f"http://127.0.0.1:{manifest['ports']['provider']}/v1",
            "api_key_env": "BOB108_SCRIPTED_PROVIDER_KEY",
        }
        encoded_profile = json.dumps(profile, ensure_ascii=False)
        await conn.fetchval("SELECT set_config('agent.is_configured', 'true'::jsonb)")
        await conn.fetchval(
            "SELECT set_config('agent.objectives', $1::jsonb)",
            json.dumps(["BOB-108 controlled tool activity verification"]),
        )
        await conn.fetchval("SELECT set_config('agent.name', $1::jsonb)", json.dumps("BOB-108 Controlled Agent"))
        for key in ("llm.chat", "llm.heartbeat", "llm.subconscious"):
            await conn.fetchval("SELECT set_config($1, $2::jsonb)", key, encoded_profile)
        await conn.execute("UPDATE heartbeat_state SET is_paused = TRUE WHERE id = 1")
        consent_exists = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM consent_log)")
        if not consent_exists:
            consent = await conn.fetchval(
                "SELECT record_consent_response($1::jsonb)",
                json.dumps({"decision": "consent", "signature": "bob108-scripted-test-provider", "memories": []}),
            )
            if not consent:
                raise RuntimeError("test-only local-provider consent could not be initialized")
        return {
            "database": identity["database"],
            "system_identifier": identity["system_identifier"],
            "configured": True,
            "provider": profile["provider"],
            "model": profile["model"],
            "endpoint": profile["endpoint"],
            "api_key_env": profile["api_key_env"],
            "consent_source": "owned-browser-db-test-fixture",
        }
    finally:
        await conn.close()


def _controlled_environment(manifest: dict[str, Any], password: str, *, run_dir: Path) -> dict[str, str]:
    runtime = manifest["runtime"]
    home = run_dir / "runtime-home"
    if home.is_symlink() or home.resolve().parent != run_dir.resolve():
        raise RuntimeError("controlled home must remain inside the owned run directory")
    if (home / ".hexis" / "instances.json").exists():
        raise RuntimeError("controlled home must not contain an instance registry")
    home.mkdir(parents=True, exist_ok=True)
    api_port = manifest["ports"]["api"]
    ui_port = manifest["ports"]["ui"]
    provider_port = manifest["ports"]["provider"]
    database_url = (
        f"postgresql://{quote(str(runtime['browser_user']), safe='')}:{quote(password, safe='')}"
        f"@{runtime['browser_host']}:{runtime['browser_port']}/{runtime['browser_db']}"
    )
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "TEMP", "TMP") if key in os.environ}
    env.update(
        {
            "HOME": str(home),
            "PYTHONPATH": str(REPO_ROOT),
            "POSTGRES_HOST": str(runtime["browser_host"]),
            "POSTGRES_PORT": str(runtime["browser_port"]),
            "POSTGRES_USER": str(runtime["browser_user"]),
            "POSTGRES_PASSWORD": password,
            "POSTGRES_DB": str(runtime["browser_db"]),
            "POSTGRES_ADMIN_DB": "postgres",
            "HEXIS_DATABASE_URL": database_url,
            "HEXIS_API_URL": f"http://127.0.0.1:{api_port}",
            "HEXIS_BROWSER_API_URL": f"http://127.0.0.1:{api_port}",
            "HEXIS_UI_ORIGIN": f"http://127.0.0.1:{ui_port}",
            "HEXIS_CORS_ORIGINS": f"http://127.0.0.1:{ui_port}",
            "HEXIS_RUNTIME_IDENTITY_STATUS": "1",
            "HEXIS_REALTIME_CONFIG": str(REPO_ROOT / "config/qwen-realtime.json"),
            "HEXIS_M4_VOICE_CONFIG": str(REPO_ROOT / "config/m4-voice-runtime.json"),
            "BOB108_CONTROLLED_RUNTIME": "1",
            "BOB108_RUNTIME_DIAGNOSTICS": "1",
            "BOB108_RUN_ID": str(manifest["run_id"]),
            "BOB108_SCRIPTED_PROVIDER_URL": f"http://127.0.0.1:{provider_port}",
            "BOB108_SCRIPTED_PROVIDER_KEY": "bob108-loopback-test-provider",
            "BOB108_API_PORT": str(api_port),
            "BOB108_PROVIDER_PORT": str(provider_port),
            "BOB108_EXPECTED_DATABASE": str(runtime["browser_db"]),
            "BOB108_EXPECTED_SYSTEM_IDENTIFIER": str(
                manifest.get("service_identities", {}).get("browser", {}).get("system_identifier", "")
            ),
        }
    )
    return env


def _write_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    _atomic_write(run_dir / "run-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def _copy_controlled_ui(run_dir: Path) -> Path:
    source = REPO_ROOT / "hexis-ui"
    target = run_dir / "ui-src"
    if not source.is_dir() or not (source / "node_modules").is_dir():
        raise FileNotFoundError("the selected hexis-ui source or node_modules directory is missing")
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".next", "node_modules", ".turbo", "coverage", ".vercel", "playwright-report", "test-results"),
    )
    modules = target / "node_modules"
    if modules.exists() or modules.is_symlink():
        if not modules.is_symlink() or modules.resolve() != (source / "node_modules").resolve():
            raise RuntimeError("controlled UI node_modules is not bound to the selected repository dependencies")
    else:
        modules.symlink_to(source / "node_modules", target_is_directory=True)
    return target


def _controlled_ui_command(node: str, ui_root: Path, port: int) -> list[str]:
    # 复用仓库依赖的链接跨出缓存项目根目录，使用支持该布局的 webpack。
    return [node, str(ui_root / "node_modules/next/dist/bin/next"), "dev", "--webpack", "--hostname", "127.0.0.1", "--port", str(port)]


def _wait_http_json(url: str, *, method: str = "GET", timeout: float = 2.0) -> dict[str, Any]:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
        value = json.loads(raw) if raw else {}
        if not isinstance(value, dict):
            raise RuntimeError(f"readiness endpoint returned a non-object: {url}")
        return value


def serve_controlled(run_dir: Path) -> dict[str, Any]:
    manifest = _load_manifest(run_dir)
    validate_control_manifest(manifest)
    if not manifest.get("ready"):
        raise RuntimeError("owned control services are not ready")
    if manifest.get("controlled_processes"):
        raise RuntimeError("controlled API/UI process identities already exist; stop-owned before restarting them")
    require_available_ports([manifest["ports"]["api"], manifest["ports"]["provider"], manifest["ports"]["ui"]])
    config = _config_selection()
    credentials = _read_dotenv(Path(config["credential_env_file"]))
    password = credentials.get("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError("selected credential source has no POSTGRES_PASSWORD")
    profile = asyncio.run(bootstrap_controlled_database(manifest, password))
    _atomic_write(run_dir / "evidence" / "controlled-db-profile.json", json.dumps(profile, ensure_ascii=False, indent=2) + "\n")

    env = _controlled_environment(manifest, password, run_dir=run_dir)
    env["BOB108_API_PORT"] = str(manifest["ports"]["api"])
    env["BOB108_PROVIDER_PORT"] = str(manifest["ports"]["provider"])
    runtime_log = run_dir / "evidence" / "controlled-runtime.log"
    runtime_log.parent.mkdir(parents=True, exist_ok=True)
    with runtime_log.open("ab") as log:
        api_process = subprocess.Popen(
            [sys.executable, "-m", "tests.fixtures.bob108_controlled_runtime", "--run-dir", str(run_dir)],
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    ui_root = _copy_controlled_ui(run_dir)
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node.js is unavailable for the controlled UI runtime")
    ui_env = dict(env)
    ui_env["BOB108_RUNTIME_DIAGNOSTICS"] = "1"
    ui_env["BOB108_EXPECTED_DATABASE"] = str(manifest["runtime"]["browser_db"])
    ui_env["BOB108_EXPECTED_SYSTEM_IDENTIFIER"] = str(manifest["service_identities"]["browser"]["system_identifier"])
    ui_env["BOB108_SCRIPTED_PROVIDER_URL"] = f"http://127.0.0.1:{manifest['ports']['provider']}"
    ui_env["NEXT_TELEMETRY_DISABLED"] = "1"
    ui_log_path = run_dir / "evidence" / "controlled-ui.log"
    with ui_log_path.open("ab") as log:
        ui_process = subprocess.Popen(
            _controlled_ui_command(node, ui_root, manifest["ports"]["ui"]),
            cwd=ui_root,
            env=ui_env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    manifest["controlled_processes"] = {
        "api_provider": {"pid": api_process.pid, "run_id": manifest["run_id"], "log": str(runtime_log)},
        "ui": {"pid": ui_process.pid, "run_id": manifest["run_id"], "source_root": str(ui_root), "log": str(ui_log_path)},
    }
    _write_manifest(run_dir, manifest)

    api_identity: dict[str, Any] | None = None
    ui_identity: dict[str, Any] | None = None
    origin_session: dict[str, Any] | None = None
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if api_process.poll() is not None or ui_process.poll() is not None:
            raise RuntimeError("owned controlled API/provider or UI process exited during readiness; inspect evidence logs")
        try:
            api_identity = _wait_http_json(f"{manifest['runtime']['api_url']}/__test__/tool-activity/identity")
            if api_identity.get("run_id") != manifest["run_id"]:
                raise RuntimeError("controlled API run_id mismatch")
            database_identity = api_identity.get("database_identity", {})
            expected_browser = manifest["service_identities"]["browser"]
            if database_identity.get("database") != expected_browser["database"] or database_identity.get("system_identifier") != expected_browser["system_identifier"]:
                raise RuntimeError("controlled API actual database identity mismatch")
            if not api_identity.get("task_worker_ready") or not api_identity.get("lightweight_worker_ready"):
                raise RuntimeError("controlled API task/lightweight worker readiness is incomplete")

            ui_identity = _wait_http_json(f"{manifest['runtime']['ui_url']}/api/__test__/tool-activity/identity")
            if ui_identity.get("run_id") != manifest["run_id"]:
                raise RuntimeError("controlled UI run_id mismatch")
            ui_database = ui_identity.get("database_identity", {})
            if ui_database.get("database") != expected_browser["database"] or ui_database.get("system_identifier") != expected_browser["system_identifier"]:
                raise RuntimeError("controlled UI actual database identity mismatch")
            origin_session = _wait_http_json(f"{manifest['runtime']['ui_url']}/api/chat/session", method="POST")
            session_id = origin_session.get("session_id")
            if not isinstance(session_id, str):
                raise RuntimeError("controlled UI did not create an origin chat session")
            hydrated = _wait_http_json(f"{manifest['runtime']['ui_url']}/api/chat/session/{session_id}")
            if hydrated.get("session_id") != session_id or not isinstance(hydrated.get("messages"), list):
                raise RuntimeError("controlled UI cannot hydrate the owned origin session")
            scenario_url = manifest["runtime"]["ui_url"] + "/__test__/tool-activity"
            with urllib.request.urlopen(scenario_url, timeout=10) as response:
                if response.status != 200:
                    raise RuntimeError("controlled test scenario page did not return HTTP 200")
            manifest["origin_session_id"] = session_id
            manifest["api_start"] = api_identity
            manifest["ui_start"] = ui_identity
            manifest["controlled_runtime_ready"] = True
            _write_manifest(run_dir, manifest)
            _atomic_write(run_dir / "evidence" / "api-start.json", json.dumps(api_identity, ensure_ascii=False, indent=2) + "\n")
            _atomic_write(run_dir / "evidence" / "ui-start.json", json.dumps(ui_identity, ensure_ascii=False, indent=2) + "\n")
            _atomic_write(run_dir / "evidence" / "origin-session.json", json.dumps({"session_id": session_id}, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({"run_id": manifest["run_id"], "controlled_runtime_ready": True, "origin_session_id": session_id, "database": expected_browser["database"]}, ensure_ascii=False, indent=2))
            return manifest
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"controlled runtime readiness failed: {type(exc).__name__}; inspect {runtime_log} and {ui_log_path}") from exc
            time.sleep(1)
    raise RuntimeError("controlled runtime readiness timed out")


def _http_health(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"fake embedding health returned {response.status}")
        return json.loads(response.read().decode("utf-8"))


def _owned_resources(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    credential_path = Path(_config_selection()["credential_env_file"])
    compose = [
        "docker",
        "compose",
        "--project-name",
        manifest["project"],
        "--env-file",
        str(credential_path),
        "-f",
        str(run_dir / "compose.yml"),
    ]
    listed = subprocess.run(
        [*compose, "ps", "--status", "running", "-q"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    container_ids = [item for item in listed.stdout.splitlines() if item.strip()]
    if len(container_ids) != len(manifest["services"]):
        raise RuntimeError("owned control runtime does not have the expected running services")
    inspected = subprocess.run(
        ["docker", "inspect", *container_ids],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    values = json.loads(inspected.stdout)
    resources: dict[str, Any] = {}
    for container in values:
        labels = container.get("Config", {}).get("Labels") or {}
        service = labels.get("com.docker.compose.service")
        if (
            labels.get("com.docker.compose.project") != manifest["project"]
            or labels.get("hexis.bob108.run_id") != manifest["run_id"]
            or service not in manifest["services"]
            or container.get("State", {}).get("Status") != "running"
        ):
            raise RuntimeError("a running control container has an unexpected project, service, or run identity")
        mounts = [
            {"name": mount.get("Name"), "destination": mount.get("Destination")}
            for mount in container.get("Mounts", [])
            if mount.get("Type") == "volume"
        ]
        if service in {"pytest-db", "browser-db"}:
            expected_logical = manifest["volumes"]["pytest" if service == "pytest-db" else "browser"]
            expected_physical = f"{manifest['project']}_{expected_logical}"
            if not any(
                mount["name"] == expected_physical
                and mount["destination"] == "/var/lib/postgresql/data"
                for mount in mounts
            ):
                raise RuntimeError(f"{service} is not mounted on its manifest-owned volume")
        resources[service] = {
            "container_id": container.get("Id"),
            "image_ref": container.get("Config", {}).get("Image"),
            "image_id": container.get("Image"),
            "project": labels.get("com.docker.compose.project"),
            "run_id": labels.get("hexis.bob108.run_id"),
            "mounts": mounts,
        }
    if set(resources) != set(manifest["services"]):
        raise RuntimeError("the owned control service set is incomplete")
    return resources


def up_control(run_dir: Path) -> dict[str, Any]:
    manifest = _load_manifest(run_dir)
    validate_control_manifest(manifest)
    require_available_ports(list(manifest["ports"].values()))
    credential_path = Path(_config_selection()["credential_env_file"])
    password = _read_dotenv(credential_path).get("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError("selected credential source has no POSTGRES_PASSWORD")
    command = ["docker", "compose", "--project-name", manifest["project"], "--env-file", str(credential_path), "-f", str(run_dir / "compose.yml"), "up", "-d"]
    subprocess.run(command, cwd=REPO_ROOT, check=True)
    ports = manifest["ports"]
    runtime = manifest["runtime"]
    deadline = time.monotonic() + 90
    identities: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            identities["pytest"] = asyncio.run(_pg_identity("127.0.0.1", ports["pytest"], "bob108_pytest", "bob108", password))
            identities["browser"] = asyncio.run(_pg_identity("127.0.0.1", ports["browser"], "bob108_browser", "bob108", password))
            health = _http_health(f"http://127.0.0.1:{ports['embeddings']}/api/tags")
            validate_embedding_health(health)
            break
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"owned control services failed readiness: {type(exc).__name__}") from exc
            time.sleep(1)
    validate_service_identities(identities)
    resources = _owned_resources(run_dir, manifest)
    manifest["service_identities"] = identities
    manifest["fake_embeddings_health"] = health
    manifest["owned_resources"] = resources
    manifest["ready"] = True
    _atomic_write(run_dir / "run-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"run_id": manifest["run_id"], "ready": True, "service_identities": identities, "fake_embeddings": health}, ensure_ascii=False, indent=2))
    return manifest


def _validate_pytest_runtime(run_dir: Path, manifest: dict[str, Any], password: str) -> None:
    """在启动测试进程前回读服务归属与实际连接，拒绝陈旧的 ready 标记。"""
    validate_control_manifest(manifest)
    runtime = manifest["runtime"]
    expected_bindings = {
        "pytest_host": "127.0.0.1",
        "pytest_port": manifest["ports"]["pytest"],
        "pytest_user": "bob108",
        "pytest_db": "bob108_pytest",
        "pytest_admin_db": "postgres",
        "embedding_host": "127.0.0.1",
        "embedding_port": manifest["ports"]["embeddings"],
        "fake_embeddings_url": f"http://127.0.0.1:{manifest['ports']['embeddings']}/api/embed",
    }
    if any(runtime.get(key) != value for key, value in expected_bindings.items()):
        raise RuntimeError("pytest runtime binding differs from the owned control manifest")
    validate_service_identities(manifest.get("service_identities", {}))
    resources = _owned_resources(run_dir, manifest)
    recorded = manifest.get("owned_resources", {})
    for service in ("pytest-db", "fake-embeddings"):
        expected_id = recorded.get(service, {}).get("container_id")
        if not expected_id or resources.get(service, {}).get("container_id") != expected_id:
            raise RuntimeError(f"{service} container identity differs from the owned control manifest")
    actual = asyncio.run(_pg_identity(
        runtime["pytest_host"], runtime["pytest_port"], runtime["pytest_db"], runtime["pytest_user"], password
    ))
    expected = manifest["service_identities"]["pytest"]
    if any(actual.get(key) != expected.get(key) for key in ("database", "system_identifier")):
        raise RuntimeError("pytest PostgreSQL identity differs from the owned control manifest")
    health = _http_health(f"http://127.0.0.1:{runtime['embedding_port']}/api/tags")
    validate_embedding_health(health)


def pytest_command(run_dir: Path, pytest_args: list[str]) -> int:
    manifest = _load_manifest(run_dir)
    if not manifest.get("ready"):
        raise RuntimeError("control services are not ready; run up-control first")
    credentials = _read_dotenv(Path(_config_selection()["credential_env_file"]))
    password = credentials.get("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError("selected credential source has no POSTGRES_PASSWORD")
    try:
        _validate_pytest_runtime(run_dir, manifest, password)
    except Exception as exc:
        manifest["ready"] = False
        _write_manifest(run_dir, manifest)
        detail = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
        raise RuntimeError(f"control services failed pytest readiness: {detail}; run up-control before retrying") from exc
    env = build_pytest_environment(runtime_env=manifest["runtime"], password=password)
    args = pytest_args[1:] if pytest_args and pytest_args[0] == "--" else pytest_args
    completed = subprocess.run([sys.executable, "-m", "pytest", *args], cwd=REPO_ROOT, env=env)
    return completed.returncode


def stop_owned(run_dir: Path) -> None:
    manifest = _load_manifest(run_dir)
    validate_control_manifest(manifest)
    manifest["ready"] = False
    manifest["controlled_runtime_ready"] = False
    _write_manifest(run_dir, manifest)
    for role in ("ui", "api_provider"):
        process = manifest.get("controlled_processes", {}).get(role)
        if isinstance(process, dict):
            _stop_controlled_process(process, run_dir=run_dir, role=role)
    credential_path = Path(_config_selection()["credential_env_file"])
    command = ["docker", "compose", "--project-name", manifest["project"], "--env-file", str(credential_path), "-f", str(run_dir / "compose.yml")]
    running = subprocess.run(
        [*command, "ps", "--status", "running", "-q"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    if running.stdout.strip():
        expected = manifest.get("owned_resources")
        if isinstance(expected, dict):
            current = _owned_resources(run_dir, manifest)
            if any(
                current.get(service, {}).get("container_id") != expected.get(service, {}).get("container_id")
                for service in manifest["services"]
            ):
                raise RuntimeError("refusing to stop containers whose identities differ from the control manifest")
        subprocess.run([*command, "stop"], cwd=REPO_ROOT, check=True)
    manifest["controlled_processes"] = {}
    manifest["controlled_runtime_ready"] = False
    manifest["owned_resources"] = {}
    _write_manifest(run_dir, manifest)


def stop_controlled(run_dir: Path) -> None:
    """仅停止本轮 API/provider 与 UI，保留 PostgreSQL 与嵌入服务。"""
    manifest = _load_manifest(run_dir)
    validate_control_manifest(manifest)
    for role in ("ui", "api_provider"):
        process = manifest.get("controlled_processes", {}).get(role)
        if isinstance(process, dict):
            _stop_controlled_process(process, run_dir=run_dir, role=role)
    manifest["controlled_processes"] = {}
    manifest["controlled_runtime_ready"] = False
    _write_manifest(run_dir, manifest)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare-control", "up-control", "serve-controlled", "stop-controlled", "stop-owned"):
        command = commands.add_parser(name)
        command.add_argument("--run-dir", required=True)
    pytest_parser = commands.add_parser("pytest")
    pytest_parser.add_argument("--run-dir", required=True)
    pytest_parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    run_dir = validate_run_dir(args.run_dir)
    if args.command == "prepare-control":
        manifest = prepare_control(run_dir)
        print(json.dumps({"run_id": manifest["run_id"], "prepared": True, "fixture_sha256": MOCK_SHA256}, ensure_ascii=False))
        return 0
    if args.command == "up-control":
        up_control(run_dir)
        return 0
    if args.command == "pytest":
        return pytest_command(run_dir, args.pytest_args)
    if args.command == "stop-owned":
        stop_owned(run_dir)
        return 0
    if args.command == "serve-controlled":
        serve_controlled(run_dir)
        return 0
    if args.command == "stop-controlled":
        stop_controlled(run_dir)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
