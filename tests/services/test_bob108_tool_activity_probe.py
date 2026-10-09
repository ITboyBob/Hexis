from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from apps import bob108_tool_activity_probe as probe


def _write_ref(path: Path, content: str) -> dict[str, str]:
    path.write_text(content, encoding="utf-8")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def test_readiness_can_be_rechecked_after_it_marks_the_run_ready(tmp_path, monkeypatch):
    session_id = str(uuid4())
    identity = {"database": "owned_target", "system_identifier": "123"}
    manifest = {
        "run_id": "a" * 32,
        "state": "consent_accepted",
        "api_process": {"pid": 1},
        "ui_process": {"pid": 2},
        "source_fingerprints": {"source.py": "a" * 64},
        "service_identities": {"target": identity},
        "runtime": {"api_url": "http://api.test", "ui_url": "http://ui.test"},
    }
    (tmp_path / "evidence").mkdir()
    monkeypatch.setattr(probe, "load_run", lambda path: (tmp_path, manifest, {}, {}, {}))
    process_checks = []
    monkeypatch.setattr(probe, "_owned_process_alive", lambda process, **kwargs: process_checks.append(kwargs["role"]) or True)
    monkeypatch.setattr(probe, "_assert_current_source_fingerprints", lambda value: None)
    endpoint_calls = []

    def wait_json(url):
        endpoint_calls.append(url)
        return {"run_id": manifest["run_id"], "database_identity": identity,
                "task_worker_ready": True, "lightweight_worker_ready": True}

    monkeypatch.setattr(probe, "_wait_json", wait_json)
    session_creations = []
    monkeypatch.setattr(probe, "_http_json", lambda url, **kwargs: session_creations.append(url) or {"session_id": session_id})
    session_checks = []

    async def belongs_to_target(runtime, values, origin):
        session_checks.append(origin)
        return True

    monkeypatch.setattr(probe, "_session_belongs_to_target", belongs_to_target)
    first = probe.check_readiness(tmp_path)
    second = probe.check_readiness(tmp_path)

    assert first == second
    assert second["state"] == "ready"
    assert second["origin_session_id"] == session_id
    assert process_checks == ["api", "ui", "api", "ui"]
    assert len(endpoint_calls) == 4
    assert len(session_creations) == 1
    assert session_checks == [session_id, session_id]
    assert json.loads((tmp_path / "run-manifest.json").read_text())["state"] == "ready"


def test_ready_run_still_rechecks_owned_process_liveness(tmp_path, monkeypatch):
    manifest = {"state": "ready", "api_process": {"pid": 1}}
    monkeypatch.setattr(probe, "load_run", lambda path: (tmp_path, manifest, {}, {}, {}))
    monkeypatch.setattr(probe, "_owned_process_alive", lambda *args, **kwargs: False)

    with pytest.raises(probe.ProbeError, match="API process identity is not alive"):
        probe.check_readiness(tmp_path)
    assert not (tmp_path / "run-manifest.json").exists()


@pytest.mark.parametrize("state", ["awaiting_consent", "consent_error", "declined", None])
def test_readiness_rejects_runs_without_accepted_consent(tmp_path, monkeypatch, state):
    monkeypatch.setattr(probe, "load_run", lambda path: (tmp_path, {"state": state}, {}, {}, {}))
    monkeypatch.setattr(probe, "_owned_process_alive", lambda *args, **kwargs: pytest.fail("consent gate must run before process checks"))
    with pytest.raises(probe.ProbeError, match="gated on accepted real model consent"):
        probe.check_readiness(tmp_path)


