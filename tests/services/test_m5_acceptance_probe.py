from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from apps.m5_acceptance_probe import (
    ProbeError,
    assert_isolated_database,
    build_child_environment,
    load_selected_config,
    redact_evidence,
    verify_assets,
    validate_ui_origin,
    acceptance_connect_factory,
    freeze_case_artifact,
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def selected(tmp_path):
    env_file = tmp_path / "selected.env"
    env_file.write_text("POSTGRES_HOST=127.0.0.1\nPOSTGRES_PORT=5432\nPOSTGRES_USER=test\nPOSTGRES_PASSWORD=selected-db-secret\nPOSTGRES_DB=main_selected\nSELECTED_KEY=selected-model-secret\n")
    realtime = tmp_path / "realtime.json"
    realtime.write_text(json.dumps({"endpoint": "wss://selected.example/realtime", "model": "selected-realtime", "api_key_env": "SELECTED_KEY", "timeout": 30}))
    gate = tmp_path / "gate.json"
    gate.write_text(json.dumps({"emotion": {"provider": "openai-chat-completions-endpoint", "model": "qwen-plus", "endpoint": "https://selected.example/v1", "api_key_env": "SELECTED_KEY", "timeout_ms": 1000}}))
    wav = tmp_path / "input.wav"
    wav.write_bytes(b"selected-asset")
    manifest = tmp_path / "assets.json"
    manifest.write_text(json.dumps({"audio": [{"path": str(wav), "sha256": digest(wav)}], "data_files": []}))
    value = {
        "schema": "m5-selected-runtime-v1",
        "source_root": str(Path(__file__).resolve().parents[2]),
        "database": {"env_file": str(env_file), "main_database": "main_selected", "admin_database": "postgres", "profile_source_database": "tmp_m5_selected", "consent_log_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        "realtime": {"path": str(realtime), "sha256": digest(realtime)},
        "input_gate": {"path": str(gate), "sha256": digest(gate)},
        "assets_manifest": {"path": str(manifest), "sha256": digest(manifest)},
        "report": {"provider": "openai-chat-completions-endpoint", "model": "qwen-plus", "endpoint": "https://selected.example/v1", "api_key_env": "SELECTED_KEY", "timeout_seconds": 30, "timeout_source": "realtime.timeout"},
        "api_url": "http://127.0.0.1:43819",
        "ui_url": "http://127.0.0.1:3479",
    }
    path = tmp_path / "selected.json"
    path.write_text(json.dumps(value))
    return path, value


def test_requires_absolute_explicit_selection():
    with pytest.raises(ProbeError, match="绝对路径"):
        load_selected_config("relative.json")


def test_case_proof_is_immutable_while_the_shared_websocket_log_grows(tmp_path):
    source = tmp_path / "live.json"
    source.write_text('{"events":["C1"]}')
    reference = freeze_case_artifact(source, tmp_path, "C1-submit")
    source.write_text('{"events":["C1","C2"]}')
    assert digest(Path(reference["path"])) == reference["sha256"]
    assert Path(reference["path"]).read_text() == '{"events":["C1"]}'


def test_ui_missing_session_200_response_is_not_binding_evidence():
    run = {"origin_session_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
    with pytest.raises(ProbeError, match="来源"):
        validate_ui_origin({"session_id": run["origin_session_id"], "messages": []}, run)
    validate_ui_origin({"session_id": run["origin_session_id"], "surface": "web"}, run)


@pytest.mark.parametrize("database", ["main_selected", "hexis_memory", "m5_acceptance_bad", 'm5_acceptance_";DROP DATABASE x;--'])
def test_denies_main_and_non_generated_database_names(database):
    with pytest.raises(ProbeError):
        assert_isolated_database(database, "main_selected")


def test_selected_config_and_assets_match_actual_files(selected):
    path, _ = selected
    config = load_selected_config(str(path))
    assert verify_assets(config)["verified_asset_count"] == 1


def test_asset_hash_mismatch_stops_before_database_or_provider(selected):
    path, value = selected
    asset = Path(json.loads(Path(value["assets_manifest"]["path"]).read_text())["audio"][0]["path"])
    asset.write_bytes(b"changed")
    with pytest.raises(ProbeError, match="哈希"):
        verify_assets(load_selected_config(str(path)))


@pytest.mark.parametrize("change", ["main", "credential", "inline", "report_timeout", "endpoint", "runtime_hash"])
def test_rejects_incomplete_or_ambient_selection(selected, change):
    path, value = selected
    value = copy.deepcopy(value)
    if change == "main": value["database"]["main_database"] = "different"
    if change == "credential": Path(value["database"]["env_file"]).write_text("POSTGRES_DB=main_selected\n")
    if change == "inline": value["report"]["api_key"] = "inline-secret"
    if change == "report_timeout": value["report"].pop("timeout_seconds")
    if change == "endpoint": value["api_url"] = "http://public.example:43819"
    if change == "runtime_hash": value["realtime"]["sha256"] = "0" * 64
    path.write_text(json.dumps(value))
    with pytest.raises(ProbeError): load_selected_config(str(path))


def test_child_environment_binds_both_api_and_prisma_and_bypasses_instance_registry(selected, monkeypatch):
    path, _ = selected
    monkeypatch.setenv("HEXIS_INSTANCE", "ambient-main")
    config = load_selected_config(str(path))
    run = {"database": "m5_acceptance_" + "a" * 32}
    env = build_child_environment(config, run, Path("/selected/run.json"))
    assert "HEXIS_INSTANCE" not in env
    assert env["POSTGRES_DB"] == run["database"]
    assert env["HEXIS_DATABASE_URL"].endswith("/" + run["database"])
    assert env["HEXIS_API_URL"] == config["api_url"]
    assert env["HEXIS_BROWSER_API_URL"] == config["api_url"]
    assert env["HEXIS_UI_ORIGIN"] == config["ui_url"]
    assert env["HEXIS_M5_ACCEPTANCE_RUN"] == "/selected/run.json"


def test_no_credentials_are_written_to_nested_evidence(selected):
    path, _ = selected
    config = load_selected_config(str(path))
    value = {"nested": {"url": "postgresql://test:selected-db-secret@host/db", "error": "Bearer selected-model-secret"}, "api_key": "unrecognized-secret"}
    clean = json.dumps(redact_evidence(value, config))
    for forbidden in ("selected-db-secret", "selected-model-secret", "unrecognized-secret"):
        assert forbidden not in clean


@pytest.mark.asyncio
async def test_evidence_socket_observes_without_replacing_messages_and_never_records_headers(selected, tmp_path):
    path, _ = selected
    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps({"run_id": "a" * 32, "database": "m5_acceptance_" + "a" * 32,
                                   "selected_config": {"path": str(path), "sha256": digest(path)}}))
    class Socket:
        def __init__(self): self.sent = []; self.closed = False
        async def send(self, message): self.sent.append(message)
        async def recv(self): return '{"type":"response.function_call_arguments.done","name":"remember","arguments":"{}","call_id":"call-observed"}'
        async def close(self): self.closed = True
    socket = Socket()
    observed_headers = []
    async def connect(_endpoint, *, extra_headers):
        observed_headers.append(extra_headers)
        return socket
    wrapper = await acceptance_connect_factory(run_path, connect)("wss://selected.example", extra_headers={"Authorization": "Bearer selected-model-secret"})
    sent = '{"type":"response.create","response":{}}'
    await wrapper.send(sent)
    received = await wrapper.recv()
    await wrapper.close()
    assert socket.sent == [sent] and socket.closed
    assert json.loads(received)["call_id"] == "call-observed"
    assert observed_headers == [{"Authorization": "Bearer selected-model-secret"}]
    record = json.loads(next((tmp_path / "cloud").glob("*.json")).read_text())
    assert record["raw_events"][0]["raw"] == json.loads(sent)
    assert record["raw_events"][1]["raw"] == json.loads(received)
    assert "selected-model-secret" not in json.dumps(record)