def _ready_launcher_repair_fixture(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    launcher = root / "apps/bob108_tool_activity_probe.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("new launcher", encoding="utf-8")
    ui = root / "hexis-ui/page.tsx"
    ui.parent.mkdir()
    ui.write_text("same product", encoding="utf-8")
    run = tmp_path / "run"
    (run / "evidence").mkdir(parents=True)
    (run / "ui-src").mkdir()
    (run / "ui-src/page.tsx").write_bytes(ui.read_bytes())
    old = {"apps/bob108_tool_activity_probe.py": "0" * 64, "hexis-ui/page.tsx": probe._digest(ui)}
    identity = {"database": "owned", "system_identifier": "123"}
    manifest = {
        "run_id": "a" * 32, "state": "ready", "source_fingerprints": old,
        "consent": {"state": "consent", "operator_override": False, "consent_log_id": str(uuid4())},
        "api_process": {"pid": 1}, "ui_process": {"pid": 2},
        "runtime": {"api_url": "http://api.test", "ui_url": "http://ui.test"},
        "service_identities": {"target": identity},
    }
    (run / "run-manifest.json").write_text(json.dumps(manifest))
    for name in ("api-start", "ui-start", "consent-summary"):
        (run / "evidence" / f"{name}.json").write_text(json.dumps({"source_fingerprints": old, "run_id": manifest["run_id"]}))
    monkeypatch.setattr(probe, "ROOT", root)
    monkeypatch.setattr(probe, "load_run", lambda path: (run, json.loads((run / "run-manifest.json").read_text()), {}, {}, {}))
    monkeypatch.setattr(probe, "_owned_process_alive", lambda *args, **kwargs: True)
    monkeypatch.setattr(probe, "_http_json", lambda *args, **kwargs: {"run_id": manifest["run_id"], "database_identity": identity})
    async def validate(*args):
        return None
    monkeypatch.setattr(probe, "_validate_launcher_repair_consent", validate, raising=False)
    return run, launcher, ui, manifest


def test_ready_launcher_repair_preserves_originals_and_restarts_only_api(tmp_path, monkeypatch):
    run, launcher, _ui, old = _ready_launcher_repair_fixture(tmp_path, monkeypatch)
    originals = {p: p.read_bytes() for p in (run / "evidence").glob("*.json")}
    manifest_before = (run / "run-manifest.json").read_bytes()
    operations = []
    monkeypatch.setattr(probe, "_stop_run_process", lambda process, **kwargs: operations.append(("stop", kwargs["role"])))
    def start(path):
        operations.append(("start", "api"))
        manifest = json.loads((run / "run-manifest.json").read_text())
        manifest["api_process"] = {"pid": 3}
        probe._atomic_json(run / "run-manifest.json", manifest)
        return {"api_pid": 3}
    monkeypatch.setattr(probe, "serve_api", start)

    result = probe.repair_ready_launcher(run, reason="修复 readiness 重复调用状态门禁")

    assert operations == [("stop", "api"), ("start", "api")]
    assert result["provider_requested"] is False
    assert all(p.read_bytes() == data for p, data in originals.items())
    assert (run / "evidence/launcher-repair-original-manifest.json").read_bytes() == manifest_before
    current = json.loads((run / "run-manifest.json").read_text())
    assert current["source_fingerprints"]["apps/bob108_tool_activity_probe.py"] == probe._digest(launcher)
    assert current["consent"] == old["consent"]
    assert current["ui_process"] == old["ui_process"]
    assert current["api_process"] == {"pid": 3}
    with pytest.raises(probe.ProbeError):
        probe.repair_ready_launcher(run, reason="禁止重复刷新")
    assert len(operations) == 2


@pytest.mark.parametrize("invalid", ["product", "ui-copy", "cases", "ownership", "identity", "override", "consent", "reason"])
def test_ready_launcher_repair_rejects_drift_before_stopping_services(tmp_path, monkeypatch, invalid):
    run, _launcher, ui, manifest = _ready_launcher_repair_fixture(tmp_path, monkeypatch)
    if invalid == "product":
        ui.write_text("drift")
    elif invalid == "ui-copy":
        (run / "ui-src/page.tsx").write_text("drift")
    elif invalid == "cases":
        (run / "evidence/cases").mkdir()
        (run / "evidence/cases/failed.json").write_text("{}")
    elif invalid == "ownership":
        monkeypatch.setattr(probe, "_owned_process_alive", lambda *args, **kwargs: False)
    elif invalid == "identity":
        monkeypatch.setattr(probe, "_http_json", lambda *args, **kwargs: {"run_id": "wrong"})
    elif invalid in {"override", "consent"}:
        manifest["consent"]["operator_override" if invalid == "override" else "state"] = True if invalid == "override" else "decline"
        (run / "run-manifest.json").write_text(json.dumps(manifest))
    before = (run / "run-manifest.json").read_bytes()
    monkeypatch.setattr(probe, "_stop_run_process", lambda *args, **kwargs: pytest.fail("invalid inputs must not stop services"))
    with pytest.raises(probe.ProbeError):
        probe.repair_ready_launcher(run, reason="" if invalid == "reason" else "限定 launcher 修复")
    assert (run / "run-manifest.json").read_bytes() == before
    assert not (run / "evidence/launcher-repair-audit.json").exists()


def test_readiness_appends_changed_start_evidence_after_launcher_repair(tmp_path):
    original = tmp_path / "api-start.json"
    original.write_text('{"old":true}')
    before = original.read_bytes()
    value = {"source_fingerprints": {"probe": "new"}}
    path = probe._write_readiness_record(tmp_path, "api-start", value, launcher_repaired=True)
    assert path.name == "api-start-after-launcher-repair.json"
    assert original.read_bytes() == before
    assert probe._write_readiness_record(tmp_path, "api-start", value, launcher_repaired=True) == path
    with pytest.raises(probe.ProbeError):
        probe._write_readiness_record(tmp_path, "api-start", {"different": True}, launcher_repaired=True)
    with pytest.raises(probe.ProbeError):
        probe._write_readiness_record(tmp_path, "api-start", value, launcher_repaired=False)


@pytest.mark.parametrize("scenario", ["valid", "wrong-db", "override", "inactive", "tool-events"])
def test_launcher_repair_validates_real_target_consent_without_writes(monkeypatch, scenario):
    import asyncpg

    profile = {"provider": "provider", "model": "qwen-plus", "endpoint": "https://provider.test/v1"}
    identity = {"database": "owned", "system_identifier": "123"}
    manifest = {"runtime": {"target": {"host": "127.0.0.1", "port": 40000, "database": "owned", "user": "user"}, "provider_profile": profile},
                "service_identities": {"target": identity}, "consent": {"consent_log_id": str(uuid4())}}
    calls = []
    class Connection:
        async def fetchrow(self, query, *args):
            calls.append(query)
            if "pg_control_system" in query:
                return {**identity, "database": "wrong" if scenario == "wrong-db" else "owned"}
            return {**profile, "decision": "consent", "response": {"operator_override": scenario == "override"}}
        async def fetchval(self, query):
            calls.append(query)
            return ("decline" if scenario == "inactive" else "consent") if "get_agent_consent_status" in query else scenario == "tool-events"
        async def close(self):
            calls.append("close")
    async def connect(*args, **kwargs):
        return Connection()
    monkeypatch.setattr(asyncpg, "connect", connect)
    if scenario == "valid":
        asyncio.run(probe._validate_launcher_repair_consent(manifest, {"POSTGRES_PASSWORD": "secret"}))
    else:
        with pytest.raises(probe.ProbeError):
            asyncio.run(probe._validate_launcher_repair_consent(manifest, {"POSTGRES_PASSWORD": "secret"}))
    assert calls[-1] == "close"
    assert all(query.startswith("SELECT") for query in calls[:-1])


def _ready_e2e_repair_fixture(tmp_path, monkeypatch):
    run, launcher, ui, manifest = _ready_launcher_repair_fixture(tmp_path, monkeypatch)
    spec = probe.ROOT / "hexis-ui/e2e/tool-activity.spec.ts"
    spec.parent.mkdir()
    spec.write_text("new tested e2e")
    startup = probe.ROOT / "hexis-ui/lib/tool-activity-startup.test.ts"
    startup.parent.mkdir()
    startup.write_text("tested startup regression")
    trace = probe.ROOT / "hexis-ui/test-results/trace.zip"
    trace.parent.mkdir()
    trace.write_bytes(b"interrupted playwright trace")
    (run / "ui-src/e2e").mkdir()
    (run / "ui-src/e2e/tool-activity.spec.ts").write_text("original e2e")
    spec_key = "hexis-ui/e2e/tool-activity.spec.ts"
    manifest["source_fingerprints"][spec_key] = probe._digest(run / "ui-src/e2e/tool-activity.spec.ts")
    first_manifest = run / "evidence/launcher-repair-original-manifest.json"
    probe._exclusive_json(first_manifest, manifest)
    first_audit = {
        "run_id": manifest["run_id"], "current_launcher_sha256": "1" * 64,
        "original_manifest": {"path": str(first_manifest), "sha256": probe._digest(first_manifest)},
        "retained_original_evidence": {str(p.name): {"path": str(p), "sha256": probe._digest(p)} for p in (run / "evidence").glob("*-*.json")},
    }
    first_path = run / "evidence/launcher-repair-audit.json"
    probe._exclusive_json(first_path, first_audit)
    manifest["source_fingerprints"]["apps/bob108_tool_activity_probe.py"] = "1" * 64
    manifest["launcher_repair"] = {"path": str(first_path), "sha256": probe._digest(first_path)}
    probe._exclusive_json(run / "evidence/launcher-repair-result.json", {"api_pid": 1})
    probe._exclusive_json(run / "evidence/api-start-after-launcher-repair.json", {"source_fingerprints": manifest["source_fingerprints"]})
    probe._atomic_json(run / "run-manifest.json", manifest)
    monkeypatch.setattr(probe, "_http_json", lambda *args, **kwargs: {"run_id": manifest["run_id"], "database_identity": manifest["service_identities"]["target"], "source_fingerprints": manifest["source_fingerprints"]})
    async def empty(*args):
        return {"empty_session_ids": [str(uuid4())]}
    monkeypatch.setattr(probe, "_validate_empty_e2e_repair_target", empty, raising=False)
    return run, launcher, spec, ui, manifest


def test_ready_e2e_repair_preserves_first_repair_and_all_original_evidence(tmp_path, monkeypatch):
    run, launcher, spec, _ui, previous = _ready_e2e_repair_fixture(tmp_path, monkeypatch)
    originals = {p: p.read_bytes() for p in (run / "evidence").glob("*.json")}
    before = (run / "run-manifest.json").read_bytes()
    operations = []
    monkeypatch.setattr(probe, "_stop_run_process", lambda process, **kwargs: operations.append(("stop", kwargs["role"])))
    monkeypatch.setattr(probe, "serve_api", lambda path: operations.append(("start", "api")) or {"api_pid": 3})
    trace = probe.ROOT / "hexis-ui/test-results/trace.zip"
    result = probe.repair_ready_e2e(run, reason="修复 E2E session 切换", expected_e2e_sha256=probe._digest(spec), interrupted_trace=trace)
    assert result["provider_requested"] is False
    assert operations == [("stop", "api"), ("start", "api")]
    assert all(path.read_bytes() == data for path, data in originals.items())
    assert (run / "evidence/e2e-repair-original-manifest.json").read_bytes() == before
    current = json.loads((run / "run-manifest.json").read_text())
    assert current["consent"] == previous["consent"]
    assert current["launcher_repair"] == previous["launcher_repair"]
    assert current["ui_process"] == previous["ui_process"]
    audit = json.loads((run / "evidence/e2e-repair-audit.json").read_text())
    assert audit["changes"]["hexis-ui/lib/tool-activity-startup.test.ts"]["previous"] is None
    assert Path(audit["interrupted_trace"]["path"]).read_bytes() == trace.read_bytes()
    for key, source in (("apps/bob108_tool_activity_probe.py", launcher), ("hexis-ui/e2e/tool-activity.spec.ts", spec)):
        assert audit["changes"][key] == {"previous": previous["source_fingerprints"][key], "current": probe._digest(source)}
    with pytest.raises(probe.ProbeError):
        probe.repair_ready_e2e(run, reason="禁止重复", expected_e2e_sha256=probe._digest(spec), interrupted_trace=trace)


@pytest.mark.parametrize("invalid", ["product", "ui-copy", "cases", "ownership", "identity", "first-audit", "expected-e2e", "tool-state", "startup-missing", "trace-missing"])
def test_ready_e2e_repair_rejects_drift_or_activity_before_any_mutation(tmp_path, monkeypatch, invalid):
    run, _launcher, spec, ui, manifest = _ready_e2e_repair_fixture(tmp_path, monkeypatch)
    if invalid == "product":
        ui.write_text("drift")
    elif invalid == "ui-copy":
        (run / "ui-src/page.tsx").write_text("drift")
    elif invalid == "cases":
        (run / "evidence/cases").mkdir()
        (run / "evidence/cases/failed.json").write_text("{}")
    elif invalid == "ownership":
        monkeypatch.setattr(probe, "_owned_process_alive", lambda *args, **kwargs: False)
    elif invalid == "identity":
        monkeypatch.setattr(probe, "_http_json", lambda *args, **kwargs: {"run_id": "wrong"})
    elif invalid == "first-audit":
        (run / "evidence/launcher-repair-audit.json").write_text("{}")
    elif invalid == "tool-state":
        async def deny(*args):
            raise probe.ProbeError("target has existing action")
        monkeypatch.setattr(probe, "_validate_empty_e2e_repair_target", deny)
    elif invalid == "startup-missing":
        (probe.ROOT / "hexis-ui/lib/tool-activity-startup.test.ts").unlink()
    elif invalid == "trace-missing":
        (probe.ROOT / "hexis-ui/test-results/trace.zip").unlink()
    before = (run / "run-manifest.json").read_bytes()
    monkeypatch.setattr(probe, "_stop_run_process", lambda *args, **kwargs: pytest.fail("must reject before stopping"))
    with pytest.raises(probe.ProbeError):
        probe.repair_ready_e2e(run, reason="限定修复", expected_e2e_sha256="0" * 64 if invalid == "expected-e2e" else probe._digest(spec), interrupted_trace=probe.ROOT / "hexis-ui/test-results/trace.zip")
    assert (run / "run-manifest.json").read_bytes() == before
    assert not (run / "evidence/e2e-repair-audit.json").exists()


@pytest.mark.parametrize("scenario", ["empty", "action", "message", "old-session", "other-surface", "cleared", "origin-missing", "no-sessions"])
def test_e2e_repair_target_only_allows_this_runs_empty_sessions(monkeypatch, scenario):
    import asyncpg
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    origin = str(uuid4())
    manifest = {"runtime": {"target": {"host": "127.0.0.1", "port": 40000, "database": "owned", "user": "user"}},
                "consent": {"decided_at": now.isoformat()}, "origin_session_id": origin}
    row = {"id": origin, "surface": "web", "external_id": None, "status": "active", "metadata": {"source": "web", "created_by": "user"}, "created_at": now, "cleared_at": None}
    if scenario == "old-session":
        row["created_at"] = now - timedelta(seconds=1)
    elif scenario == "other-surface":
        row["surface"] = "task"
    elif scenario == "cleared":
        row["cleared_at"] = now
    elif scenario == "origin-missing":
        row["id"] = str(uuid4())
    calls = []
    class Connection:
        async def fetchval(self, query):
            calls.append(query)
            return scenario in {"action", "message"}
        async def fetch(self, query):
            calls.append(query)
            return [] if scenario == "no-sessions" else [row]
        async def close(self):
            calls.append("close")
    async def connect(*args, **kwargs):
        return Connection()
    async def accepted(*args):
        calls.append("SELECT consent preflight")
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(probe, "_validate_launcher_repair_consent", accepted)
    if scenario == "empty":
        assert asyncio.run(probe._validate_empty_e2e_repair_target(manifest, {"POSTGRES_PASSWORD": "secret"}))["empty_session_ids"] == [origin]
    else:
        with pytest.raises(probe.ProbeError):
            asyncio.run(probe._validate_empty_e2e_repair_target(manifest, {"POSTGRES_PASSWORD": "secret"}))
    assert calls[-1] == "close"
    assert all(query.startswith("SELECT") for query in calls[:-1])


def test_e2e_repair_readiness_appends_third_record_without_overwriting_either_baseline(tmp_path):
    original = tmp_path / "api-start.json"
    first_repair = tmp_path / "api-start-after-launcher-repair.json"
    original.write_text('{"source":"initial"}')
    first_repair.write_text('{"source":"launcher"}')
    before = {p: p.read_bytes() for p in (original, first_repair)}
    current = {"source": "e2e"}
    path = probe._write_readiness_record(tmp_path, "api-start", current, launcher_repaired=True, e2e_repaired=True)
    assert path.name == "api-start-after-e2e-repair.json"
    assert all(p.read_bytes() == data for p, data in before.items())
    assert probe._write_readiness_record(tmp_path, "api-start", current, launcher_repaired=True, e2e_repaired=True) == path
    with pytest.raises(probe.ProbeError):
        probe._write_readiness_record(tmp_path, "api-start", {"source": "unexpected"}, launcher_repaired=True, e2e_repaired=True)


def _harness_retry_fixture(tmp_path, monkeypatch):
    run, launcher, spec, ui, manifest = _ready_e2e_repair_fixture(tmp_path, monkeypatch)
    original = run / "evidence/e2e-repair-original-manifest.json"
    probe._exclusive_json(original, manifest)
    startup = "hexis-ui/lib/tool-activity-startup.test.ts"
    changes = {"apps/bob108_tool_activity_probe.py": {"previous": "1" * 64, "current": "2" * 64},
               "hexis-ui/e2e/tool-activity.spec.ts": {"previous": manifest["source_fingerprints"]["hexis-ui/e2e/tool-activity.spec.ts"], "current": probe._digest(spec)},
               startup: {"previous": None, "current": probe._digest(probe.ROOT / startup)}}
    audit = {"run_id": manifest["run_id"], "changes": changes, "previous_source_fingerprints": copy.deepcopy(manifest["source_fingerprints"]),
             "original_manifest": {"path": str(original), "sha256": probe._digest(original)}, "retained_original_evidence": {}}
    second = run / "evidence/e2e-repair-audit.json"
    probe._exclusive_json(second, audit)
    manifest["e2e_repair"] = {"path": str(second), "sha256": probe._digest(second)}
    for name, change in changes.items():
        manifest["source_fingerprints"][name] = change["current"]
    probe._exclusive_json(run / "evidence/api-start-after-e2e-repair.json", {"source_fingerprints": manifest["source_fingerprints"]})
    probe._atomic_json(run / "run-manifest.json", manifest)
    launcher.write_text("known retry CLI")
    spec.write_text("fixed sequential turns")
    session = str(uuid4())
    events = [{"event_id": str(uuid4()), "session_id": session, "event_type": kind, "payload": {"tool_name": "voice_recall", "call_id": "call_one"}} for kind in ("TOOL_START", "TOOL_RESULT")]
    events[1]["payload"]["success"] = True
    cases = run / "evidence/cases"
    cases.mkdir()
    for name, status, image in (("voice-recall", "PASS", "voice-recall.png"), ("remember", "FAIL", "remember-failed.png")):
        screenshot = cases / image
        screenshot.write_bytes(b"screenshot")
        value = {"run_id": manifest["run_id"], "case": name, "status": status, "origin_session_id": session,
                 "reason": "Timed out waiting for actual Activity events" if status == "FAIL" else None,
                 "evidence": {"source_fingerprints": manifest["source_fingerprints"], "event_ids": [e["event_id"] for e in events] if status == "PASS" else [],
                              "screenshot": str(screenshot), "screenshot_sha256": probe._digest(screenshot),
                              "browser_realtime_protocol": {"received": [{"type": "error", "code": "speech_not_recognized", "session_id": session}] if status == "FAIL" else []}}}
        probe._exclusive_json(cases / f"{name}.json", value)
    probe._exclusive_json(cases / "browser-run.json", {"run_id": manifest["run_id"], "origin_session_id": session, "received": [{"type": "error", "code": "speech_not_recognized", "session_id": session}]})
    async def validated(*args):
        return {"tool_events": events, "provider_realtime_attempts": [{"qwen_session_id": "session-real"}], "remember_failure_code": "speech_not_recognized"}
    monkeypatch.setattr(probe, "_validate_harness_retry_target", validated, raising=False)
    monkeypatch.setattr(probe, "_assert_harness_retry_owned_services", lambda *args: None, raising=False)
    return run, spec, ui, session, events


def test_harness_retry_archives_original_cases_trace_manifest_before_only_api_restart(tmp_path, monkeypatch):
    run, spec, _ui, session, events = _harness_retry_fixture(tmp_path, monkeypatch)
    originals = {p: p.read_bytes() for p in (run / "evidence/cases").glob("*")}
    before = (run / "run-manifest.json").read_bytes()
    trace = probe.ROOT / "hexis-ui/test-results/trace.zip"
    operations = []
    monkeypatch.setattr(probe, "_stop_run_process", lambda process, **kwargs: operations.append(("stop", kwargs["role"])))
    monkeypatch.setattr(probe, "serve_api", lambda path: operations.append(("start", "api")) or {"api_pid": 4})
    result = probe.repair_e2e_harness_retry(run, reason="修复序列", expected_e2e_sha256=probe._digest(spec), expected_startup_sha256=json.loads((run / "run-manifest.json").read_text())["source_fingerprints"]["hexis-ui/lib/tool-activity-startup.test.ts"], interrupted_trace=trace, session_id=session)
    archive = run / "evidence/e2e-attempt-1"
    assert (archive / "run-manifest.json").read_bytes() == before
    assert (archive / "trace.zip").read_bytes() == trace.read_bytes()
    assert all(p.read_bytes() == raw and (archive / "cases" / p.name).read_bytes() == raw for p, raw in originals.items())
    assert operations == [("stop", "api"), ("start", "api")]
    assert result["provider_requested"] is False
    audit = json.loads((run / "evidence/harness-retry-audit.json").read_text())
    assert audit["prior_attempt"]["tool_events"] == events
    with pytest.raises(probe.ProbeError):
        probe.repair_e2e_harness_retry(run, reason="禁止第二次", expected_e2e_sha256=probe._digest(spec), expected_startup_sha256=json.loads((run / "run-manifest.json").read_text())["source_fingerprints"]["hexis-ui/lib/tool-activity-startup.test.ts"], interrupted_trace=trace, session_id=session)


@pytest.mark.parametrize("invalid", ["product", "startup-test", "ui-copy", "ownership", "db-ownership", "identity", "case-status", "wrong-session", "failure-code", "case-extra", "screenshot", "trace", "prior-audit", "db-events"])
def test_harness_retry_rejects_every_unapproved_boundary_before_archiving_or_stopping(tmp_path, monkeypatch, invalid):
    run, spec, ui, session, _events = _harness_retry_fixture(tmp_path, monkeypatch)
    trace = probe.ROOT / "hexis-ui/test-results/trace.zip"
    if invalid == "product":
        ui.write_text("drift")
    elif invalid == "startup-test":
        (probe.ROOT / "hexis-ui/lib/tool-activity-startup.test.ts").write_text("unapproved")
    elif invalid == "ui-copy":
        (run / "ui-src/page.tsx").write_text("drift")
    elif invalid == "ownership":
        monkeypatch.setattr(probe, "_owned_process_alive", lambda *args, **kwargs: False)
    elif invalid == "db-ownership":
        def deny(*args):
            raise probe.ProbeError("database container ownership changed")
        monkeypatch.setattr(probe, "_assert_harness_retry_owned_services", deny)
    elif invalid == "identity":
        monkeypatch.setattr(probe, "_http_json", lambda *args, **kwargs: {"run_id": "wrong"})
    elif invalid in {"case-status", "wrong-session", "failure-code"}:
        p = run / "evidence/cases/remember.json"
        value = json.loads(p.read_text())
        if invalid == "case-status":
            value["status"] = "PASS"
        elif invalid == "wrong-session":
            value["origin_session_id"] = str(uuid4())
        else:
            value["evidence"]["browser_realtime_protocol"]["received"] = []
        probe._atomic_json(p, value)
    elif invalid == "case-extra":
        (run / "evidence/cases/background-tools.json").write_text("{}")
    elif invalid == "screenshot":
        (run / "evidence/cases/remember-failed.png").write_bytes(b"changed")
    elif invalid == "trace":
        trace.unlink()
    elif invalid == "prior-audit":
        (run / "evidence/e2e-repair-audit.json").write_text("{}")
    elif invalid == "db-events":
        async def deny(*args):
            raise probe.ProbeError("unexpected tool or action")
        monkeypatch.setattr(probe, "_validate_harness_retry_target", deny)
    before = (run / "run-manifest.json").read_bytes()
    monkeypatch.setattr(probe, "_stop_run_process", lambda *args, **kwargs: pytest.fail("must reject before stop"))
    with pytest.raises(probe.ProbeError):
        probe.repair_e2e_harness_retry(run, reason="限定重试", expected_e2e_sha256=probe._digest(spec), expected_startup_sha256=json.loads((run / "run-manifest.json").read_text())["source_fingerprints"]["hexis-ui/lib/tool-activity-startup.test.ts"], interrupted_trace=trace, session_id=session)
    assert (run / "run-manifest.json").read_bytes() == before
    assert not (run / "evidence/e2e-attempt-1").exists()


@pytest.mark.parametrize("invalid", [None, "extra-tool", "wrong-call", "wrong-tool", "wrong-session", "missing-error", "extra-input", "extra-provider"])
def test_harness_retry_event_boundary_allows_exactly_one_recall_pair_and_failed_speech(invalid):
    session = str(uuid4())
    events = [{"event_id": str(uuid4()), "session_id": session, "event_type": kind, "payload": payload}
              for kind, payload in (("voice_session_ready", {"qwen_session_id": "actual-provider-session"}),
                                    ("TOOL_START", {"tool_name": "voice_recall", "call_id": "call_one"}),
                                    ("TOOL_RESULT", {"tool_name": "voice_recall", "call_id": "call_one", "success": True}),
                                    ("voice_speech_start", {}), ("speech_without_usable_transcript", {}))]
    if invalid == "extra-tool":
        events.append({**events[1], "event_id": str(uuid4())})
    elif invalid == "wrong-call":
        events[2]["payload"]["call_id"] = "different"
    elif invalid == "wrong-tool":
        events[2]["payload"]["tool_name"] = "remember"
    elif invalid == "wrong-session":
        events[1]["session_id"] = str(uuid4())
    elif invalid == "missing-error":
        events.pop()
    elif invalid == "extra-input":
        events.append({"event_id": str(uuid4()), "session_id": session, "event_type": "voice_input_committed", "payload": {}})
    elif invalid == "extra-provider":
        events.append({**events[0], "event_id": str(uuid4())})
    if invalid is None:
        assert probe._validate_harness_retry_events(events, session)["tool_events"] == events[1:3]
    else:
        with pytest.raises(probe.ProbeError):
            probe._validate_harness_retry_events(events, session)


@pytest.mark.parametrize("drift", [False, True])
def test_harness_retry_checks_original_database_container_ids(tmp_path, monkeypatch, drift):
    resources = {name: {"container_id": name} for name in ("pytest-db", "browser-db", "fake-embeddings")}
    actual = copy.deepcopy(resources)
    if drift:
        actual["browser-db"]["container_id"] = "different"
    monkeypatch.setattr(probe.control_runtime, "_owned_resources", lambda *args: actual)
    if drift:
        with pytest.raises(probe.ProbeError):
            probe._assert_harness_retry_owned_services(tmp_path, {"owned_resources": resources})
    else:
        probe._assert_harness_retry_owned_services(tmp_path, {"owned_resources": resources})


def _case_refresh_fixture(tmp_path, monkeypatch):
    run, spec, ui, session, events = _harness_retry_fixture(tmp_path, monkeypatch)
    manifest = json.loads((run / "run-manifest.json").read_text())
    archive = run / "evidence/e2e-attempt-1"
    archive.mkdir()
    original = archive / "run-manifest.json"
    probe._exclusive_json(original, manifest)
    changes = {key: {"previous": manifest["source_fingerprints"][key], "current": probe._digest(probe.ROOT / key)}
               for key in ("apps/bob108_tool_activity_probe.py", "hexis-ui/e2e/tool-activity.spec.ts", "hexis-ui/lib/tool-activity-startup.test.ts")}
    audit = {"run_id": manifest["run_id"], "changes": changes, "original_manifest": {"path": str(original), "sha256": probe._digest(original)},
             "previous_source_fingerprints": copy.deepcopy(manifest["source_fingerprints"]), "retained_original_evidence": {}, "prior_attempt": {"tool_events": events, "raw_behavior_events": events}}
    audit_path = run / "evidence/harness-retry-audit.json"
    probe._exclusive_json(audit_path, audit)
    manifest["harness_retry"] = {"path": str(audit_path), "sha256": probe._digest(audit_path)}
    for key, change in changes.items():
        manifest["source_fingerprints"][key] = change["current"]
    probe._atomic_json(run / "run-manifest.json", manifest)
    for case_path in (run / "evidence/cases").glob("*.json"):
        raw = json.loads(case_path.read_text())
        if "evidence" in raw:
            raw["evidence"]["source_fingerprints"] = manifest["source_fingerprints"]
            raw["evidence"]["browser_realtime_protocol"]["received"] = []
        elif "received" in raw:
            raw["received"] = []
        probe._atomic_json(case_path, raw)
    monkeypatch.setattr(probe, "_http_json", lambda *args, **kwargs: {"run_id": manifest["run_id"], "database_identity": manifest["service_identities"]["target"], "source_fingerprints": manifest["source_fingerprints"]})
    spec.write_text("independent case coverage")
    (probe.ROOT / "apps/bob108_tool_activity_probe.py").write_text("not-run verifier and case refresh CLI")
    async def valid(*args):
        return {"tool_events": events, "prior_sessions_preserved": True, "completed_input_ids": ["recall-input", "remember-input"]}
    monkeypatch.setattr(probe, "_validate_case_refresh_target", valid, raising=False)
    return run, spec, ui, session


def test_case_refresh_archives_second_attempt_and_keeps_prior_retry_immutable(tmp_path, monkeypatch):
    run, spec, _ui, session = _case_refresh_fixture(tmp_path, monkeypatch)
    previous = {p: p.read_bytes() for p in (run / "evidence").rglob("*") if p.is_file()}
    operations = []
    monkeypatch.setattr(probe, "_stop_run_process", lambda process, **kwargs: operations.append(("stop", kwargs["role"])))
    monkeypatch.setattr(probe, "serve_api", lambda path: operations.append(("start", "api")) or {"api_pid": 5})
    result = probe.repair_e2e_case_refresh(run, reason="逐案覆盖", expected_e2e_sha256=probe._digest(spec), expected_startup_sha256=probe._digest(probe.ROOT / "hexis-ui/lib/tool-activity-startup.test.ts"), interrupted_trace=probe.ROOT / "hexis-ui/test-results/trace.zip", session_id=session)
    assert result["provider_requested"] is False
    assert operations == [("stop", "api"), ("start", "api")]
    assert all(p.read_bytes() == data for p, data in previous.items())
    assert (run / "evidence/e2e-attempt-2/cases/remember.json").read_bytes() == previous[run / "evidence/cases/remember.json"]
    with pytest.raises(probe.ProbeError):
        probe.repair_e2e_case_refresh(run, reason="重复", expected_e2e_sha256=probe._digest(spec), expected_startup_sha256=probe._digest(probe.ROOT / "hexis-ui/lib/tool-activity-startup.test.ts"), interrupted_trace=probe.ROOT / "hexis-ui/test-results/trace.zip", session_id=session)


@pytest.mark.parametrize("invalid", ["accounting", "case-extra", "product", "old-audit", "startup-hash"])
def test_case_refresh_rejects_unexplained_activity_and_source_drift_before_archival(tmp_path, monkeypatch, invalid):
    run, spec, ui, session = _case_refresh_fixture(tmp_path, monkeypatch)
    if invalid == "accounting":
        async def deny(*args):
            raise probe.ProbeError("unexplained tool/action")
        monkeypatch.setattr(probe, "_validate_case_refresh_target", deny)
    elif invalid == "case-extra":
        (run / "evidence/cases/add-evidence.json").write_text("{}")
    elif invalid == "product":
        ui.write_text("drift")
    elif invalid == "old-audit":
        (run / "evidence/harness-retry-audit.json").write_text("{}")
    monkeypatch.setattr(probe, "_stop_run_process", lambda *args, **kwargs: pytest.fail("must not stop"))
    with pytest.raises(probe.ProbeError):
        probe.repair_e2e_case_refresh(run, reason="范围门禁", expected_e2e_sha256=probe._digest(spec), expected_startup_sha256="0" * 64 if invalid == "startup-hash" else probe._digest(probe.ROOT / "hexis-ui/lib/tool-activity-startup.test.ts"), interrupted_trace=probe.ROOT / "hexis-ui/test-results/trace.zip", session_id=session)
    assert not (run / "evidence/e2e-attempt-2").exists()


@pytest.mark.parametrize("invalid", [None, "partial", "unmatched-response", "model-failed", "extra-tool", "extra-input", "wrong-session"])
def test_case_refresh_requires_complete_responses_for_exactly_two_known_inputs(invalid):
    session = str(uuid4())
    events = []
    def event(kind, payload):
        row = {"event_id": str(uuid4()), "session_id": session, "event_type": kind, "payload": payload}
        events.append(row)
        return row
    event("voice_session_ready", {"qwen_session_id": "provider-session"})
    for index in range(2):
        identity = {"input_turn_id": f"input-{index}", "stimulus_revision": 0}
        event("voice_transcript_final", {"input_turn_id": identity["input_turn_id"]})
        event("voice_input_committed", {"identity": identity})
        event("voice_model_call_started", {"identity": identity, "response_id": f"response-{index}"})
        if index == 0:
            event("TOOL_START", {"tool_name": "voice_recall", "call_id": "recall-call"})
            event("TOOL_RESULT", {"tool_name": "voice_recall", "call_id": "recall-call", "success": True})
        event("voice_model_call_ended", {"identity": identity, "response_id": f"response-{index}", "reason": "completed"})
        event("voice_turn_completed", {"source_identity": f"voice:{session}:{identity['input_turn_id']}:0", "response_ids": [f"response-{index}"], "playback_scope": "complete"})
    if invalid == "partial":
        events[-1]["payload"]["playback_scope"] = "partial"
    elif invalid == "unmatched-response":
        events[-1]["payload"]["response_ids"] = ["different-response"]
    elif invalid == "model-failed":
        events[-2]["payload"]["reason"] = "failed"
    elif invalid == "extra-tool":
        event("TOOL_START", {"tool_name": "remember", "call_id": "different"})
    elif invalid == "extra-input":
        event("voice_input_committed", {"identity": {"input_turn_id": "third-input"}})
    elif invalid == "wrong-session":
        events[-1]["session_id"] = str(uuid4())
    if invalid is None:
        assert probe._validate_case_refresh_events(events, session)["completed_input_ids"] == ["input-0", "input-1"]
    else:
        with pytest.raises(probe.ProbeError):
            probe._validate_case_refresh_events(events, session)


def _evidence_replay_fixture(tmp_path, monkeypatch):
    run, spec, ui, session = _case_refresh_fixture(tmp_path, monkeypatch)
    manifest = json.loads((run / "run-manifest.json").read_text())
    original = run / "evidence/e2e-attempt-2/run-manifest.json"
    original.parent.mkdir()
    probe._exclusive_json(original, manifest)
    changes = {key: {"previous": manifest["source_fingerprints"][key], "current": probe._digest(probe.ROOT / key)} for key in ("apps/bob108_tool_activity_probe.py", "hexis-ui/e2e/tool-activity.spec.ts", "hexis-ui/lib/tool-activity-startup.test.ts")}
    audit = {"run_id": manifest["run_id"], "changes": changes, "original_manifest": {"path": str(original), "sha256": probe._digest(original)}, "previous_source_fingerprints": copy.deepcopy(manifest["source_fingerprints"]), "retained_original_evidence": {}, "prior_attempt": {"raw_behavior_events": []}}
    record = run / "evidence/case-refresh-audit.json"
    probe._exclusive_json(record, audit)
    manifest["case_refresh"] = {"path": str(record), "sha256": probe._digest(record)}
    for name, change in changes.items():
        manifest["source_fingerprints"][name] = change["current"]
    probe._atomic_json(run / "run-manifest.json", manifest)
    for case in (run / "evidence/cases").glob("*.json"):
        raw = json.loads(case.read_text())
        if "evidence" in raw:
            raw["evidence"]["source_fingerprints"] = manifest["source_fingerprints"]
        probe._atomic_json(case, raw)
    for name, status in (("add-evidence", "NOT_RUN"), ("background-tools", "FAIL")):
        probe._exclusive_json(run / f"evidence/cases/{name}.json", {"run_id": manifest["run_id"], "origin_session_id": session, "case": name, "status": status, "evidence": {"source_fingerprints": manifest["source_fingerprints"]}})
    monkeypatch.setattr(probe, "_http_json", lambda *args, **kwargs: {"run_id": manifest["run_id"], "database_identity": manifest["service_identities"]["target"], "source_fingerprints": manifest["source_fingerprints"]})
    spec.write_text("nested receipt and task session replay")
    (probe.ROOT / "apps/bob108_tool_activity_probe.py").write_text("fixed readonly receipt probe")
    task_id, task_session, memory_id = str(uuid4()), str(uuid4()), str(uuid4())
    async def valid(*args):
        return {"actual_memory_id": memory_id, "historical_event_ledger_preserved": True, "background_task_session": task_session, "real_time_working_verified": False}
    monkeypatch.setattr(probe, "_validate_evidence_replay_accounting", valid, raising=False)
    return run, spec, ui, session, task_id, task_session, memory_id


def test_evidence_replay_keeps_root_artifacts_and_previous_archives_unchanged(tmp_path, monkeypatch):
    run, spec, _ui, origin, task, task_session, memory = _evidence_replay_fixture(tmp_path, monkeypatch)
    originals = {p: p.read_bytes() for p in (run / "evidence").rglob("*") if p.is_file()}
    previous_manifest = (run / "run-manifest.json").read_bytes()
    operations = []
    monkeypatch.setattr(probe, "_stop_run_process", lambda process, **kwargs: operations.append(("stop", kwargs["role"])))
    monkeypatch.setattr(probe, "serve_api", lambda path: operations.append(("start", "api")) or {"api_pid": 6})
    kwargs = {"reason": "只读证据回放", "expected_e2e_sha256": probe._digest(spec), "expected_startup_sha256": probe._digest(probe.ROOT / "hexis-ui/lib/tool-activity-startup.test.ts"), "interrupted_trace": probe.ROOT / "hexis-ui/test-results/trace.zip", "origin_session_id": origin, "task_id": task, "task_session_id": task_session, "memory_id": memory}
    result = probe.repair_evidence_replay(run, **kwargs)
    assert result["provider_requested"] is False
    assert result["real_time_working_verified"] is False
    assert operations == [("stop", "api"), ("start", "api")]
    assert all(p.read_bytes() == raw for p, raw in originals.items())
    assert (run / "evidence/evidence-replay-originals/run-manifest.json").read_bytes() == previous_manifest
    current_manifest = json.loads((run / "run-manifest.json").read_text())
    current_manifest["origin_session_id"] = origin
    probe._atomic_json(run / "run-manifest.json", current_manifest)
    def service_identity(url):
        current = json.loads((run / "run-manifest.json").read_text())
        return {"run_id": current["run_id"], "database_identity": current["service_identities"]["target"], "source_fingerprints": current["source_fingerprints"], "task_worker_ready": True, "lightweight_worker_ready": True}
    async def belongs(*args):
        return True
    monkeypatch.setattr(probe, "_wait_json", service_identity)
    monkeypatch.setattr(probe, "_session_belongs_to_target", belongs)
    assert probe.check_readiness(run)["state"] == "ready"
    assert probe.check_readiness(run)["state"] == "ready"
    assert (run / "evidence/api-start-after-evidence-replay.json").exists()
    assert all(p.read_bytes() == raw for p, raw in originals.items())
    with pytest.raises(probe.ProbeError):
        probe.repair_evidence_replay(run, **kwargs)


@pytest.mark.parametrize("invalid", ["product", "ui-copy", "accounting", "case-identity", "prior-audit", "ownership", "spec-hash", "startup-hash"])
def test_evidence_replay_rejects_any_unexplained_source_or_ledger_drift_before_write(tmp_path, monkeypatch, invalid):
    run, spec, ui, origin, task, task_session, memory = _evidence_replay_fixture(tmp_path, monkeypatch)
    if invalid == "product":
        ui.write_text("drift")
    elif invalid == "ui-copy":
        (run / "ui-src/page.tsx").write_text("drift")
    elif invalid == "accounting":
        async def deny(*args):
            raise probe.ProbeError("unexplained tool/action/memory")
        monkeypatch.setattr(probe, "_validate_evidence_replay_accounting", deny)
    elif invalid == "case-identity":
        (run / "evidence/cases/remember.json").write_text("{}")
    elif invalid == "prior-audit":
        (run / "evidence/case-refresh-audit.json").write_text("{}")
    elif invalid == "ownership":
        monkeypatch.setattr(probe, "_owned_process_alive", lambda *args, **kwargs: False)
    monkeypatch.setattr(probe, "_stop_run_process", lambda *args, **kwargs: pytest.fail("must reject before stop"))
    with pytest.raises(probe.ProbeError):
        probe.repair_evidence_replay(run, reason="严格白名单", expected_e2e_sha256="0" * 64 if invalid == "spec-hash" else probe._digest(spec), expected_startup_sha256="0" * 64 if invalid == "startup-hash" else probe._digest(probe.ROOT / "hexis-ui/lib/tool-activity-startup.test.ts"), interrupted_trace=probe.ROOT / "hexis-ui/test-results/trace.zip", origin_session_id=origin, task_id=task, task_session_id=task_session, memory_id=memory)
    assert not (run / "evidence/evidence-replay-originals").exists()


@pytest.mark.parametrize("invalid", [None, "unknown-memory", "task-session", "missing-pair", "wrong-task", "wrong-work", "failed-recall"])
def test_evidence_replay_snapshot_uses_outer_task_identity_and_genuine_receipt_state(invalid):
    origin, task, session, memory, work = [str(uuid4()) for _ in range(5)]
    actions = [{"tool_name": "remember", "state": "done", "actual_call_id": "remember-call", "result": {"state": "done", "ok": True, "error": None, "data": {"memory_id": memory}}},
               {"tool_name": "submit", "state": "accepted", "result": {"accepted": True, "task_id": task, "task_session_id": session, "work_execution_id": work}}]
    events = []
    for tool, call in (("voice_recall", "recall-call"), ("remember", "remember-call")):
        for kind in ("TOOL_START", "TOOL_RESULT"):
            events.append({"session_id": origin, "event_type": kind, "payload": {"tool_name": tool, "call_id": call, "state": "done" if tool == "remember" and kind == "TOOL_RESULT" else None}})
    events.append({"session_id": origin, "event_type": "TOOL_RESULT", "payload": {"tool_name": "remember", "call_id": "remember-call", "state": "accepted"}})
    for index, tool in enumerate(("recall", "read_file", "read_file")):
        for kind in ("TOOL_START", "TOOL_RESULT"):
            events.append({"session_id": session, "task_id": task, "event_type": kind, "payload": {"tool_name": tool, "call_id": f"background-{index}", "execution_id": work, "success": index != 2 if kind == "TOOL_RESULT" else None}})
    snapshot = {"origin_session_id": origin, "actions": actions, "memories": [{"id": memory, "type": "semantic", "status": "active"}],
                "tasks": [{"id": task, "task_session_id": session, "origin_session_id": origin, "product_state": "idle"}],
                "work": [{"id": work, "task_id": task, "status": "completed"}], "events": events}
    if invalid == "unknown-memory":
        actions[0]["result"]["state"] = "unknown"
    elif invalid == "task-session":
        snapshot["tasks"][0]["task_session_id"] = origin
    elif invalid == "missing-pair":
        events.pop()
    elif invalid == "wrong-task":
        events[-1]["task_id"] = str(uuid4())
    elif invalid == "wrong-work":
        events[-1]["payload"]["execution_id"] = str(uuid4())
    elif invalid == "failed-recall":
        next(e for e in events if e.get("task_id") == task and e["event_type"] == "TOOL_RESULT")["payload"]["success"] = False
    if invalid is None:
        probe._validate_evidence_replay_snapshot(snapshot, origin, task, session, memory)
    else:
        with pytest.raises(probe.ProbeError):
            probe._validate_evidence_replay_snapshot(snapshot, origin, task, session, memory)


def _final_refresh_fixture(tmp_path, monkeypatch):
    run, spec, ui, origin, _task, _task_session, _memory = _evidence_replay_fixture(tmp_path, monkeypatch)
    manifest = json.loads((run / "run-manifest.json").read_text())
    original = run / "evidence/evidence-replay-originals/run-manifest.json"
    original.parent.mkdir()
    probe._exclusive_json(original, manifest)
    changes = {name: {"previous": manifest["source_fingerprints"][name], "current": probe._digest(probe.ROOT / name)} for name in ("apps/bob108_tool_activity_probe.py", "hexis-ui/e2e/tool-activity.spec.ts", "hexis-ui/lib/tool-activity-startup.test.ts")}
    audit = {"run_id": manifest["run_id"], "changes": changes, "original_manifest": {"path": str(original), "sha256": probe._digest(original)}, "previous_source_fingerprints": copy.deepcopy(manifest["source_fingerprints"]), "retained_original_evidence": {}}
    audit_path = run / "evidence/evidence-replay-audit.json"
    probe._exclusive_json(audit_path, audit)
    manifest["evidence_replay"] = {"path": str(audit_path), "sha256": probe._digest(audit_path)}
    for name, change in changes.items():
        manifest["source_fingerprints"][name] = change["current"]
    manifest["state"] = "verification_incomplete"
    manifest["origin_session_id"] = origin
    probe._atomic_json(run / "run-manifest.json", manifest)
    probe._exclusive_json(run / "evidence/verification-matrix.json", {"run_id": manifest["run_id"], "state": "verification_incomplete"})
    for file in (run / "evidence/cases").glob("*.json"):
        raw = json.loads(file.read_text())
        if "evidence" in raw:
            raw["evidence"]["source_fingerprints"] = manifest["source_fingerprints"]
        probe._atomic_json(file, raw)
    probe_tests = probe.ROOT / "tests/services/test_bob108_tool_activity_probe.py"
    probe_tests.parent.mkdir(parents=True)
    probe_tests.write_text("final gate regressions")
    (probe.ROOT / "apps/bob108_tool_activity_probe.py").write_text("nullable timestamp and final CLI")
    allowed = {name: probe._digest(probe.ROOT / name) for name in ("apps/bob108_tool_activity_probe.py", "tests/services/test_bob108_tool_activity_probe.py", "hexis-ui/e2e/tool-activity.spec.ts", "hexis-ui/lib/tool-activity-startup.test.ts")}
    async def valid(*args):
        return {"real_consent_active": True, "pending_work": False}
    monkeypatch.setattr(probe, "_validate_final_verification_target", valid, raising=False)
    monkeypatch.setattr(probe, "_http_json", lambda *args, **kwargs: {"run_id": manifest["run_id"], "database_identity": manifest["service_identities"]["target"], "source_fingerprints": manifest["source_fingerprints"]})
    return run, ui, allowed


def test_final_refresh_preserves_matrix_cases_and_registers_only_four_exact_sources(tmp_path, monkeypatch):
    run, _ui, allowed = _final_refresh_fixture(tmp_path, monkeypatch)
    originals = {p: p.read_bytes() for p in (run / "evidence").rglob("*") if p.is_file()}
    before = (run / "run-manifest.json").read_bytes()
    operations = []
    monkeypatch.setattr(probe, "_stop_run_process", lambda process, **kwargs: operations.append(("stop", kwargs["role"])))
    monkeypatch.setattr(probe, "serve_api", lambda path: operations.append(("start", "api")) or {"api_pid": 7})
    result = probe.refresh_final_verification(run, expected_source_hashes=allowed, reason="仅最终source receipts核验")
    assert operations == [("stop", "api"), ("start", "api")]
    assert result["provider_requested"] is False and result["real_e2e_rerun"] is False
    assert all(p.read_bytes() == raw for p, raw in originals.items())
    assert (run / "evidence/final-verification-originals/run-manifest.json").read_bytes() == before
    current = json.loads((run / "run-manifest.json").read_text())
    assert all(current["source_fingerprints"][name] == sha for name, sha in allowed.items())
    audit = json.loads((run / "evidence/final-verification-audit.json").read_text())
    assert audit["changes"]["tests/services/test_bob108_tool_activity_probe.py"]["previous"] is None
    def identity(url):
        m = json.loads((run / "run-manifest.json").read_text())
        return {"run_id": m["run_id"], "database_identity": m["service_identities"]["target"], "source_fingerprints": m["source_fingerprints"], "task_worker_ready": True, "lightweight_worker_ready": True}
    async def belongs(*args):
        return True
    monkeypatch.setattr(probe, "_wait_json", identity)
    monkeypatch.setattr(probe, "_session_belongs_to_target", belongs)
    assert probe.check_readiness(run)["state"] == "ready"
    assert probe.check_readiness(run)["state"] == "ready"
    assert (run / "evidence/api-start-after-final-verification.json").exists()
    assert all(p.read_bytes() == raw for p, raw in originals.items())
    with pytest.raises(probe.ProbeError):
        probe.refresh_final_verification(run, expected_source_hashes=allowed, reason="禁止重复")


@pytest.mark.parametrize("invalid", ["source-extra", "expected-hash", "product", "ui-copy", "real-consent", "pending-work", "owned-api", "db-identity", "matrix", "case-identity", "prior-audit"])
def test_final_refresh_fail_closed_before_archival_or_process_signals(tmp_path, monkeypatch, invalid):
    run, ui, allowed = _final_refresh_fixture(tmp_path, monkeypatch)
    if invalid == "source-extra":
        allowed["unapproved.py"] = "0" * 64
    elif invalid == "expected-hash":
        allowed["apps/bob108_tool_activity_probe.py"] = "0" * 64
    elif invalid == "product":
        ui.write_text("drift")
    elif invalid == "ui-copy":
        (run / "ui-src/page.tsx").write_text("drift")
    elif invalid in {"real-consent", "pending-work"}:
        async def deny(*args):
            raise probe.ProbeError("target consent/work gate failed")
        monkeypatch.setattr(probe, "_validate_final_verification_target", deny)
    elif invalid == "owned-api":
        monkeypatch.setattr(probe, "_owned_process_alive", lambda *args, **kwargs: False)
    elif invalid == "db-identity":
        monkeypatch.setattr(probe, "_http_json", lambda *args, **kwargs: {"run_id": "other"})
    elif invalid == "matrix":
        (run / "evidence/verification-matrix.json").write_text("{}")
    elif invalid == "case-identity":
        (run / "evidence/cases/remember.json").write_text("{}")
    else:
        (run / "evidence/evidence-replay-audit.json").write_text("{}")
    before = (run / "run-manifest.json").read_bytes()
    monkeypatch.setattr(probe, "_stop_run_process", lambda *args, **kwargs: pytest.fail("invalid inputs must never signal"))
    with pytest.raises(probe.ProbeError):
        probe.refresh_final_verification(run, expected_source_hashes=allowed, reason="严格source白名单")
    assert (run / "run-manifest.json").read_bytes() == before
    assert not (run / "evidence/final-verification-originals").exists()


@pytest.mark.parametrize("invalid", [None, "original-bytes", "archive-bytes", "producer-map", "product-drift", "owner"])
def test_historical_case_lineage_never_impersonates_current_source_execution(tmp_path, monkeypatch, invalid):
    run, ui, allowed = _final_refresh_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(probe, "_stop_run_process", lambda *args, **kwargs: None)
    monkeypatch.setattr(probe, "serve_api", lambda path: {"api_pid": 7})
    probe.refresh_final_verification(run, expected_source_hashes=allowed, reason="历史原件与当前checker分别记账")
    m = json.loads((run / "run-manifest.json").read_text())
    original = run / "evidence/cases/voice-recall.json"
    raw = json.loads(original.read_text())
    if invalid == "original-bytes":
        original.write_bytes(original.read_bytes() + b" ")
    elif invalid == "archive-bytes":
        archived = run / "evidence/final-verification-originals/evidence/cases/voice-recall.json"
        archived.write_bytes(archived.read_bytes() + b" ")
    elif invalid == "producer-map":
        raw["evidence"]["source_fingerprints"] = allowed
    elif invalid == "product-drift":
        ui.write_text("unapproved product drift")
    elif invalid == "owner":
        raw["origin_session_id"] = str(uuid4())
    if invalid is None:
        lineage = probe._historical_case_lineage(run, m, "voice-recall", raw)
        assert lineage["classification"] == "historical_attempt"
        assert lineage["assessment_source_fingerprints"] == m["source_fingerprints"]
        assert lineage["producer_source_fingerprints"] == raw["evidence"]["source_fingerprints"]
        assert lineage["provider_rerun"] is False
    else:
        with pytest.raises(probe.ProbeError):
            probe._historical_case_lineage(run, m, "voice-recall", raw)


def test_final_verifier_keeps_historical_lineage_even_when_case_validation_fails(tmp_path, monkeypatch):
    run, _ui, allowed = _final_refresh_fixture(tmp_path, monkeypatch)
    for file in (run / "evidence/cases").glob("*.json"):
        if file.name == "browser-run.json":
            continue
        raw = json.loads(file.read_text())
        raw["schema"] = "bob108-tool-activity-case-v1"
        probe._atomic_json(file, raw)
    monkeypatch.setattr(probe, "_stop_run_process", lambda *args, **kwargs: None)
    monkeypatch.setattr(probe, "serve_api", lambda path: {"api_pid": 7})
    probe.refresh_final_verification(run, expected_source_hashes=allowed, reason="失败结果仍保留历史出处")
    monkeypatch.setattr(probe, "verification_coverage", lambda *args, **kwargs: ({}, {"test_reports": [], "controlled_runs": []}))
    result = probe.verify_run(run)
    assert result["cases"]["voice-recall"]["status"] == "FAIL"
    assert result["cases"]["voice-recall"]["source_lineage"]["classification"] == "historical_attempt"
    assert result["cases"]["remember"]["source_lineage"]["provider_rerun"] is False
    assert result["state"] == "verification_incomplete"


def _resume_fixture(tmp_path, monkeypatch):
    root, material = tmp_path / "repo", tmp_path / "material"
    run = material / "runs/run-01"
    (run / "evidence").mkdir(parents=True)
    root.mkdir()
    files = ("product.py", "apps/bob108_tool_activity_probe.py", "tests/services/test_bob108_tool_activity_probe.py", "hexis-ui/e2e/tool-activity.spec.ts")
    hashes = {}
    for relative in files:
        source = root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("original source")
        hashes[relative] = probe._digest(source)
    (root / files[1]).write_text("resume CLI")
    (root / files[2]).write_text("resume regressions")
    (root / files[3]).write_text("approved text E2E")
    profile = {"provider": "openai-chat-completions-endpoint", "model": "qwen-plus", "endpoint": "https://provider.test/v1", "api_key_env": "DASHSCOPE_API_KEY"}
    ports = {"pytest": 45816, "browser": 45815, "api": 44817, "ui": 3577, "embeddings": 45435}
    runtime = {"database": "m5_acceptance_" + "a" * 32, "ports": ports, "provider_profile": profile,
               "pytest": {"host": "127.0.0.1", "port": 45816, "database": "bob108_pytest", "user": "user"},
               "target": {"host": "127.0.0.1", "port": 45815, "database": "m5_acceptance_" + "a" * 32, "admin_database": "postgres", "user": "user"},
               "api_url": "http://127.0.0.1:44817", "ui_url": "http://127.0.0.1:3577", "embedding_url": "http://127.0.0.1:45435/api/embed"}
    resources = {name: {"container_id": "id-" + name, "image_id": "image-" + name, "image_ref": "selected-image", "mounts": []} for name in ("pytest-db", "browser-db", "fake-embeddings")}
    manifest = {"schema": "bob108-tool-activity-run-v1", "run_id": "a" * 32, "run_directory": str(run), "state": "stopped", "project": "owned-project", "services": list(resources), "ports": ports, "volumes": {"pytest": "pytest-data", "browser": "browser-data"},
                "source_fingerprints": hashes, "runtime": runtime, "owned_resources": copy.deepcopy(resources), "api_process": None, "ui_process": None,
                "consent": {"state": "consent", "operator_override": False, "consent_log_id": str(uuid4())},
                "service_identities": {"pytest": {"database": "bob108_pytest", "system_identifier": "111"}, "browser_admin": {"database": "postgres", "system_identifier": "222"}, "target": {"database": runtime["database"], "system_identifier": "222"}}}
    probe._atomic_json(run / "run-manifest.json", manifest)
    old_voice = run / "evidence/add-evidence-voice-original.json"
    probe._exclusive_json(old_voice, {"case": "add-evidence", "input_mode": "voice", "status": "FAIL"})
    selected = {"run_id": manifest["run_id"], "selected_profile": profile, "configuration_hashes": {"config": "1" * 64}}
    selection = {"_config_hashes": selected["configuration_hashes"], "credential_env_file": str(tmp_path / "credentials.env")}
    values = {"POSTGRES_PASSWORD": "secret", "POSTGRES_USER": "user"}
    monkeypatch.setattr(probe, "ROOT", root)
    monkeypatch.setattr(probe, "MATERIAL_ROOT", material)
    monkeypatch.setattr(probe, "load_run", lambda path: (run, json.loads((run / "run-manifest.json").read_text()), selected, selection, values))
    monkeypatch.setattr(probe, "_process_command", lambda pid: None)
    monkeypatch.setattr(probe, "_resume_configuration_hashes", lambda *args: {"compose": "same", "runtime.env": "same"}, raising=False)
    statuses, operations = {name: "exited" for name in resources}, []
    def inspect(*args, **kwargs):
        return {"services": {name: {**record, "state": statuses[name]} for name, record in resources.items()}, "volumes": {"pytest": {"name": "owned-project_pytest-data"}, "browser": {"name": "owned-project_browser-data"}}}
    monkeypatch.setattr(probe, "_inspect_resume_resources", inspect, raising=False)
    def command(cmd, **kwargs):
        operations.append(cmd)
        if "start" in cmd:
            statuses.update({name: "running" for name in resources})
        elif cmd[:2] == ["docker", "stop"]:
            for cid in cmd[2:]:
                statuses[next(name for name in resources if resources[name]["container_id"] == cid)] = "exited"
        else:
            pytest.fail(f"unexpected resource command: {cmd}")
        return type("Result", (), {"stdout": "", "returncode": 0})()
    monkeypatch.setattr(probe.subprocess, "run", command)
    async def live(*args):
        return {"postgres": manifest["service_identities"], "real_consent_active": True, "embedding_healthy": True}
    monkeypatch.setattr(probe, "_validate_resumed_runtime", live, raising=False)
    def start_api(path):
        m = json.loads((run / "run-manifest.json").read_text())
        m["api_process"] = {"pid": 200, "run_id": m["run_id"]}
        probe._atomic_json(run / "run-manifest.json", m)
        operations.append(["serve-api"])
        return {"api_pid": 200}
    def start_ui(path):
        m = json.loads((run / "run-manifest.json").read_text())
        m["ui_process"] = {"pid": 201, "run_id": m["run_id"]}
        probe._atomic_json(run / "run-manifest.json", m)
        operations.append(["serve-ui"])
        return {"ui_pid": 201}
    def readiness(path):
        m = json.loads((run / "run-manifest.json").read_text())
        m["state"] = "ready"
        probe._atomic_json(run / "run-manifest.json", m)
        operations.append(["readiness"])
        return {"state": "ready"}
    monkeypatch.setattr(probe, "serve_api", start_api)
    monkeypatch.setattr(probe, "serve_ui", start_ui)
    monkeypatch.setattr(probe, "check_readiness", readiness)
    monkeypatch.setattr(probe, "_stop_run_process", lambda process, **kwargs: operations.append(["stop-process", kwargs["role"]]) if process else None)
    return run, manifest, resources, statuses, operations


def test_resume_owned_starts_only_existing_containers_then_owned_services_and_two_readiness_checks(tmp_path, monkeypatch):
    run, old, _resources, statuses, operations = _resume_fixture(tmp_path, monkeypatch)
    before = (run / "run-manifest.json").read_bytes()
    result = probe.resume_owned(run, source_files=["hexis-ui/e2e/tool-activity.spec.ts"], reason="用户批准text add_evidence，同一run恢复")
    assert result["state"] == "ready" and result["provider_requested"] is False and result["consent_requested"] is False
    assert operations[0][-4:] == ["start", "pytest-db", "browser-db", "fake-embeddings"]
    assert operations[1:] == [["serve-api"], ["serve-ui"], ["readiness"], ["readiness"]]
    assert all(status == "running" for status in statuses.values())
    assert (run / "evidence/resume-owned-originals/run-manifest.json").read_bytes() == before
    current = json.loads((run / "run-manifest.json").read_text())
    assert current["acceptance_modes"]["add-evidence"] == "text_chat"
    assert current["consent"] == old["consent"] and current["volumes"] == old["volumes"]
    with pytest.raises(probe.ProbeError):
        probe.resume_owned(run, source_files=["hexis-ui/e2e/tool-activity.spec.ts"], reason="禁止重复恢复")


@pytest.mark.parametrize("invalid", ["state", "run-dir", "override", "consent", "unapproved-source", "missing-spec-approval", "product", "config", "container-id", "running-container", "pid"])
def test_resume_owned_fail_closed_before_container_start(tmp_path, monkeypatch, invalid):
    run, manifest, _resources, statuses, operations = _resume_fixture(tmp_path, monkeypatch)
    sources = ["hexis-ui/e2e/tool-activity.spec.ts"]
    if invalid == "state":
        manifest["state"] = "ready"
    elif invalid == "run-dir":
        manifest["run_directory"] = str(tmp_path / "other")
    elif invalid == "override":
        manifest["consent"]["operator_override"] = True
    elif invalid == "consent":
        manifest["consent"]["state"] = "unknown"
    elif invalid == "unapproved-source":
        sources.append("product.py")
    elif invalid == "missing-spec-approval":
        sources = []
    elif invalid == "product":
        (probe.ROOT / "product.py").write_text("drift")
    elif invalid == "config":
        def deny(*args):
            raise probe.ProbeError("selected config hash changed")
        monkeypatch.setattr(probe, "_resume_configuration_hashes", deny)
    elif invalid == "container-id":
        manifest["owned_resources"]["browser-db"]["container_id"] = "unowned"
    elif invalid == "running-container":
        statuses["browser-db"] = "running"
    elif invalid == "pid":
        manifest["api_process"] = {"pid": 300, "run_id": manifest["run_id"]}
        monkeypatch.setattr(probe, "_process_command", lambda pid: "unrelated process")
    probe._atomic_json(run / "run-manifest.json", manifest)
    before = (run / "run-manifest.json").read_bytes()
    with pytest.raises(probe.ProbeError):
        probe.resume_owned(run, source_files=sources, reason="严格恢复门禁")
    assert operations == []
    assert (run / "run-manifest.json").read_bytes() == before
    assert not (run / "evidence/resume-owned-audit.json").exists()


@pytest.mark.parametrize("failure", ["partial-start", "runtime-db", "api", "ui", "readiness"])
def test_resume_owned_failure_stops_only_new_owned_resources_and_preserves_volumes(tmp_path, monkeypatch, failure):
    run, manifest, resources, statuses, operations = _resume_fixture(tmp_path, monkeypatch)
    if failure == "partial-start":
        def command(cmd, **kwargs):
            operations.append(cmd)
            if "start" in cmd:
                statuses["pytest-db"] = "running"
                raise probe.subprocess.CalledProcessError(1, cmd)
            assert cmd == ["docker", "stop", resources["pytest-db"]["container_id"]]
            statuses["pytest-db"] = "exited"
            return type("Result", (), {"stdout": "", "returncode": 0})()
        monkeypatch.setattr(probe.subprocess, "run", command)
    elif failure == "runtime-db":
        async def deny(*args):
            raise probe.ProbeError("real DB/consent/embedding mismatch")
        monkeypatch.setattr(probe, "_validate_resumed_runtime", deny)
    else:
        def deny(*args):
            raise probe.ProbeError("controlled startup failure")
        monkeypatch.setattr(probe, {"api": "serve_api", "ui": "serve_ui", "readiness": "check_readiness"}[failure], deny)
    with pytest.raises(probe.ProbeError):
        probe.resume_owned(run, source_files=["hexis-ui/e2e/tool-activity.spec.ts"], reason="失败仅回滚本次资源")
    assert all(status == "exited" for status in statuses.values())
    current = json.loads((run / "run-manifest.json").read_text())
    assert current["state"] == "stopped" and current["volumes"] == manifest["volumes"]
    assert all(not any(part in cmd for part in ("up", "build", "down", "-v", "reset", "recreate")) for cmd in operations)
    assert (run / "evidence/resume-owned-result.json").exists()


def test_resumed_runtime_derives_missing_pytest_user_from_existing_selected_credentials(tmp_path, monkeypatch):
    validator = probe._validate_resumed_runtime
    _run, manifest, _resources, _statuses, _operations = _resume_fixture(tmp_path, monkeypatch)
    manifest["runtime"]["pytest"].pop("user")
    before = copy.deepcopy(manifest["runtime"])
    async def ready(runtime, values):
        assert runtime["pytest"]["user"] == values["POSTGRES_USER"] == "user"
        return {"postgres": {"pytest": manifest["service_identities"]["pytest"], "target": manifest["service_identities"]["browser_admin"]}, "fake_embeddings_health": {"models": []}}
    async def identity(*args):
        return manifest["service_identities"]["target"]
    async def consent(*args):
        return {"real_consent_active": True}
    monkeypatch.setattr(probe, "_wait_owned_postgres", ready)
    monkeypatch.setattr(probe.control_runtime, "_pg_identity", identity)
    monkeypatch.setattr(probe, "_validate_final_verification_target", consent)
    result = asyncio.run(validator(manifest, {"POSTGRES_USER": "user", "POSTGRES_PASSWORD": "secret"}))
    assert result["real_consent_active"] is True
    assert manifest["runtime"] == before


def test_resume_failure_records_safe_stage_and_cause_without_exception_text(tmp_path, monkeypatch):
    run, _manifest, _resources, _statuses, _operations = _resume_fixture(tmp_path, monkeypatch)
    async def failure(*args):
        try:
            raise KeyError("user")
        except KeyError as cause:
            raise probe.ProbeError("owned run-01 PostgreSQL or fake embedding service did not become ready") from cause
    monkeypatch.setattr(probe, "_validate_resumed_runtime", failure)
    with pytest.raises(probe.ProbeError):
        probe.resume_owned(run, source_files=["hexis-ui/e2e/tool-activity.spec.ts"], reason="安全分类")
    result = json.loads((run / "evidence/resume-owned-result.json").read_text())
    assert result["error_stage"] == "runtime_check"
    assert result["cause_type"] == "KeyError"
    assert result["missing_binding_field"] == "user"
    assert result["error_classification"] == "database_or_embedding_timeout"
    assert "secret" not in (run / "evidence/resume-owned-result.json").read_text()


@pytest.mark.parametrize("invalid", [None, "rollback-incomplete", "provider-requested", "first-audit-changed", "first-result-missing"])
def test_resume_allows_exactly_one_audited_failed_attempt_retry_without_overwriting_history(tmp_path, monkeypatch, invalid):
    run, _manifest, _resources, statuses, _operations = _resume_fixture(tmp_path, monkeypatch)
    async def failure(*args):
        raise probe.ProbeError("owned run-01 PostgreSQL or fake embedding service did not become ready")
    monkeypatch.setattr(probe, "_validate_resumed_runtime", failure)
    with pytest.raises(probe.ProbeError):
        probe.resume_owned(run, source_files=["hexis-ui/e2e/tool-activity.spec.ts"], reason="首次受控失败")
    originals = {p: p.read_bytes() for p in (run / "evidence").rglob("*") if p.is_file()}
    result_path = run / "evidence/resume-owned-result.json"
    if invalid in {"rollback-incomplete", "provider-requested"}:
        result = json.loads(result_path.read_text())
        result["rollback_failures" if invalid == "rollback-incomplete" else "provider_requested"] = ["identity_changed"] if invalid == "rollback-incomplete" else True
        probe._atomic_json(result_path, result)
    elif invalid == "first-audit-changed":
        (run / "evidence/resume-owned-audit.json").write_text("{}")
    elif invalid == "first-result-missing":
        result_path.unlink()
    async def recovered(*args):
        return {"real_consent_active": True}
    monkeypatch.setattr(probe, "_validate_resumed_runtime", recovered)
    kwargs = {"source_files": ["hexis-ui/e2e/tool-activity.spec.ts"], "reason": "明确授权额外一次恢复", "retry_failed_once": True}
    if invalid is not None:
        with pytest.raises(probe.ProbeError):
            probe.resume_owned(run, **kwargs)
        assert all(status == "exited" for status in statuses.values())
        assert not (run / "evidence/resume-owned-retry-audit.json").exists()
    else:
        assert probe.resume_owned(run, **kwargs)["state"] == "ready"
        assert all(p.read_bytes() == raw for p, raw in originals.items())
        assert (run / "evidence/resume-owned-retry-audit.json").exists()
        assert (run / "evidence/resume-owned-retry-result.json").exists()
        with pytest.raises(probe.ProbeError):
            probe.resume_owned(run, **kwargs)


def test_port_selection_reselects_each_conflict_without_sharing_ports(monkeypatch):
    requested = {"api": 44817, "ui": 3577, "pytest": 45816, "browser": 45815, "embeddings": 45435}
    occupied = {44817, 3577, 45816, 45815, 45435}
    available = lambda port: port not in occupied

    selected = probe.choose_ports(requested, port_available=available)

    assert selected["api"] == 44818
    assert selected["ui"] == 3578
    assert selected["pytest"] == 45817
    assert selected["browser"] == 45818
    assert selected["embeddings"] == 45436
    assert len(set(selected.values())) == len(selected)


def test_target_database_name_is_run_scoped_and_never_the_source_database():
    run_id = uuid4().hex
    target = probe.target_database_name(run_id, source_database="hexis_memory")

    assert target == f"m5_acceptance_{run_id}"
    assert target != "hexis_memory"
    with pytest.raises(probe.ProbeError, match="source"):
        probe.validate_target_database("hexis_memory", source_database="hexis_memory", run_id=run_id)


def test_runtime_file_bindings_omit_secret_values_but_process_env_uses_selected_credentials():
    selection = {
        "credential_env_file": "/selected/.env",
        "realtime": {"path": "/config/qwen-realtime.json"},
        "input_gate": {"path": "/config/m4-voice-runtime.json"},
        "runtime_env_names": ["HEXIS_DATABASE_URL", "HEXIS_API_URL", "HEXIS_UI_ORIGIN"],
        "credentials": {"realtime_ref": "DASHSCOPE_API_KEY", "background_ref": "DASHSCOPE_API_KEY"},
    }
    profile = {
        "provider": "openai-chat-completions-endpoint",
        "model": "qwen-plus",
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
    }
    values = {
        "POSTGRES_HOST": "localhost",
        "POSTGRES_PORT": "43815",
        "POSTGRES_USER": "hexis_user",
        "POSTGRES_PASSWORD": "postgres-secret",
        "POSTGRES_DB": "hexis_memory",
        "DASHSCOPE_API_KEY": "provider-secret",
    }
    runtime, child = probe.build_runtime_bindings(
        selection,
        profile,
        values,
        run_id="a" * 32,
        ports={"api": 44817, "ui": 3577, "pytest": 45816, "browser": 45815, "embeddings": 45435},
    )

    serialized = str(runtime)
    assert "postgres-secret" not in serialized
    assert "provider-secret" not in serialized
    assert runtime["credential_env_file"] == "/selected/.env"
    assert runtime["credential_env_names"] == ["DASHSCOPE_API_KEY", "POSTGRES_PASSWORD"]
    assert child["POSTGRES_PASSWORD"] == "postgres-secret"
    assert child["DASHSCOPE_API_KEY"] == "provider-secret"
    assert child["POSTGRES_PORT"] == "45815"
    assert child["POSTGRES_DB"] == runtime["database"]
    assert runtime["target"]["admin_database"] == "postgres"
    assert child["HEXIS_DATABASE_URL"].endswith(f"/{runtime['database']}")


def test_run_compose_separates_database_services_and_never_inlines_credentials(monkeypatch):
    monkeypatch.setattr(probe.control_runtime, "_repository_postgres_config", lambda: {
        "image": "postgres-test:16",
        "context": str(probe.ROOT),
        "dockerfile": "ops/Dockerfile.db",
    })
    ports = {"api": 44817, "ui": 3577, "pytest": 45816, "browser": 45815, "embeddings": 45435}
    volumes = {"pytest": "run-pytest-pgdata", "browser": "run-browser-pgdata"}
    compose = probe.compose_text("hexis-bob108-run-01", "a" * 32, ports, volumes)

    assert "pytest-db:" in compose and "browser-db:" in compose and "fake-embeddings:" in compose
    assert 'POSTGRES_DB: bob108_pytest' in compose
    assert 'POSTGRES_DB: postgres' in compose
    assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}" in compose
    assert "provider-secret" not in compose and "postgres-secret" not in compose
    assert f"127.0.0.1:{ports['pytest']}:5432" in compose
    assert f"127.0.0.1:{ports['browser']}:5432" in compose


def test_only_real_matching_model_consent_opens_runtime_gate():
    profile = {
        "provider": "openai-chat-completions-endpoint",
        "model": "qwen-plus",
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
    }
    row = {"decision": "consent", "provider": profile["provider"], "model": profile["model"], "endpoint": profile["endpoint"], "operator_override": False}

    assert probe.validate_model_consent(row, profile) is True
    with pytest.raises(probe.ProbeError, match="decision"):
        probe.validate_model_consent({**row, "decision": "decline"}, profile)
    with pytest.raises(probe.ProbeError, match="does not match"):
        probe.validate_model_consent({**row, "model": "gpt-5.5"}, profile)
    with pytest.raises(probe.ProbeError, match="operator override"):
        probe.validate_model_consent({**row, "operator_override": True}, profile)


def test_load_selected_inputs_verifies_exact_local_files_and_uses_known_admin_default(tmp_path):
    realtime = _write_ref(tmp_path / "qwen-realtime.json", json.dumps({"api_key_env": "DASHSCOPE_API_KEY"}))
    gate = _write_ref(tmp_path / "m4-voice-runtime.json", json.dumps({"emotion": {"api_key_env": "DASHSCOPE_API_KEY"}}))
    playback = _write_ref(tmp_path / "playback.json", "{}")
    profile_body = {
        "schema": "bob108-background-llm-selection-v1",
        "配置来源": {"path": gate["path"], "sha256": gate["sha256"]},
        "profile": {
            "provider": "openai-chat-completions-endpoint",
            "model": "qwen-plus",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key_env": "DASHSCOPE_API_KEY",
        },
    }
    profile_file = tmp_path / "background-llm-profile.json"
    profile_file.write_text(json.dumps(profile_body), encoding="utf-8")
    profile_ref = {"path": str(profile_file), "sha256": hashlib.sha256(profile_file.read_bytes()).hexdigest()}
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_HOST=localhost\nPOSTGRES_PORT=43815\nPOSTGRES_USER=hexis_user\n"
        "POSTGRES_PASSWORD=postgres-secret\nPOSTGRES_DB=hexis_memory\nDASHSCOPE_API_KEY=provider-secret\n",
        encoding="utf-8",
    )
    selection_body = {
        "schema": "bob108-config-selection-v1",
        "source_root": str(tmp_path),
        "credential_env_file": str(env_file),
        "realtime": realtime,
        "input_gate": gate,
        "playback_coordination": playback,
        "background_llm_profile": profile_ref,
        "credentials": {
            "realtime_ref": "DASHSCOPE_API_KEY",
            "emotion_ref": "DASHSCOPE_API_KEY",
            "background_ref": "DASHSCOPE_API_KEY",
        },
    }
    selection_file = tmp_path / "config-selection.json"
    selection_file.write_text(json.dumps(selection_body), encoding="utf-8")
    audio = _write_ref(tmp_path / "input.wav", "synthetic test bytes")
    seed_file = _write_ref(tmp_path / "seed-data.json", "{}")
    manifest = {
        "source_head": "ee2c2e00d4c95fddf807d287b16ab6105e37d2cf",
        "audio": [audio],
        "data_files": [
            {"path": str(selection_file), "sha256": hashlib.sha256(selection_file.read_bytes()).hexdigest()},
            profile_ref,
            seed_file,
        ],
    }
    manifest_file = tmp_path / "materials-manifest.json"
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

    selection, profile, values, loaded_manifest = probe.load_selected_inputs(
        selection_file, profile_file, materials_path=manifest_file, repo_root=tmp_path, strict_material_hashes=False
    )

    assert profile["model"] == "qwen-plus"
    assert values["POSTGRES_ADMIN_DB"] == "postgres"
    assert selection["_admin_database_source"] == "tests/conftest.py default"
    assert values["DASHSCOPE_API_KEY"] == "provider-secret"
    assert loaded_manifest["source_head"] == manifest["source_head"]


def test_load_selected_inputs_rejects_a_changed_selected_profile_hash(tmp_path):
    profile = {
        "schema": "bob108-background-llm-selection-v1",
        "配置来源": {"path": "/config/m4.json", "sha256": "a" * 64},
        "profile": {"provider": "openai-chat-completions-endpoint", "model": "qwen-plus", "endpoint": "https://example.test/v1", "api_key_env": "DASHSCOPE_API_KEY"},
    }
    profile_file = tmp_path / "profile.json"
    profile_file.write_text(json.dumps(profile), encoding="utf-8")
    selection = {
        "schema": "bob108-config-selection-v1",
        "source_root": str(probe.ROOT),
        "credential_env_file": str(tmp_path / ".env"),
        "background_llm_profile": {"path": str(profile_file), "sha256": "b" * 64},
    }
    selection_file = tmp_path / "selection.json"
    selection_file.write_text(json.dumps(selection), encoding="utf-8")

    with pytest.raises(probe.ProbeError, match="background profile"):
        probe.load_selected_inputs(selection_file, profile_file, materials_path=tmp_path / "missing.json", strict_material_hashes=False)


def test_load_run_uses_prepared_selected_runtime_contract_without_manifest_selected_config(tmp_path, monkeypatch):
    run_id = "a" * 32
    profile = {"model": "qwen-plus"}
    config = tmp_path / "config-selection.json"
    config.write_text("{}", encoding="utf-8")
    hashes = {"/config/realtime.json": "b" * 64}
    manifest = {
        "schema": "bob108-tool-activity-run-v1",
        "run_id": run_id,
        "run_directory": str(tmp_path),
        "runtime": {"database": f"m5_acceptance_{run_id}", "source": {"database": "hexis_memory"}},
    }
    selected = {
        "schema": "bob108-selected-runtime-v1",
        "run_id": run_id,
        "config_selection_path": str(config),
        "background_profile_path": "/selected/profile.json",
        "configuration_hashes": hashes,
        "selected_profile": profile,
    }
    (tmp_path / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "selected-runtime.json").write_text(json.dumps(selected), encoding="utf-8")
    calls = []

    def load_inputs(config_path, profile_path):
        calls.append((config_path, profile_path))
        return {"_config_hashes": hashes}, profile, {"POSTGRES_PASSWORD": "memory-only-secret"}, {}

    monkeypatch.setattr(probe, "load_selected_inputs", load_inputs)
    loaded = probe.load_run(tmp_path)

    assert loaded[1] == manifest
    assert calls == [(str(config), "/selected/profile.json")]
    assert loaded[4]["POSTGRES_PASSWORD"] == "memory-only-secret"
    assert "memory-only-secret" not in (tmp_path / "run-manifest.json").read_text()

    selected["configuration_hashes"] = {"/config/realtime.json": "c" * 64}
    (tmp_path / "selected-runtime.json").write_text(json.dumps(selected), encoding="utf-8")
    with pytest.raises(probe.ProbeError, match="configuration hashes"):
        probe.load_run(tmp_path)


@pytest.mark.parametrize("state", ["awaiting_consent", "consent_error"])
def test_pytest_command_accepts_run_admin_identity_and_binds_only_owned_pytest_environment(tmp_path, monkeypatch, state):
    pytest_identity = {"database": "bob108_pytest", "system_identifier": "111"}
    browser_identity = {"database": "postgres", "system_identifier": "222"}
    target_identity = {"database": "m5_acceptance_test", "system_identifier": "222"}
    resources = {service: {"container_id": service + "-owned"} for service in ("pytest-db", "browser-db", "fake-embeddings")}
    manifest = {
        "state": state,
        "ports": {"pytest": 45819, "browser": 45818, "embeddings": 45437},
        "runtime": {
            "database": "m5_acceptance_test",
            "pytest": {"host": "127.0.0.1", "port": 45819, "database": "bob108_pytest", "admin_database": "postgres"},
            "target": {"host": "127.0.0.1", "port": 45818, "database": "m5_acceptance_test", "user": "selected_user", "admin_database": "postgres"},
        },
        "service_identities": {"pytest": pytest_identity, "browser_admin": browser_identity, "target": target_identity},
        "owned_resources": resources,
    }
    values = {"POSTGRES_USER": "selected_user", "POSTGRES_PASSWORD": "child-only-secret", "DASHSCOPE_API_KEY": "provider-secret"}
    monkeypatch.setattr(probe, "load_run", lambda path: (tmp_path, manifest, {}, {}, values))
    monkeypatch.setattr(probe.control_runtime, "_owned_resources", lambda path, record: resources)
    observed = []
    wrong_browser = [False]

    async def identity(*args):
        observed.append(args)
        if wrong_browser[0] and args[2] != "bob108_pytest":
            return {"database": "hexis_memory", "system_identifier": "main"}
        return dict(pytest_identity if args[2] == "bob108_pytest" else target_identity)

    monkeypatch.setattr(probe.control_runtime, "_pg_identity", identity)
    child_calls = []

    def launch(command, **kwargs):
        child_calls.append((command, kwargs))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(probe.subprocess, "run", launch)
    monkeypatch.setenv("HEXIS_DATABASE_URL", "postgresql://source-secret@ambient/hexis_memory")
    monkeypatch.setenv("POSTGRES_DB", "hexis_memory")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "ambient-provider-secret")

    assert probe.pytest_command(tmp_path, ["--", "tests/services/test_bob108_tool_activity_probe.py", "-q"]) == 0
    env = child_calls[0][1]["env"]
    assert observed == [
        ("127.0.0.1", 45818, "m5_acceptance_test", "selected_user", "child-only-secret"),
        ("127.0.0.1", 45819, "bob108_pytest", "selected_user", "child-only-secret"),
    ]
    assert env["POSTGRES_HOST"] == "127.0.0.1"
    assert env["POSTGRES_PORT"] == "45819"
    assert env["POSTGRES_DB"] == "bob108_pytest"
    assert env["POSTGRES_PASSWORD"] == "child-only-secret"
    assert env["FAKE_EMBEDDINGS_PORT"] == "45437"
    assert "HEXIS_DATABASE_URL" not in env and "DASHSCOPE_API_KEY" not in env
    assert "HEXIS_BOB108_RUN_MANIFEST" not in env and "HEXIS_API_URL" not in env
    assert manifest["state"] == state
    assert list(tmp_path.iterdir()) == []

    pytest_identity["system_identifier"] = "222"
    with pytest.raises(probe.ProbeError, match="share a system identifier"):
        probe.pytest_command(tmp_path, ["-q"])
    assert len(child_calls) == 1

    pytest_identity["system_identifier"] = "111"
    manifest["runtime"]["target"]["port"] = 43815
    with pytest.raises(probe.ProbeError, match="browser runtime binding"):
        probe.pytest_command(tmp_path, ["-q"])
    assert len(child_calls) == 1

    manifest["runtime"]["target"]["port"] = 45818
    wrong_browser[0] = True
    with pytest.raises(probe.ProbeError, match="browser PostgreSQL identity"):
        probe.pytest_command(tmp_path, ["-q"])
    assert len(child_calls) == 1


@pytest.mark.parametrize("operation", ["serve_api", "serve_ui", "request_consent"])
def test_consent_error_allows_no_service_start_or_provider_retry(tmp_path, monkeypatch, operation):
    manifest = {"state": "consent_error"}
    monkeypatch.setattr(probe, "load_run", lambda path: (tmp_path, manifest, {}, {}, {}))

    def forbidden(*args, **kwargs):
        pytest.fail("consent_error must not start services, write evidence, or construct runtime credentials")

    monkeypatch.setattr(probe.subprocess, "Popen", forbidden)
    monkeypatch.setattr(probe, "_atomic_json", forbidden)
    monkeypatch.setattr(probe, "_child_environment", forbidden)
    with pytest.raises(probe.ProbeError):
        getattr(probe, operation)(tmp_path)
    assert manifest["state"] == "consent_error"
    assert list(tmp_path.iterdir()) == []


def test_request_consent_reuses_prepared_selected_runtime_validation_before_database_access(tmp_path, monkeypatch):
    import asyncpg
    from core import init_api

    run_id = "d" * 32
    database = f"m5_acceptance_{run_id}"
    profile = {"provider": "selected-provider", "model": "qwen-plus", "endpoint": "https://provider.test/v1", "api_key_env": "DASHSCOPE_API_KEY"}
    hashes = {"/config/realtime.json": "e" * 64}
    manifest = {
        "schema": "bob108-tool-activity-run-v1",
        "run_id": run_id,
        "run_directory": str(tmp_path),
        "state": "awaiting_consent",
        "runtime": {
            "database": database,
            "source": {"database": "hexis_memory"},
            "target": {"host": "127.0.0.1", "port": 45818, "database": database, "user": "selected_user"},
        },
    }
    selected = {
        "schema": "bob108-selected-runtime-v1",
        "run_id": run_id,
        "config_selection_path": "/selected/config-selection.json",
        "background_profile_path": "/selected/profile.json",
        "configuration_hashes": hashes,
        "selected_profile": profile,
    }
    manifest_path = tmp_path / "run-manifest.json"
    selected_path = tmp_path / "selected-runtime.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    selected_path.write_text(json.dumps(selected), encoding="utf-8")
    before = manifest_path.read_bytes()
    input_calls = []
    connection_calls = []

    def load_inputs(selection_path, profile_path):
        input_calls.append((selection_path, profile_path))
        return {"_config_hashes": hashes}, profile, {"POSTGRES_PASSWORD": "memory-only-secret"}, {}

    class ConnectionCheckpoint(Exception):
        pass

    async def connect(dsn, **kwargs):
        connection_calls.append((dsn, kwargs))
        raise ConnectionCheckpoint("stopped before database or provider access")

    async def forbidden_flow(*args, **kwargs):
        pytest.fail("provider consent flow must not run in this preflight regression")

    monkeypatch.setattr(probe, "load_selected_inputs", load_inputs)
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(init_api, "run_consent_flow", forbidden_flow)
    with pytest.raises(ConnectionCheckpoint):
        asyncio.run(probe._request_consent_async(tmp_path))

    assert input_calls == [("/selected/config-selection.json", "/selected/profile.json")]
    assert connection_calls == [(f"postgresql://selected_user:memory-only-secret@127.0.0.1:45818/{database}", {"timeout": 5})]
    assert manifest_path.read_bytes() == before
    assert not (tmp_path / "evidence").exists()

    selected["configuration_hashes"] = {"/config/realtime.json": "f" * 64}
    selected_path.write_text(json.dumps(selected), encoding="utf-8")
    with pytest.raises(probe.ProbeError, match="configuration hashes"):
        asyncio.run(probe._request_consent_async(tmp_path))
    assert len(connection_calls) == 1
    assert manifest_path.read_bytes() == before

    selected["configuration_hashes"] = hashes
    selected_path.write_text(json.dumps(selected), encoding="utf-8")
    manifest["state"] = "consent_accepted"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(probe.ProbeError, match="awaiting a real model decision"):
        asyncio.run(probe._request_consent_async(tmp_path))
    assert len(connection_calls) == 1


@pytest.mark.parametrize("history", ["api.log", "ui.log", "cases/one.json", "empty-cases", None])
def test_refresh_launcher_checks_execution_history_before_database_access(tmp_path, monkeypatch, history):
    import asyncpg

    manifest = {
        "state": "awaiting_consent",
        "consent": {"state": "required"},
        "api_process": None,
        "ui_process": None,
        "runtime": {"target": {"host": "127.0.0.1", "port": 45818, "database": "m5_acceptance_test", "admin_database": "postgres", "user": "selected_user"}},
    }
    monkeypatch.setattr(probe, "load_run", lambda path: (tmp_path, manifest, {}, {}, {"POSTGRES_PASSWORD": "memory-only-secret"}))
    evidence = tmp_path / "evidence"
    if history == "empty-cases":
        (evidence / "cases").mkdir(parents=True)
    elif history is not None:
        artifact = evidence / history
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("", encoding="utf-8")

    class ConnectionCheckpoint(Exception):
        pass

    connection_calls = []

    async def connect(*args, **kwargs):
        connection_calls.append((args, kwargs))
        raise ConnectionCheckpoint("stopped before database access")

    monkeypatch.setattr(asyncpg, "connect", connect)
    if history in {"api.log", "ui.log", "cases/one.json"}:
        with pytest.raises(probe.ProbeError, match="after API/UI execution or case evidence"):
            asyncio.run(probe._refresh_launcher_fingerprints_async(tmp_path))
        assert connection_calls == []
    else:
        with pytest.raises(ConnectionCheckpoint):
            asyncio.run(probe._refresh_launcher_fingerprints_async(tmp_path))
        assert len(connection_calls) == 1
    assert manifest["state"] == "awaiting_consent"
    assert not (tmp_path / "run-manifest.json").exists()
    assert not (evidence / "launcher-fingerprint-refresh.json").exists()


@pytest.mark.parametrize("scenario", ["no-consent", "existing-consent", "wrong-identity"])
def test_refresh_launcher_queries_consent_only_in_owned_target_database(tmp_path, monkeypatch, scenario):
    import asyncpg

    run_id = "a" * 32
    database = f"m5_acceptance_{run_id}"
    manifest = {
        "run_id": run_id,
        "state": "awaiting_consent",
        "consent": {"state": "required", "provider": "selected-provider", "model": "qwen-plus", "endpoint": "https://provider.test/v1"},
        "runtime": {"database": database, "target": {"host": "127.0.0.1", "port": 45818, "database": database, "admin_database": "postgres", "user": "selected_user"}},
        "service_identities": {"target": {"database": database, "system_identifier": "222"}},
        "source_fingerprints": {"product.py": "product-hash"},
    }
    monkeypatch.setattr(probe, "load_run", lambda path: (tmp_path, manifest, {}, {}, {"POSTGRES_PASSWORD": "memory-only-secret"}))
    source_root = tmp_path / "sources"
    for relative in ("product.py", "apps/bob108_tool_activity_probe.py", "hexis-ui/e2e/tool-activity.spec.ts"):
        source = source_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("controlled offline source", encoding="utf-8")
    monkeypatch.setattr(probe, "ROOT", source_root)
    monkeypatch.setattr(probe, "_digest", lambda path: "product-hash" if path.name == "product.py" else "launcher-hash")
    writes = []
    monkeypatch.setattr(probe, "_atomic_json", lambda path, value: writes.append((path, value)))
    queries = []
    closed = []

    class Connection:
        async def fetchrow(self, query):
            queries.append(("identity", query))
            return {"database": "postgres" if scenario == "wrong-identity" else database, "system_identifier": "222"}

        async def fetchval(self, query, *args):
            queries.append(("consent", query, args))
            assert args == ("selected-provider", "qwen-plus", "https://provider.test/v1")
            return scenario == "existing-consent"

        async def close(self):
            closed.append(True)

    async def connect(dsn, **kwargs):
        assert dsn == f"postgresql://selected_user:memory-only-secret@127.0.0.1:45818/{database}"
        return Connection()

    monkeypatch.setattr(asyncpg, "connect", connect)
    if scenario == "wrong-identity":
        with pytest.raises(probe.ProbeError, match="owned.*target PostgreSQL"):
            asyncio.run(probe._refresh_launcher_fingerprints_async(tmp_path))
        assert [item[0] for item in queries] == ["identity"]
        assert writes == []
    elif scenario == "existing-consent":
        with pytest.raises(probe.ProbeError, match="target consent already exists"):
            asyncio.run(probe._refresh_launcher_fingerprints_async(tmp_path))
        assert [item[0] for item in queries] == ["identity", "consent"]
        assert writes == []
    else:
        history = {"run_id": run_id, "reason": "Historical English reason", "previous": {"product.py": "product-hash"}}
        (tmp_path / "evidence").mkdir()
        (tmp_path / "evidence" / "launcher-fingerprint-refresh.json").write_text(json.dumps(history), encoding="utf-8")
        result = asyncio.run(probe._refresh_launcher_fingerprints_async(tmp_path))
        assert result["run_id"] == run_id
        assert "同意" in result["reason"] and "指纹" in result["reason"]
        assert "历史" in result["historical_record_note"] and "中文" in result["historical_record_note"]
        assert writes[0][1]["history"][0] == history
        assert [item[0] for item in queries] == ["identity", "consent"]
        assert len(writes) == 2
        assert "memory-only-secret" not in repr(writes)
    assert closed == [True]


def test_snapshot_projects_real_action_and_work_schema_in_readonly_transaction(tmp_path, monkeypatch):
    import asyncpg

    origin = uuid4()
    database = "m5_acceptance_test"
    work_id = str(uuid4())
    message_id = str(uuid4())
    task_session_id = uuid4()
    raw_message = {
        "id": message_id, "session_id": str(origin), "ordinal": 1, "role": "toolResult", "content": "原始工具正文",
        "content_blocks": [{"type": "text", "text": "原始工具正文"}],
        "source_message_id": "tool:actual-call:result", "metadata": {"tool_result": {"toolCallId": "actual-call"}},
    }
    raw_task_message = {**copy.deepcopy(raw_message), "id": str(uuid4()), "session_id": str(task_session_id)}
    raw_event = {"event_id": str(uuid4()), "session_id": str(origin), "stream_id": "voice", "seq": 1, "event_type": "TOOL_RESULT", "payload": {"state": "accepted"}, "ts": None, "recorded_at": "2026-10-09T00:00:00+00:00", "task_id": None}
    manifest = {
        "run_id": "a" * 32,
        "runtime": {"database": database, "target": {"host": "127.0.0.1", "port": 45818, "database": database, "user": "selected_user"}},
        "service_identities": {"target": {"system_identifier": "222"}},
    }
    monkeypatch.setattr(probe, "load_run", lambda path: (tmp_path, manifest, {}, {}, {"POSTGRES_PASSWORD": "memory-only-secret"}))
    queries = []
    transaction_options = []
    closed = []

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Connection:
        async def set_type_codec(self, *args, **kwargs):
            pass

        def transaction(self, **kwargs):
            transaction_options.append(kwargs)
            return Transaction()

        async def fetchrow(self, query):
            return {"database": database, "system_identifier": "222"}

        async def fetchval(self, query, *args):
            return True

        async def fetch(self, query, *args):
            queries.append(query)
            if "FROM m5_tasks" in query:
                return [{"id": str(uuid4()), "task_session_id": str(task_session_id), "product_state": "working", "origin_session_id": str(origin)}]
            if "FROM chat_messages" in query:
                assert args == (origin, [task_session_id])
                assert "session_id=$1::uuid" in query and "ORDER BY session_id,ordinal" in query
                assert "content_blocks" in query and "source_message_id" in query
                assert "session_id=ANY($2::uuid[])" in query
                return [copy.deepcopy(raw_message), copy.deepcopy(raw_task_message)]
            if "FROM m5_actions" in query:
                assert "result->'error' AS error" in query
                return [{"command_id": str(uuid4()), "state": "failed", "result": {"error": "controlled failure"}, "error": "controlled failure"}]
            if "FROM behavior_events" in query:
                assert "payload,ts,recorded_at,task_id" in query
                return [copy.deepcopy(raw_event)]
            if "FROM m5_work_rounds" in query:
                assert "w.execution_id" not in query and "w.completed_at" not in query
                assert "w.id::text AS execution_id" in query
                assert "w.finished_at AS completed_at" in query
                return [{"id": work_id, "execution_id": work_id, "status": "completed", "completed_at": "controlled-time"}]
            return []

        async def close(self):
            closed.append(True)

    async def connect(dsn, **kwargs):
        assert dsn.endswith(f"@127.0.0.1:45818/{database}")
        return Connection()

    monkeypatch.setattr(asyncpg, "connect", connect)
    result = asyncio.run(probe._snapshot_async(tmp_path, str(origin)))

    assert result["work"][0]["execution_id"] == work_id
    assert result["work"][0]["completed_at"] == "controlled-time"
    assert result["actions"][0]["error"] == "controlled failure"
    assert result["chat_messages"] == [raw_message, raw_task_message]
    assert result["events"] == [raw_event]
    assert result["events"][0]["ts"] is None
    assert transaction_options == [{"isolation": "repeatable_read", "readonly": True}]
    assert all(query.startswith("SELECT ") for query in queries)
    assert closed == [True]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("scenario", ["done", "accepted", "unknown", "receipt-accepted", "flat-only", "invalid-uuid", "missing-db-memory"])
def test_snapshot_reads_only_authoritative_done_receipt_data_uuid_and_real_memory(tmp_path, monkeypatch, scenario):
    import asyncpg
    from contextlib import asynccontextmanager

    memory_id, origin = uuid4(), uuid4()
    receipt = {"state": "done", "ok": True, "error": None, "data": {"memory_id": str(memory_id), "write_effect": "created"}, "memory_id": str(uuid4())}
    action = {"tool_name": "remember", "state": "done", "result": receipt}
    if scenario in {"accepted", "unknown"}:
        action["state"] = scenario
        receipt["state"] = scenario
    elif scenario == "receipt-accepted":
        receipt["state"] = "accepted"
    elif scenario == "flat-only":
        receipt.pop("data")
    elif scenario == "invalid-uuid":
        receipt["data"]["memory_id"] = "fabricated-memory"
    manifest = {"run_id": "a" * 32, "runtime": {"database": "owned", "target": {"host": "127.0.0.1", "port": 40000, "database": "owned", "user": "user"}}, "service_identities": {"target": {"system_identifier": "123"}}}
    monkeypatch.setattr(probe, "load_run", lambda path: (tmp_path, manifest, {}, {}, {"POSTGRES_PASSWORD": "secret"}))
    memory_queries = []
    class Connection:
        async def set_type_codec(self, *args, **kwargs):
            pass
        @asynccontextmanager
        async def transaction(self, **kwargs):
            assert kwargs == {"isolation": "repeatable_read", "readonly": True}
            yield
        async def fetchrow(self, query):
            return {"database": "owned", "system_identifier": "123"}
        async def fetchval(self, *args):
            return True
        async def fetch(self, query, *args):
            if "FROM m5_actions" in query:
                return [action]
            if "FROM memories" in query:
                memory_queries.append(args[0])
                return [] if scenario == "missing-db-memory" else [{"id": str(memory_id), "type": "semantic", "status": "active"}]
            if "FROM belief_revision_audit" in query:
                assert args[0] == ([memory_id] if scenario == "done" else [])
                return [{"memory_id": str(memory_id)}] if scenario == "done" else []
            return []
        async def close(self):
            pass
    async def connect(*args, **kwargs):
        return Connection()
    monkeypatch.setattr(asyncpg, "connect", connect)
    if scenario in {"flat-only", "invalid-uuid", "missing-db-memory"}:
        with pytest.raises(probe.ProbeError):
            asyncio.run(probe._snapshot_async(tmp_path, str(origin)))
    else:
        snapshot = asyncio.run(probe._snapshot_async(tmp_path, str(origin)))
        assert snapshot["actions"] == [action]
        assert snapshot["memories"] == ([{"id": str(memory_id), "type": "semantic", "status": "active"}] if scenario == "done" else [])
    assert memory_queries == ([[memory_id]] if scenario in {"done", "missing-db-memory"} else [])


@pytest.mark.parametrize("scenario", ["text-no-actions", "accepted", "unknown", "bad-uuid", "missing-memory"])
def test_snapshot_explicit_memory_selection_is_readonly_without_inventing_done_actions(tmp_path, monkeypatch, scenario):
    import asyncpg
    from contextlib import asynccontextmanager
    origin, memory = uuid4(), uuid4()
    manifest = {"run_id": "a" * 32, "runtime": {"database": "owned", "target": {"host": "127.0.0.1", "port": 40000, "database": "owned", "user": "user"}}, "service_identities": {"target": {"system_identifier": "123"}}}
    monkeypatch.setattr(probe, "load_run", lambda path: (tmp_path, manifest, {}, {}, {"POSTGRES_PASSWORD": "secret"}))
    connections, selects = [], []
    actions = [] if scenario == "text-no-actions" else [{"tool_name": "remember", "state": scenario, "result": {"state": scenario, "data": {"memory_id": str(memory)}}}]
    class Connection:
        async def set_type_codec(self, *args, **kwargs):
            pass
        @asynccontextmanager
        async def transaction(self, **kwargs):
            assert kwargs["readonly"] is True
            yield
        async def fetchrow(self, query):
            return {"database": "owned", "system_identifier": "123"}
        async def fetchval(self, *args):
            return True
        async def fetch(self, query, *args):
            selects.append(query)
            if "FROM m5_actions" in query:
                return actions
            if "FROM memories" in query:
                assert args[0] == [memory]
                return [] if scenario == "missing-memory" else [{"id": str(memory), "type": "semantic", "status": "active"}]
            if "FROM belief_revision_audit" in query:
                assert args[0] == [memory]
                return [{"memory_id": str(memory), "evidence": {"source": "actual stored source"}}]
            return []
        async def close(self):
            pass
    async def connect(*args, **kwargs):
        connections.append(True)
        return Connection()
    monkeypatch.setattr(asyncpg, "connect", connect)
    if scenario in {"bad-uuid", "missing-memory"}:
        with pytest.raises(probe.ProbeError):
            asyncio.run(probe._snapshot_async(tmp_path, str(origin), memory_ids=["bad"] if scenario == "bad-uuid" else [str(memory)]))
    else:
        result = asyncio.run(probe._snapshot_async(tmp_path, str(origin), memory_ids=[str(memory), str(memory)]))
        assert result["actions"] == actions
        assert result["memories"][0]["id"] == str(memory)
        assert result["belief_revision_audit"][0]["memory_id"] == str(memory)
    if scenario == "bad-uuid":
        assert connections == []
    assert all(query.startswith("SELECT") for query in selects)


def _prepared_case_evidence(tmp_path, monkeypatch):
    source_root = tmp_path / "sources"
    source_root.mkdir()
    source = source_root / "product.py"
    source.write_text("controlled source", encoding="utf-8")
    monkeypatch.setattr(probe, "ROOT", source_root)
    run_id = "a" * 32
    origin = str(uuid4())
    task_session = str(uuid4())
    task_id, work_id = str(uuid4()), str(uuid4())
    database = f"m5_acceptance_{run_id}"
    fingerprints = {"product.py": probe._digest(source)}
    manifest = {
        "run_id": run_id, "state": "ready", "source_fingerprints": fingerprints,
        "runtime": {"database": database},
        "service_identities": {"target": {"database": database, "system_identifier": "222"}},
    }
    cases_dir = tmp_path / "evidence" / "cases"
    cases_dir.mkdir(parents=True)
    all_cases = {}
    memory_id = str(uuid4())
    names = {"voice-recall": ["voice_recall"], "remember": ["remember"], "add-evidence": ["add_evidence"], "background-tools": ["recall", "read_file", "read_file"]}
    for name, tools in names.items():
        session = task_session if name == "background-tools" else origin
        events = []
        for index, tool in enumerate(tools):
            call_id = f"{name}-call-{index}"
            for kind in ("TOOL_START", "TOOL_RESULT"):
                payload = {"tool_name": tool, "call_id": call_id, "execution_id": work_id, "task_id": task_id}
                if kind == "TOOL_RESULT":
                    payload.update({"success": index != 2, "state": "done", "output": {}})
                events.append({"event_id": str(uuid4()), "session_id": session, "stream_id": "tool-stream", "seq": len(events) + 1, "event_type": kind, "payload": payload, "ts": "2026-10-09T00:00:00Z"})
        screenshot = cases_dir / f"{name}.png"
        screenshot.write_bytes(b"controlled image fixture")
        wav = source_root / f"{name}.wav"
        wav.write_bytes(b"controlled wav fixture")
        snapshot = {
            "run_id": run_id, "database": database, "system_identifier": "222", "origin_session_id": origin,
            "events": copy.deepcopy(events),
            "tasks": [{"id": task_id, "task_session_id": task_session, "product_state": "working"}],
            "work": [{"id": work_id, "execution_id": work_id, "task_id": task_id, "status": "running"}],
        }
        evidence = {
            "call_ids": sorted({event["payload"]["call_id"] for event in events}),
            "event_ids": [event["event_id"] for event in events],
            "activity_session_id": session, "activity_events": events, "activity_dom": " ".join(tools),
            "screenshot": str(screenshot), "screenshot_sha256": probe._digest(screenshot),
            "snapshot": snapshot, "api_cursor": {"tool-stream": len(events)},
            "source_fingerprints": fingerprints,
            "wav": {"path": str(wav), "sha256": probe._digest(wav)},
        }
        if name == "background-tools":
            messages = [{
                "id": str(uuid4()), "session_id": task_session, "ordinal": 1, "role": "assistant", "content": "",
                "content_blocks": [{"type": "toolCall", "id": "call", "name": "read_file", "arguments": {"path": "README.md"}}],
                "source_message_id": "tool:call:start", "metadata": {},
            }, {
                "id": str(uuid4()), "session_id": task_session, "ordinal": 2, "role": "toolResult", "content": "工具消息保持原样",
                "content_blocks": [{"type": "text", "text": "工具消息保持原样"}],
                "source_message_id": "tool:call:result", "metadata": {"tool_result": {"toolCallId": "call"}},
            }]
            snapshot["chat_messages"] = copy.deepcopy(messages)
            summary = _test_original_messages_summary(messages)
            evidence.update({
                "task": {"task_id": task_id, "task_session_id": task_session},
                "task_activity": events,
                "observed_event_state_snapshots": [{"event": event, "snapshot": copy.deepcopy(snapshot), "observed_at": "2026-10-09T00:00:00Z"} for event in events],
                "original_tool_messages": {"before": copy.deepcopy(summary), "after": copy.deepcopy(summary),
                    "before_messages": copy.deepcopy(messages), "after_messages": copy.deepcopy(messages), "unchanged": True},
            })
        if name == "add-evidence":
            evidence["bound_context"] = {"run_id": run_id, "source": {"ref": str(source)}, "source_sha256": probe._digest(source), "memory_id": memory_id}
        if name in {"remember", "add-evidence"}:
            action = {"command_id": str(uuid4()), "actual_call_id": events[0]["payload"]["call_id"], "tool_name": tools[0], "state": "done", "origin_session_id": origin,
                      "result": {"state": "done", "ok": True, "error": None, "data": {"memory_id": memory_id, "write_effect": "created" if name == "remember" else "updated"}}}
            memory = {"id": memory_id, "type": "semantic", "status": "active", "content": "真实fixture记忆正文"}
            snapshot["actions"] = [copy.deepcopy(action)]
            snapshot["memories"] = [copy.deepcopy(memory)]
            evidence["receipt"] = action
            if name == "remember":
                evidence["memory"] = memory
            else:
                revision = {"memory_id": memory_id, "evidence": "fixture evidence"}
                evidence["revision_audit"] = revision
                snapshot["belief_revision_audit"] = [copy.deepcopy(revision)]
        all_cases[name] = {
            "schema": "bob108-tool-activity-case-v1", "case": name, "run_id": run_id, "status": "PASS",
            "run": {"database": database}, "origin_session_id": origin, "expected_tools": sorted(set(tools)),
            "began_at": "2026-10-09T00:00:00Z", "ended_at": "2026-10-09T00:00:01Z", "evidence": evidence,
        }
    writes = []
    monkeypatch.setattr(probe, "load_run", lambda path: (tmp_path, manifest, {}, {}, {}))
    monkeypatch.setattr(probe, "_atomic_json", lambda path, value: writes.append((path, value)))
    return manifest, all_cases, cases_dir, writes


@pytest.mark.parametrize("missing", ["raw-events", "dom", "db-owner", "call-id", "working-state", "source-hash", "evidence-source-hash"])
def test_verifier_rejects_pass_with_missing_required_case_evidence(tmp_path, monkeypatch, missing):
    manifest, cases, directory, writes = _prepared_case_evidence(tmp_path, monkeypatch)
    name = "background-tools" if missing == "working-state" else "add-evidence" if missing == "evidence-source-hash" else "voice-recall"
    evidence = cases[name]["evidence"]
    if missing == "raw-events":
        evidence.pop("activity_events")
    elif missing == "dom":
        evidence["activity_dom"] = ""
    elif missing == "db-owner":
        evidence["snapshot"]["database"] = "hexis_memory"
    elif missing == "call-id":
        evidence["activity_events"][0]["payload"].pop("call_id")
    elif missing == "working-state":
        evidence.pop("observed_event_state_snapshots")
    elif missing == "source-hash":
        evidence.pop("source_fingerprints")
    else:
        evidence["bound_context"].pop("source_sha256")
    for key, value in cases.items():
        (directory / f"{key}.json").write_text(json.dumps(value), encoding="utf-8")

    result = probe.verify_run(tmp_path)

    assert result["cases"][name]["status"] == "FAIL"
    assert result["cases"][name]["reason"]
    assert result["state"] == "verification_incomplete"
    assert all(case["status"] == "PASS" for key, case in result["cases"].items() if key != name)


def test_verifier_accepts_complete_case_shape_and_api_enrichment(tmp_path, monkeypatch):
    manifest, cases, directory, writes = _prepared_case_evidence(tmp_path, monkeypatch)
    for event in cases["background-tools"]["evidence"]["task_activity"]:
        event["activity"] = {"title": event["payload"]["tool_name"]}
        event["ts"] = "2026-10-09T00:00:00+00:00"
    for name, value in cases.items():
        (directory / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")

    result = probe.verify_run(tmp_path)

    assert result["state"] == "verification_incomplete"
    assert all(case["status"] == "PASS" for case in result["cases"].values())
    assert result["coverage"]["original-tool-messages-unchanged"]["status"] == "PASS"
    assert all(row["status"] == "NOT_RUN" for key, row in result["coverage"].items() if key != "original-tool-messages-unchanged")
    assert len(writes) == 2


@pytest.mark.parametrize("invalid", ["accepted", "unknown", "receipt-unknown", "flat-only", "wrong-db-uuid", "memory-missing", "wrong-evidence-uuid"])
def test_verifier_requires_done_nested_memory_receipt_and_matching_db_memory_or_revision(tmp_path, monkeypatch, invalid):
    manifest, cases, directory, _writes = _prepared_case_evidence(tmp_path, monkeypatch)
    name = "add-evidence" if invalid == "wrong-evidence-uuid" else "remember"
    raw = cases[name]
    receipt = raw["evidence"]["receipt"]
    if invalid in {"accepted", "unknown"}:
        receipt["state"] = invalid
    elif invalid == "receipt-unknown":
        receipt["result"]["state"] = "unknown"
    elif invalid == "flat-only":
        receipt["result"]["memory_id"] = receipt["result"]["data"]["memory_id"]
        receipt["result"].pop("data")
    elif invalid == "wrong-db-uuid":
        raw["evidence"]["snapshot"]["memories"][0]["id"] = str(uuid4())
    elif invalid == "memory-missing":
        raw["evidence"]["snapshot"]["memories"] = []
    else:
        raw["evidence"]["revision_audit"]["memory_id"] = str(uuid4())
    raw["evidence"]["snapshot"]["actions"] = [copy.deepcopy(receipt)]
    for key, value in cases.items():
        (directory / f"{key}.json").write_text(json.dumps(value))
    result = probe.verify_run(tmp_path)
    assert result["cases"][name]["status"] == "FAIL"
    assert result["state"] == "verification_incomplete"


@pytest.mark.parametrize("scenario", ["both-null", "one-sided-null", "payload-tamper", "event-id", "seq", "missing-ts", "bad-ts", "cursor-regression"])
def test_verifier_checks_entire_accepted_event_with_schema_nullable_timestamp(tmp_path, monkeypatch, scenario):
    manifest, cases, directory, _writes = _prepared_case_evidence(tmp_path, monkeypatch)
    evidence = cases["remember"]["evidence"]
    accepted = copy.deepcopy(evidence["activity_events"][-1])
    accepted.update({"event_id": str(uuid4()), "stream_id": "voice-accepted", "seq": 1, "ts": None, "activity": None})
    accepted["payload"]["state"] = "accepted"
    evidence["activity_events"].append(accepted)
    evidence["snapshot"]["events"].append(copy.deepcopy(accepted))
    evidence["api_cursor"]["voice-accepted"] = 1
    if scenario == "one-sided-null":
        evidence["snapshot"]["events"][-1]["ts"] = "2026-10-09T00:00:00Z"
    elif scenario == "payload-tamper":
        accepted["payload"]["arguments"] = {"changed": True}
    elif scenario == "event-id":
        accepted["event_id"] = str(uuid4())
    elif scenario == "seq":
        accepted["seq"] = 2
        evidence["api_cursor"]["voice-accepted"] = 2
    elif scenario == "missing-ts":
        accepted.pop("ts")
    elif scenario == "bad-ts":
        accepted["ts"] = "bad timestamp"
        evidence["snapshot"]["events"][-1]["ts"] = "bad timestamp"
    elif scenario == "cursor-regression":
        evidence["api_cursor"]["voice-accepted"] = 0
    evidence["event_ids"] = [event["event_id"] for event in evidence["activity_events"]]
    for name, case in cases.items():
        (directory / f"{name}.json").write_text(json.dumps(case))
    result = probe.verify_run(tmp_path)
    assert result["cases"]["remember"]["status"] == ("PASS" if scenario == "both-null" else "FAIL")


@pytest.mark.parametrize("invalid", [None, "voice-schema", "prompt-hash", "tool-failed", "wrong-memory", "wrong-session", "missing-chat-call", "missing-chat-result", "missing-revision", "revision-source", "revision-kind", "screenshot", "source-map"])
def test_text_add_evidence_verifier_uses_persistent_call_result_and_memory_audit_without_m5_actions(tmp_path, monkeypatch, invalid):
    manifest, cases, directory, _writes = _prepared_case_evidence(tmp_path, monkeypatch)
    manifest["acceptance_modes"] = {"add-evidence": "text_chat"}
    raw = cases["add-evidence"]
    raw.update({"schema": "bob108-add-evidence-text-case-v1", "input_mode": "text_chat"})
    evidence = raw["evidence"]
    evidence.pop("receipt")
    memory_id = evidence["bound_context"]["memory_id"]
    source = {"kind": "repository_document", "ref": evidence["bound_context"]["source"]["ref"], "sha256": evidence["bound_context"]["source_sha256"]}
    prompt = f"请实际调用 add_evidence 工具，不要只用文字回答。记忆 {memory_id} 使用来源 {source['ref']}。"
    evidence.update({"memory_id": memory_id, "source": source, "text_request": prompt, "text_request_sha256": hashlib.sha256(prompt.encode()).hexdigest()})
    start, result = evidence["activity_events"]
    start["payload"]["arguments"] = {"memory_id": memory_id, "stance": "supports", "source": {"kind": source["kind"], "ref": source["ref"]}}
    result["payload"].update({"success": True, "error": None, "output": {"prior": 0.5, "posterior": 0.7}})
    evidence.update({"call_id": start["payload"]["call_id"], "tool_call": start, "tool_result": result})
    snapshot = evidence["snapshot"]
    snapshot["events"] = []
    evidence["activity_events"] = []
    evidence["api_cursor"] = {}
    snapshot["actions"] = []
    turn, evidence_uuid = str(uuid4()), str(uuid4())
    origin = raw["origin_session_id"]
    details = {"memory_id": memory_id, "evidence_memory_id": evidence_uuid, "prior": 0.5, "posterior": 0.7, "applied": True}
    snapshot["chat_messages"] = [
        {"id": str(uuid4()), "ordinal": 0, "session_id": origin, "role": "user", "content": prompt, "source_message_id": f"chat:{origin}:request:user"},
        {"id": str(uuid4()), "ordinal": 1, "session_id": origin, "role": "assistant", "metadata": {"agent_turn_id": turn}, "content_blocks": [{"type": "toolCall", "id": evidence["call_id"], "name": "add_evidence", "arguments": start["payload"]["arguments"]}]},
        {"id": str(uuid4()), "ordinal": 2, "session_id": origin, "role": "toolResult", "metadata": {"agent_turn_id": turn, "tool_result": {"toolCallId": evidence["call_id"], "toolName": "add_evidence", "isError": False, "details": details}}},
        {"id": str(uuid4()), "ordinal": 3, "session_id": origin, "role": "assistant", "content": f"✅ add_evidence executed successfully. {memory_id} {evidence_uuid}", "metadata": {"agent_turn_id": turn}, "source_message_id": f"chat:{origin}:request:assistant"},
    ]
    evidence.update(dict(zip(("user_message", "tool_call_message", "tool_result_message", "assistant_response_message"), snapshot["chat_messages"])))
    evidence["chat_dom"] = prompt + "\n" + snapshot["chat_messages"][-1]["content"]
    evidence["browser_capture"] = {"browser": "chrome", "headed": True, "page_url": "http://127.0.0.1:3577/chat", "captured_at": "2026-10-09T00:00:00.9Z", "scope": "original_live_capture"}
    revision = {"memory_id": memory_id, "evidence": {"kind": source["kind"], "ref": source["ref"], "observed_at": "2026-10-09T00:00:00.5Z", "trust": 0.9}, "prior_confidence": 0.5, "posterior_confidence": 0.7, "created_at": "2026-10-09T00:00:00.5Z"}
    evidence["revision_audit"] = revision
    snapshot["belief_revision_audit"] = [copy.deepcopy(revision)]
    evidence["memory"] = snapshot["memories"][0]
    if invalid == "voice-schema":
        raw["schema"] = "bob108-tool-activity-case-v1"
    elif invalid == "prompt-hash":
        evidence["text_request_sha256"] = "0" * 64
    elif invalid == "tool-failed":
        evidence["tool_result_message"]["metadata"]["tool_result"]["isError"] = True
    elif invalid == "wrong-memory":
        snapshot["memories"][0]["id"] = str(uuid4())
    elif invalid == "wrong-session":
        evidence["tool_call_message"]["session_id"] = str(uuid4())
    elif invalid == "missing-chat-call":
        snapshot["chat_messages"].pop(1)
    elif invalid == "missing-chat-result":
        snapshot["chat_messages"].pop(2)
    elif invalid == "missing-revision":
        snapshot["belief_revision_audit"] = []
    elif invalid == "revision-source":
        evidence["revision_audit"]["evidence"]["ref"] = "wrong source"
        snapshot["belief_revision_audit"] = [copy.deepcopy(evidence["revision_audit"])]
    elif invalid == "revision-kind":
        evidence["revision_audit"]["evidence"]["kind"] = "wrong_kind"
        snapshot["belief_revision_audit"] = [copy.deepcopy(evidence["revision_audit"])]
    elif invalid == "screenshot":
        evidence["screenshot_sha256"] = "0" * 64
    elif invalid == "source-map":
        evidence["source_fingerprints"] = {}
    for name, case in cases.items():
        (directory / f"{name}.json").write_text(json.dumps(case))
    outcome = probe.verify_run(tmp_path)
    assert outcome["cases"]["add-evidence"]["status"] == ("PASS" if invalid is None else "FAIL")
    assert outcome["state"] == "verification_incomplete"


@pytest.mark.parametrize("db_ts,api_ts,expected", [(None, None, True), (None, "2026-10-09T00:00:00Z", False), ("2026-10-09T00:00:00Z", None, False), ("2026-10-09T00:00:00Z", "2026-10-09 08:00:00+08:00", True), ("bad", "bad", False)])
def test_persisted_event_match_preserves_nullable_and_nonnull_timestamp_contract(db_ts, api_ts, expected):
    row = {"event_id": str(uuid4()), "session_id": str(uuid4()), "stream_id": "voice", "seq": 1, "event_type": "TOOL_RESULT", "payload": {"tool_name": "remember", "call_id": "call", "state": "accepted"}, "ts": db_ts}
    event = {**copy.deepcopy(row), "ts": api_ts}
    assert probe._persisted_event_matches(row, event) is expected


def test_original_message_verifier_excludes_origin_and_other_task_rows(tmp_path, monkeypatch):
    manifest, cases, directory, writes = _prepared_case_evidence(tmp_path, monkeypatch)
    evidence = cases["background-tools"]["evidence"]
    example = evidence["snapshot"]["chat_messages"][0]
    evidence["snapshot"]["chat_messages"].extend([
        {**copy.deepcopy(example), "id": str(uuid4()), "session_id": cases["background-tools"]["origin_session_id"], "content": "来源会话内容"},
        {**copy.deepcopy(example), "id": str(uuid4()), "session_id": str(uuid4()), "content": "另一任务内容"},
    ])
    (directory / "background-tools.json").write_text(json.dumps(cases["background-tools"]), encoding="utf-8")

    result = probe.verify_run(tmp_path)

    assert result["cases"]["background-tools"]["status"] == "PASS"
    assert result["coverage"]["original-tool-messages-unchanged"]["status"] == "PASS"


def test_verifier_preserves_fail_and_not_run_status_without_demands_for_success_evidence(tmp_path, monkeypatch):
    manifest, cases, directory, writes = _prepared_case_evidence(tmp_path, monkeypatch)
    for name, status in (("voice-recall", "FAIL"), ("remember", "NOT_RUN")):
        value = cases[name]
        evidence = {"source_fingerprints": value["evidence"]["source_fingerprints"], "snapshot": value["evidence"]["snapshot"]} if status == "NOT_RUN" else {}
        value.update({"status": status, "reason": "original recorded reason", "evidence": evidence})
        (directory / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")

    result = probe.verify_run(tmp_path)

    assert result["state"] == "verification_incomplete"
    assert result["cases"]["voice-recall"] == {"status": "FAIL", "reason": "original recorded reason"}
    assert result["cases"]["remember"] == {"status": "NOT_RUN", "reason": "original recorded reason"}
    assert result["cases"]["background-tools"]["status"] == "NOT_RUN"


@pytest.mark.parametrize("invalid", [None, "reason", "reason-empty", "snapshot", "owner", "sources", "dependency-reason", "memory-exists", "tool-executed", "dependency-pass"])
def test_verifier_requires_truthful_not_run_dependency_and_snapshot_and_never_verifies(tmp_path, monkeypatch, invalid):
    manifest, cases, directory, _writes = _prepared_case_evidence(tmp_path, monkeypatch)
    raw = cases["add-evidence"]
    raw["status"] = "NOT_RUN"
    raw["reason"] = "本轮 remember 没有真实 done memory UUID，add_evidence 依赖未满足。"
    snapshot = raw["evidence"]["snapshot"]
    snapshot["actions"] = []
    snapshot["events"] = []
    cases["remember"]["status"] = "FAIL"
    cases["remember"]["reason"] = "真实 remember 未执行"
    if invalid == "reason":
        raw.pop("reason")
    elif invalid == "reason-empty":
        raw["reason"] = " "
    elif invalid == "snapshot":
        raw["evidence"].pop("snapshot")
    elif invalid == "owner":
        snapshot["database"] = "other"
    elif invalid == "sources":
        raw["evidence"]["source_fingerprints"] = {}
    elif invalid == "dependency-reason":
        raw["reason"] = "任意跳过理由"
    elif invalid == "memory-exists":
        snapshot["actions"] = [{"tool_name": "remember", "state": "done", "result": {"state": "done", "ok": True, "error": None, "data": {"memory_id": str(uuid4())}}}]
    elif invalid == "tool-executed":
        snapshot["events"] = [{"event_type": "TOOL_START", "payload": {"tool_name": "add_evidence"}}]
    elif invalid == "dependency-pass":
        cases["remember"]["status"] = "PASS"
    for name, value in cases.items():
        (directory / f"{name}.json").write_text(json.dumps(value))
    monkeypatch.setattr(probe, "verification_coverage", lambda *args, **kwargs: ({"original-tool-messages-unchanged": {"status": "PASS"}}, {"test_reports": [], "controlled_runs": []}))
    result = probe.verify_run(tmp_path)
    assert result["cases"]["add-evidence"]["status"] == ("NOT_RUN" if invalid is None else "FAIL")
    assert result["cases"]["add-evidence"]["reason"]
    assert result["state"] == "verification_incomplete"


def test_total_matrix_lists_every_required_sprint_boundary_without_inventing_pass(tmp_path, monkeypatch):
    _prepared_case_evidence(tmp_path, monkeypatch)

    result = probe.verify_run(tmp_path)

    assert set(result["coverage"]) == {
        "unknown", "accepted-cursor", "refresh-replay", "disconnect-replay", "out-of-order",
        "same-seq-cross-stream", "late-session-response", "origin-task-isolation",
        "original-tool-messages-unchanged",
    }
    assert all(row["status"] == "NOT_RUN" for row in result["coverage"].values())
    assert result["coverage_inputs"] == {"controlled_runs": [], "test_reports": []}


def _test_original_messages_summary(messages):
    canonical = json.dumps(sorted(messages, key=lambda row: row["ordinal"]), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"message_count": len(messages), "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def test_original_message_canonicalization_matches_javascript_key_and_number_rules():
    value = {"中文": "原始正文", "number": [1.0, 1e-7, 1e-6, 1e20, 1e21, -0.0],
        "keys": {"10": 10, "2": 2, "Z": 3, "a": 4, "_": 5, "\U00010000": 6, "\ue000": 7}}

    assert probe._canonical_message_json(value) == '{"keys":{"2":2,"10":10,"Z":3,"_":5,"a":4,"𐀀":6,"":7},"number":[1,1e-7,0.000001,100000000000000000000,1e+21,0],"中文":"原始正文"}'


@pytest.mark.parametrize("invalid", ["missing", "claimed-hash", "count", "before-content", "snapshot-content", "snapshot-missing", "shape-only", "empty", "no-tools", "foreign-session", "missing-call", "missing-result"])
def test_background_original_messages_requires_raw_unchanged_db_rows(tmp_path, monkeypatch, invalid):
    manifest, cases, directory, writes = _prepared_case_evidence(tmp_path, monkeypatch)
    evidence = cases["background-tools"]["evidence"]
    original = evidence["original_tool_messages"]
    if invalid == "missing":
        evidence.pop("original_tool_messages")
    elif invalid == "claimed-hash":
        original["before"]["sha256"] = original["after"]["sha256"] = "0" * 64
    elif invalid == "count":
        original["before"]["message_count"] = original["after"]["message_count"] = 99
    elif invalid == "before-content":
        original["before_messages"][0]["content_blocks"] = [{"type": "text", "text": "已篡改正文"}]
    elif invalid == "snapshot-content":
        evidence["snapshot"]["chat_messages"][0]["metadata"] = {"changed": True}
    elif invalid == "snapshot-missing":
        evidence["snapshot"].pop("chat_messages")
    elif invalid == "shape-only":
        for side in ("before", "after"):
            original.pop(f"{side}_messages")
    elif invalid == "empty":
        summary = _test_original_messages_summary([])
        original.update({"before": summary, "after": copy.deepcopy(summary)})
        original.update({"before_messages": [], "after_messages": []})
        evidence["snapshot"]["chat_messages"] = []
    elif invalid == "no-tools":
        messages = original["before_messages"]
        for message in messages:
            message.update({"role": "user", "content_blocks": [{"type": "text", "text": "普通用户消息"}], "metadata": {}})
        summary = _test_original_messages_summary(messages)
        original.update({"before": summary, "after": copy.deepcopy(summary)})
        original["after_messages"] = copy.deepcopy(messages)
        evidence["snapshot"]["chat_messages"] = copy.deepcopy(messages)
    else:
        messages = original["before_messages"]
        if invalid == "foreign-session":
            for message in messages:
                message["session_id"] = cases["background-tools"]["origin_session_id"]
        else:
            messages.pop(0 if invalid == "missing-call" else 1)
        summary = _test_original_messages_summary(messages)
        original.update({"before": summary, "after": copy.deepcopy(summary), "after_messages": copy.deepcopy(messages)})
        evidence["snapshot"]["chat_messages"] = copy.deepcopy(messages)
    (directory / "background-tools.json").write_text(json.dumps(cases["background-tools"]), encoding="utf-8")

    result = probe.verify_run(tmp_path)

    assert result["cases"]["background-tools"]["status"] == "FAIL"
    assert "original" in result["cases"]["background-tools"]["reason"]
    assert result["coverage"]["original-tool-messages-unchanged"]["status"] == "NOT_RUN"


def _coverage_test_receipt(tmp_path, manifest, *, status="passed", framework="vitest"):
    relative = "hexis-ui/lib/tool-activity.test.ts"
    title = "keeps event identity and current session when merging out-of-order page results"
    source = probe.ROOT / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(f'it("{title}", () => {{}});', encoding="utf-8")
    report = tmp_path / "vitest.json"
    report.write_text(json.dumps({"testResults": [{"name": str(source), "assertionResults": [
        {"title": title, "status": status},
    ]}]}), encoding="utf-8")
    receipt = {
        "schema": "bob108-tool-activity-test-report-v1", "framework": framework,
        "run_id": manifest["run_id"],
        "report": {"path": str(report), "sha256": probe._digest(report)},
        "source_fingerprints": {**manifest["source_fingerprints"], relative: probe._digest(source)},
    }
    receipt_path = tmp_path / "test-report-receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    return receipt_path, receipt, report, source


@pytest.mark.parametrize("status,expected", [("passed", "PASS"), ("failed", "FAIL"), ("pending", "NOT_RUN")])
def test_coverage_uses_native_test_result_and_keeps_product_cases_required(tmp_path, monkeypatch, status, expected):
    manifest, cases, directory, writes = _prepared_case_evidence(tmp_path, monkeypatch)
    receipt_path, receipt, report, source = _coverage_test_receipt(tmp_path, manifest, status=status)

    result = probe.verify_run(tmp_path, coverage_test_reports=[receipt_path])

    assert result["coverage"]["out-of-order"]["status"] == expected
    assert result["coverage"]["unknown"]["status"] == "NOT_RUN"
    assert result["state"] == "verification_incomplete"
    assert result["coverage_inputs"]["test_reports"][0]["report"] == receipt["report"]


@pytest.mark.parametrize("invalid", ["run", "report-hash", "source-hash", "pytest-db", "empty-report"])
def test_coverage_rejects_unverifiable_test_execution_claims(tmp_path, monkeypatch, invalid):
    manifest, cases, directory, writes = _prepared_case_evidence(tmp_path, monkeypatch)
    receipt_path, receipt, report, source = _coverage_test_receipt(tmp_path, manifest)
    if invalid == "run":
        receipt["run_id"] = "foreign-run"
    elif invalid == "report-hash":
        receipt["report"]["sha256"] = "0" * 64
    elif invalid == "source-hash":
        source.write_text("changed after execution", encoding="utf-8")
    elif invalid == "pytest-db":
        receipt["framework"] = "pytest"
        receipt["database_identity"] = {"database": "hexis_memory", "system_identifier": "main"}
    else:
        report.write_text('{"testResults": []}', encoding="utf-8")
        receipt["report"]["sha256"] = probe._digest(report)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    result = probe.verify_run(tmp_path, coverage_test_reports=[receipt_path])

    assert result["coverage_inputs"]["test_reports"][0]["status"] == "FAIL"
    assert result["state"] == "verification_incomplete"
    assert not any(row["status"] == "PASS" for row in result["coverage"].values())


def test_controlled_summary_is_hashed_readonly_and_never_substitutes_test_execution(tmp_path, monkeypatch):
    manifest, cases, directory, writes = _prepared_case_evidence(tmp_path, monkeypatch)
    control = tmp_path / "control"
    evidence = control / "evidence"
    evidence.mkdir(parents=True)
    identity = {"database": "bob108_browser", "system_identifier": "333"}
    (control / "run-manifest.json").write_text(json.dumps({
        "run_id": "control-01", "runtime": {"browser_db": "bob108_browser"},
        "service_identities": {"browser": identity}, "source_fingerprints": manifest["source_fingerprints"],
    }), encoding="utf-8")
    screenshot = evidence / "replay.png"
    screenshot.write_bytes(b"fixture image")
    summary = evidence / "sprint2-voice-recall-replay.json"
    summary.write_text(json.dumps({
        "session_id": str(uuid4()), "call_id": "controlled-call", "events": [
            {"event_id": str(uuid4()), "event_type": "TOOL_START", "stream_id": "voice", "seq": 1},
            {"event_id": str(uuid4()), "event_type": "TOOL_RESULT", "stream_id": "voice", "seq": 2},
        ], "browser_replay_after_reload": {"recent_events": 2, "request_visible": True,
            "result_visible": True, "duplicate_rows": False}, "screenshot": str(screenshot),
    }), encoding="utf-8")
    before = {str(file): file.read_bytes() for file in control.rglob("*") if file.is_file()}

    result = probe.verify_run(tmp_path, coverage_run_dirs=[control])

    row = result["coverage"]["refresh-replay"]
    assert row["status"] == "NOT_RUN"
    assert row["controlled_evidence"][0]["status"] == "CONTROLLED"
    assert row["controlled_evidence"][0]["summary"]["sha256"] == probe._digest(summary)
    assert {str(file): file.read_bytes() for file in control.rglob("*") if file.is_file()} == before
    assert all(not path.is_relative_to(control) for path, value in writes)


def test_verify_cli_accepts_explicit_repeated_coverage_inputs():
    args = probe._parser().parse_args([
        "verify", "--run-dir", "/cache/run", "--coverage-run-dir", "/cache/control",
        "--coverage-test-report", "/cache/pytest-receipt.json",
        "--coverage-test-report", "/cache/vitest-receipt.json",
    ])

    assert args.coverage_run_dir == ["/cache/control"]
    assert args.coverage_test_report == ["/cache/pytest-receipt.json", "/cache/vitest-receipt.json"]
    refresh = probe._parser().parse_args([
        "refresh-launcher-fingerprints", "--run-dir", "/cache/run",
        "--source-file", "hexis-ui/app/chat/page.tsx", "--reason", "已授权修复",
    ])
    assert refresh.source_file == ["hexis-ui/app/chat/page.tsx"]
    assert refresh.reason == "已授权修复"


def test_pytest_coverage_keeps_all_parameter_results_and_binds_isolated_identity(tmp_path, monkeypatch):
    manifest, cases, directory, writes = _prepared_case_evidence(tmp_path, monkeypatch)
    identity = {"database": "bob108_pytest", "system_identifier": "111"}
    manifest["service_identities"]["pytest"] = identity
    relative = "tests/voice_realtime/test_voice_recall.py"
    selector = "test_talk_disconnect_preserves_dispatched_tool_receipt_and_replay"
    source = probe.ROOT / relative
    source.parent.mkdir(parents=True)
    source.write_text(f"async def {selector}(): pass", encoding="utf-8")
    report = tmp_path / "pytest.xml"
    report.write_text(f'<testsuites><testsuite><testcase classname="tests.voice_realtime.test_voice_recall" name="{selector}[success]"/><testcase classname="tests.voice_realtime.test_voice_recall" name="{selector}[failure]"><failure message="assertion"/></testcase></testsuite></testsuites>', encoding="utf-8")
    receipt = tmp_path / "pytest-receipt.json"
    receipt.write_text(json.dumps({
        "schema": "bob108-tool-activity-test-report-v1", "framework": "pytest", "run_id": manifest["run_id"],
        "database_identity": identity, "report": {"path": str(report), "sha256": probe._digest(report)},
        "source_fingerprints": {**manifest["source_fingerprints"], relative: probe._digest(source)},
    }), encoding="utf-8")

    result = probe.verify_run(tmp_path, coverage_test_reports=[receipt])

    assert result["coverage"]["disconnect-replay"]["status"] == "FAIL"
    assert result["coverage"]["disconnect-replay"]["test_execution"][0]["execution"][0]["statuses"] == ["PASS", "FAIL"]


@pytest.mark.parametrize("invalid", ["missing-reason", "unknown-file", "absolute-file", "traversal", "unlisted-drift", "consent", "wrong-db", "api-start"])
def test_explicit_preconsent_source_refresh_never_silently_accepts_other_drift(tmp_path, monkeypatch, invalid):
    import asyncpg

    source_root = tmp_path / "sources"
    paths = ("hexis-ui/app/chat/page.tsx", "product.py", "apps/bob108_tool_activity_probe.py", "hexis-ui/e2e/tool-activity.spec.ts")
    for relative in paths:
        source = source_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("before", encoding="utf-8")
    monkeypatch.setattr(probe, "ROOT", source_root)
    manifest = {
        "run_id": "a" * 32, "state": "awaiting_consent",
        "consent": {"state": "required", "provider": "provider", "model": "qwen-plus", "endpoint": "https://provider.test"},
        "runtime": {"target": {"host": "127.0.0.1", "port": 45818, "database": "isolated", "user": "selected"}},
        "service_identities": {"target": {"database": "isolated", "system_identifier": "222"}},
        "source_fingerprints": {relative: probe._digest(source_root / relative) for relative in paths},
    }
    before = copy.deepcopy(manifest)
    (source_root / paths[0]).write_text("after authorised fix", encoding="utf-8")
    source_files, reason = [paths[0]], "修复用户手动 Activity 选择被初始 rAF 覆盖"
    if invalid == "missing-reason":
        reason = " "
    elif invalid == "unknown-file":
        source_files = ["unknown.py"]
    elif invalid == "absolute-file":
        source_files = [str(source_root / paths[0])]
    elif invalid == "traversal":
        source_files = ["../sources/product.py"]
    elif invalid == "unlisted-drift":
        (source_root / "product.py").write_text("unapproved drift", encoding="utf-8")
    elif invalid == "api-start":
        (tmp_path / "evidence").mkdir()
        (tmp_path / "evidence" / "api-start.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(probe, "load_run", lambda path: (tmp_path, manifest, {}, {}, {"POSTGRES_PASSWORD": "secret-value"}))
    writes, queries = [], []
    monkeypatch.setattr(probe, "_atomic_json", lambda path, value: writes.append((path, copy.deepcopy(value))))

    class Connection:
        async def fetchrow(self, query):
            queries.append(query)
            return {"database": "foreign" if invalid == "wrong-db" else "isolated", "system_identifier": "222"}

        async def fetchval(self, query, *args):
            queries.append(query)
            return invalid == "consent"

        async def close(self):
            pass

    async def connect(*args, **kwargs):
        return Connection()

    monkeypatch.setattr(asyncpg, "connect", connect)

    with pytest.raises(probe.ProbeError):
        asyncio.run(probe._refresh_launcher_fingerprints_async(tmp_path, source_files=source_files, reason=reason))

    assert writes == []
    assert manifest == before


def test_explicit_preconsent_source_refresh_records_old_new_hash_and_reason(tmp_path, monkeypatch):
    import asyncpg

    source_root = tmp_path / "sources"
    paths = ("hexis-ui/app/chat/page.tsx", "product.py", "apps/bob108_tool_activity_probe.py", "hexis-ui/e2e/tool-activity.spec.ts")
    for relative in paths:
        source = source_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("before", encoding="utf-8")
    monkeypatch.setattr(probe, "ROOT", source_root)
    fingerprints = {relative: probe._digest(source_root / relative) for relative in paths}
    manifest = {
        "run_id": "a" * 32, "state": "awaiting_consent",
        "consent": {"state": "required", "provider": "provider", "model": "qwen-plus", "endpoint": "https://provider.test"},
        "runtime": {"target": {"host": "127.0.0.1", "port": 45818, "database": "isolated", "user": "selected"}},
        "service_identities": {"target": {"database": "isolated", "system_identifier": "222"}},
        "source_fingerprints": dict(fingerprints),
    }
    (source_root / paths[0]).write_text("after authorised fix", encoding="utf-8")
    monkeypatch.setattr(probe, "load_run", lambda path: (tmp_path, manifest, {}, {}, {"POSTGRES_PASSWORD": "secret-value"}))
    writes = []
    monkeypatch.setattr(probe, "_atomic_json", lambda path, value: writes.append((path, copy.deepcopy(value))))

    class Connection:
        async def fetchrow(self, query):
            return {"database": "isolated", "system_identifier": "222"}

        async def fetchval(self, query, *args):
            return False

        async def close(self):
            pass

    async def connect(*args, **kwargs):
        return Connection()

    monkeypatch.setattr(asyncpg, "connect", connect)
    reason = "修复用户手动 Activity 选择被初始 rAF 覆盖"

    record = asyncio.run(probe._refresh_launcher_fingerprints_async(tmp_path, source_files=[paths[0]], reason=reason))

    assert record["reason"] == reason
    assert record["previous"][paths[0]] == fingerprints[paths[0]]
    assert record["current"][paths[0]] == probe._digest(source_root / paths[0])
    assert record["explicit_source_files"] == [paths[0]]
    assert manifest["source_fingerprints"]["product.py"] == fingerprints["product.py"]
    assert manifest["state"] == "awaiting_consent"
    assert len(writes) == 2


@pytest.mark.parametrize("invalid", ["accepted-only", "late-replay", "task-session", "cursor", "screenshot-hash", "call-pair", "source-drift"])
def test_verifier_rejects_inconsistent_evidence_without_treating_replay_as_live(tmp_path, monkeypatch, invalid):
    manifest, cases, directory, writes = _prepared_case_evidence(tmp_path, monkeypatch)
    name = "background-tools" if invalid in {"late-replay", "task-session"} else "voice-recall"
    evidence = cases[name]["evidence"]
    if invalid == "accepted-only":
        evidence["activity_events"][1]["payload"]["state"] = "accepted"
        evidence["snapshot"]["events"] = copy.deepcopy(evidence["activity_events"])
    elif invalid == "late-replay":
        for observed in evidence["observed_event_state_snapshots"]:
            observed["snapshot"]["tasks"][0]["product_state"] = "idle"
    elif invalid == "task-session":
        evidence["task"]["task_session_id"] = cases[name]["origin_session_id"]
    elif invalid == "cursor":
        evidence["api_cursor"]["tool-stream"] = 0
    elif invalid == "screenshot-hash":
        evidence["screenshot_sha256"] = "0" * 64
    elif invalid == "call-pair":
        evidence["activity_events"][1]["payload"]["call_id"] = "different-result-call"
        evidence["snapshot"]["events"] = copy.deepcopy(evidence["activity_events"])
        evidence["call_ids"].append("different-result-call")
    else:
        (probe.ROOT / "product.py").write_text("changed source", encoding="utf-8")
    for key, value in cases.items():
        (directory / f"{key}.json").write_text(json.dumps(value), encoding="utf-8")

    result = probe.verify_run(tmp_path)

    assert result["state"] == "verification_incomplete"
    assert result["cases"][name]["status"] == "FAIL"
    assert result["cases"][name]["reason"]


@pytest.mark.parametrize("error,classification,stage", [
    (RuntimeError("The model did not choose either consent or decline."), "decision_missing", "response_validation"),
    (RuntimeError("The model did not provide the required reason for its decision."), "reason_missing", "response_validation"),
    (RuntimeError("The model chose consent without providing the required signature."), "signature_missing", "response_validation"),
    (RuntimeError("remote response includes provider-secret and postgres-secret"), "Unknown", "consent_flow"),
    (RuntimeError("The model did not choose either consent or decline. provider-secret"), "Unknown", "consent_flow"),
    (ValueError("The model did not choose either consent or decline."), "Unknown", "consent_flow"),
])
def test_consent_failure_records_only_safe_exact_error_classification(tmp_path, monkeypatch, error, classification, stage):
    import asyncpg
    from core import init_api

    profile = {"provider": "selected-provider", "model": "qwen-plus", "endpoint": "https://provider.test/v1", "api_key_env": "DASHSCOPE_API_KEY"}
    manifest = {
        "run_id": "a" * 32, "state": "awaiting_consent",
        "runtime": {"database": "m5_acceptance_test", "target": {"host": "127.0.0.1", "port": 45818, "database": "m5_acceptance_test", "user": "selected_user"}},
        "service_identities": {"target": {"system_identifier": "222"}},
    }
    values = {"POSTGRES_PASSWORD": "postgres-secret", "DASHSCOPE_API_KEY": "provider-secret"}
    monkeypatch.setattr(probe, "load_run", lambda path: (tmp_path, manifest, {"selected_profile": profile}, {}, values))
    writes, closed, flow_calls = [], [], []
    monkeypatch.setattr(probe, "_atomic_json", lambda path, value: writes.append((path, copy.deepcopy(value))))

    class Connection:
        async def fetchrow(self, query, *args):
            if "current_database()" in query:
                return {"database": "m5_acceptance_test", "system_identifier": "222"}
            assert query.startswith("SELECT ")
            return None

        async def close(self):
            closed.append(True)

    async def connect(*args, **kwargs):
        return Connection()

    async def fail_flow(conn, config):
        flow_calls.append(True)
        raise error

    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(init_api, "run_consent_flow", fail_flow)
    with pytest.raises(probe.ProbeError) as raised:
        asyncio.run(probe._request_consent_async(tmp_path))

    expected = {"error_type": type(error).__name__, "error_stage": stage, "error_classification": classification}
    assert manifest["consent"] == {"state": "error", **expected}
    summary = next(value for path, value in writes if path.name == "consent-summary.json")
    assert {key: summary[key] for key in expected} == expected
    for forbidden in ("provider-secret", "postgres-secret", str(error)):
        assert forbidden not in json.dumps(writes, default=str)
        assert forbidden not in str(raised.value)
    assert manifest["state"] == "consent_error"
    assert flow_calls == [True]
    assert closed == [True]
    assert list(tmp_path.iterdir()) == []


def _retry_consent_fixture(tmp_path, monkeypatch):
    import asyncpg
    from core import init_api

    profile = {"provider": "openai-chat-completions-endpoint", "model": "qwen-plus", "endpoint": "https://provider.test/v1", "api_key_env": "DASHSCOPE_API_KEY"}
    run_id = "a" * 32
    database = f"m5_acceptance_{run_id}"
    source_root = tmp_path / "sources"
    paths = ("apps/bob108_tool_activity_probe.py", "hexis-ui/e2e/tool-activity.spec.ts", "hexis-ui/app/chat/page.tsx", "product.py")
    for relative in paths:
        file = source_root / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("attempt one source", encoding="utf-8")
    monkeypatch.setattr(probe, "ROOT", source_root)
    target = {"host": "127.0.0.1", "port": 45818, "database": database, "user": "selected_user", "admin_database": "postgres"}
    target_identity = {"database": database, "system_identifier": "222"}
    pytest_identity = {"database": "bob108_pytest", "system_identifier": "111"}
    resources = {name: {"container_id": name + "-owned"} for name in ("pytest-db", "browser-db", "fake-embeddings")}
    manifest = {
        "run_id": run_id, "state": "consent_error", "consent": {"state": "error", "error_type": "RuntimeError"},
        "ports": {"browser": 45818, "pytest": 45819},
        "runtime": {"database": database, "target": target, "pytest": {"host": "127.0.0.1", "port": 45819, "database": "bob108_pytest", "admin_database": "postgres"}},
        "service_identities": {"target": target_identity, "browser_admin": {"database": "postgres", "system_identifier": "222"}, "pytest": pytest_identity},
        "source_fingerprints": {relative: probe._digest(source_root / relative) for relative in paths},
        "owned_resources": resources,
        "api_process": None, "ui_process": None,
    }
    values = {"POSTGRES_PASSWORD": "postgres-secret", "POSTGRES_USER": "selected_user", "DASHSCOPE_API_KEY": "provider-secret"}
    (tmp_path / "evidence").mkdir()
    summary = {"run_id": run_id, "decision": "error", "error_type": "RuntimeError", **{key: profile[key] for key in ("provider", "model", "endpoint")}}
    (tmp_path / "evidence" / "consent-summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (tmp_path / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(probe, "load_run", lambda path: (tmp_path, manifest, {"selected_profile": profile}, {}, values))
    monkeypatch.setattr(probe.control_runtime, "_owned_resources", lambda path, record: resources)
    flow_calls, queries = [], []

    async def pg_identity(*args):
        return dict(pytest_identity)

    monkeypatch.setattr(probe.control_runtime, "_pg_identity", pg_identity)

    class Connection:
        async def fetchrow(self, query, *args):
            queries.append(query)
            if "current_database()" in query:
                return dict(target_identity)
            return None

        async def fetchval(self, query, *args):
            queries.append(query)
            return False

        async def close(self):
            pass

    async def connect(*args, **kwargs):
        return Connection()

    async def fail_flow(conn, config):
        flow_calls.append(copy.deepcopy(manifest))
        assert (tmp_path / "evidence" / "consent-retry-audit.json").is_file()
        raise RuntimeError("The model did not choose either consent or decline.")

    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(init_api, "run_consent_flow", fail_flow)
    return manifest, summary, flow_calls, queries


def test_retry_consent_records_second_attempt_before_call_and_never_allows_third(tmp_path, monkeypatch):
    manifest, summary, calls, queries = _retry_consent_fixture(tmp_path, monkeypatch)
    first_summary = (tmp_path / "evidence" / "consent-summary.json").read_bytes()
    first_fingerprints = dict(manifest["source_fingerprints"])
    with pytest.raises(probe.ProbeError):
        probe.retry_consent(tmp_path)
    assert len(calls) == 1
    assert calls[0]["consent_attempt_count"] == 2
    audit = json.loads((tmp_path / "evidence" / "consent-retry-audit.json").read_text())
    assert audit["max_total_attempts"] == 2
    assert audit["attempt_number"] == 2
    assert audit["source_fingerprints"] == first_fingerprints
    assert audit["first_attempt"]["source_fingerprints"] == first_fingerprints
    assert (tmp_path / "evidence" / "consent-attempt-1.json").read_bytes() == first_summary
    assert manifest["state"] == "consent_error"
    assert manifest["consent"]["error_classification"] == "decision_missing"
    with pytest.raises(probe.ProbeError):
        probe.retry_consent(tmp_path)
    assert len(calls) == 1
    assert all(query.startswith("SELECT ") for query in queries)


@pytest.mark.parametrize("invalid", ["state", "summary-missing", "summary-profile", "attempt-count", "api-process", "ui-log", "api-start", "readiness", "case", "source-drift", "pytest-identity", "target-identity", "existing-consent"])
def test_retry_consent_refuses_inconsistent_history_or_runtime_before_provider(tmp_path, monkeypatch, invalid):
    import asyncpg
    manifest, summary, calls, queries = _retry_consent_fixture(tmp_path, monkeypatch)
    if invalid == "state":
        manifest["state"] = "awaiting_consent"
    elif invalid == "summary-missing":
        (tmp_path / "evidence" / "consent-summary.json").unlink()
    elif invalid == "summary-profile":
        summary["model"] = "other-model"
        (tmp_path / "evidence" / "consent-summary.json").write_text(json.dumps(summary), encoding="utf-8")
    elif invalid == "attempt-count":
        manifest["consent_attempt_count"] = 2
    elif invalid == "api-process":
        manifest["api_process"] = {}
    elif invalid in {"ui-log", "api-start"}:
        (tmp_path / "evidence" / ("ui.log" if invalid == "ui-log" else "api-start.json")).write_text("", encoding="utf-8")
    elif invalid == "readiness":
        manifest["readiness"] = {}
    elif invalid == "case":
        (tmp_path / "evidence" / "cases").mkdir()
        (tmp_path / "evidence" / "cases" / "case.json").write_text("{}", encoding="utf-8")
    elif invalid == "source-drift":
        (probe.ROOT / "product.py").write_text("changed", encoding="utf-8")
    elif invalid == "pytest-identity":
        async def wrong_identity(*args):
            return {"database": "hexis_memory", "system_identifier": "main"}
        monkeypatch.setattr(probe.control_runtime, "_pg_identity", wrong_identity)
    elif invalid == "target-identity":
        manifest["service_identities"]["target"]["system_identifier"] = "main"
    else:
        class ExistingConnection:
            async def fetchrow(self, query, *args):
                if "current_database()" in query:
                    return {"database": manifest["runtime"]["database"], "system_identifier": "222"}
                return {"decision": "consent"}
            async def close(self):
                pass
        async def connect(*args, **kwargs):
            return ExistingConnection()
        monkeypatch.setattr(asyncpg, "connect", connect)
    with pytest.raises(probe.ProbeError):
        probe.retry_consent(tmp_path)
    assert calls == []
    assert not (tmp_path / "evidence" / "consent-retry-audit.json").exists()


def test_failed_first_attempt_source_refresh_preserves_history_and_freezes_retry_source(tmp_path, monkeypatch):
    manifest, summary, calls, queries = _retry_consent_fixture(tmp_path, monkeypatch)
    old = dict(manifest["source_fingerprints"])
    first_summary = (tmp_path / "evidence" / "consent-summary.json").read_bytes()
    for relative in ("apps/bob108_tool_activity_probe.py", "hexis-ui/e2e/tool-activity.spec.ts", "hexis-ui/app/chat/page.tsx"):
        (probe.ROOT / relative).write_text("new audited source", encoding="utf-8")
    record = probe.refresh_launcher_fingerprints(tmp_path, source_files=["hexis-ui/app/chat/page.tsx"], reason="记录已授权的诊断修复和本轮页面源码版本")
    assert record["historical_attempt_preserved"] is True
    assert record["first_attempt"]["source_fingerprints"] == old
    assert manifest["source_fingerprints"]["product.py"] == old["product.py"]
    assert manifest["state"] == "consent_error"
    assert (tmp_path / "evidence" / "consent-attempt-1.json").read_bytes() == first_summary
    with pytest.raises(probe.ProbeError):
        probe.retry_consent(tmp_path)
    audit = json.loads((tmp_path / "evidence" / "consent-retry-audit.json").read_text())
    assert audit["first_attempt"]["source_fingerprints"] == old
    assert audit["source_fingerprints"] == manifest["source_fingerprints"]
    assert len(calls) == 1
    assert audit["source_fingerprints"]["apps/bob108_tool_activity_probe.py"] != old["apps/bob108_tool_activity_probe.py"]
    all_evidence = " ".join(file.read_text() for file in (tmp_path / "evidence").iterdir() if file.is_file())
    assert "postgres-secret" not in all_evidence and "provider-secret" not in all_evidence


@pytest.mark.parametrize("decision", ["consent", "decline"])
def test_second_consent_attempt_success_or_decline_preserves_first_and_consumes_retry(tmp_path, monkeypatch, decision):
    import asyncpg
    from core import init_api
    from datetime import datetime, timezone

    manifest, summary, calls, queries = _retry_consent_fixture(tmp_path, monkeypatch)
    row_id = str(uuid4())
    flow_completed = []
    class Connection:
        async def fetchrow(self, query, *args):
            if "current_database()" in query:
                return {"database": manifest["runtime"]["database"], "system_identifier": "222"}
            if not flow_completed:
                return None
            return {"id": row_id, "decision": decision, "provider": "openai-chat-completions-endpoint", "model": "qwen-plus", "endpoint": "https://provider.test/v1", "response": {"operator_override": False}, "decided_at": datetime.now(timezone.utc)}
        async def fetchval(self, query, *args):
            return "consent" if "get_agent_consent_status" in query else {"configured": True}
        async def close(self):
            pass
    async def connect(*args, **kwargs):
        return Connection()
    async def flow(conn, config):
        audit = json.loads((tmp_path / "evidence" / "consent-retry-audit.json").read_text())
        assert audit["attempt_number"] == 2
        flow_completed.append(True)
        return {"decision": decision}
    async def seed(*args, **kwargs):
        return {"controlled": True}
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(init_api, "run_consent_flow", flow)
    monkeypatch.setattr(probe, "_seed_target_data", seed)
    if decision == "consent":
        result = probe.retry_consent(tmp_path)
        assert result["state"] == "consent_accepted"
    else:
        with pytest.raises(probe.ProbeError):
            probe.retry_consent(tmp_path)
        assert manifest["state"] == "consent_declined"
    with pytest.raises(probe.ProbeError):
        probe.retry_consent(tmp_path)
    assert flow_completed == [True]
    final = json.loads((tmp_path / "evidence" / "consent-summary.json").read_text())
    assert final["attempt_number"] == 2
    assert (tmp_path / "evidence" / "consent-attempt-1.json").is_file()


def test_retry_cli_is_separate_from_standard_request_consent():
    args = probe._parser().parse_args(["retry-consent", "--run-dir", "/owned/run"])
    assert args.command == "retry-consent"


@pytest.mark.parametrize("kind,classification,stage", [
    ("connection", "sdk_network_error", "provider_transport"),
    ("timeout", "sdk_network_error", "provider_transport"),
    ("auth", "sdk_authentication_error", "provider_http"),
    ("http", "sdk_http_error", "provider_http"),
])
def test_safe_consent_sdk_classification_does_not_record_body_or_network_assumptions(kind, classification, stage):
    import httpx
    import openai
    request = httpx.Request("POST", "https://provider.test/v1")
    response = httpx.Response(401 if kind == "auth" else 500, request=request)
    if kind == "connection":
        error = openai.APIConnectionError(message="provider-secret connection detail", request=request)
    elif kind == "timeout":
        error = openai.APITimeoutError(request=request)
    elif kind == "auth":
        error = openai.AuthenticationError("provider-secret auth detail", response=response, body={"secret": "provider-secret"})
    else:
        error = openai.APIStatusError("provider-secret HTTP detail", response=response, body={"secret": "provider-secret"})
    metadata = probe._safe_consent_error_metadata(error)
    assert metadata == {"error_type": type(error).__name__, "error_stage": stage, "error_classification": classification}
    assert "provider-secret" not in json.dumps(metadata)


@pytest.mark.parametrize("invalid", ["product-source", "unlisted-drift", "second-attempt", "audit-exists"])
def test_post_error_source_refresh_refuses_product_changes_or_consumed_retry(tmp_path, monkeypatch, invalid):
    manifest, summary, calls, queries = _retry_consent_fixture(tmp_path, monkeypatch)
    before = copy.deepcopy(manifest)
    files = ["hexis-ui/app/chat/page.tsx"]
    if invalid == "product-source":
        files = ["product.py"]
    elif invalid == "unlisted-drift":
        (probe.ROOT / "product.py").write_text("unauthorised product change", encoding="utf-8")
    elif invalid == "second-attempt":
        manifest["consent_attempt_count"] = 2
        before = copy.deepcopy(manifest)
    else:
        (tmp_path / "evidence" / "consent-retry-audit.json").write_text("{}", encoding="utf-8")
    with pytest.raises(probe.ProbeError):
        probe.refresh_launcher_fingerprints(tmp_path, source_files=files, reason="记录重试前允许的诊断源码修复")
    assert manifest == before
    assert not (tmp_path / "evidence" / "consent-attempt-1-source.json").exists()
    assert calls == []


def test_consent_retry_audit_reservation_is_exclusive_and_never_overwrites(tmp_path):
    file = tmp_path / "consent-retry-audit.json"
    first = {"max_total_attempts": 2, "attempt_number": 2, "说明": "额外一次授权已占用"}
    probe._exclusive_json(file, first)
    original = file.read_bytes()
    with pytest.raises(probe.ProbeError):
        probe._exclusive_json(file, {"attempt_number": 3})
    assert file.read_bytes() == original
