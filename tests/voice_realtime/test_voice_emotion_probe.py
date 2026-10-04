from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apps.voice_emotion_probe import (
    ABehaviorEventSink,
    BFirstResponseAdapter,
    ProbeConfig,
    ProbeConfigError,
    main,
    resolve_api_key,
    sanitize_evaluation,
    sanitize_hydration,
    summarize_samples,
    write_evidence_exclusively,
)
from services.voice_emotion_gate import VoiceEmotionGateResult


def config():
    return {
        "emotion": {
            "provider": "openai-chat-completions-endpoint",
            "model": "qwen-plus",
            "endpoint": "https://example.invalid/v1",
            "api_key_env": "DASHSCOPE_API_KEY",
            "timeout_ms": 1000,
        },
        "db_source": "hexis-env",
        "memory_limit": 10,
        "stable_instructions": "稳定层指令",
    }


def messages():
    return {
        "schema": "bob102-emotion-hydration-probe-v1",
        "source": "synthetic",
        "messages": [
            {"id": "one", "text": "一"},
            {"id": "two", "text": "二"},
            {"id": "three", "text": "三"},
        ],
    }


def test_probe_config_requires_explicit_sources_and_sample_count():
    parsed = ProbeConfig.from_inputs(config(), messages())
    assert parsed.provider == "openai-chat-completions-endpoint"
    assert parsed.model == "qwen-plus"
    assert parsed.endpoint == "https://example.invalid/v1"
    assert parsed.api_key_env == "DASHSCOPE_API_KEY"
    assert parsed.db_source == "hexis-env"
    assert parsed.emotion_timeout_ms == 1000
    assert parsed.memory_limit == 10
    assert parsed.stable_instructions == "稳定层指令"
    assert parsed.sample_count == 3

    bad = config()
    del bad["emotion"]["api_key_env"]
    with pytest.raises(ProbeConfigError, match="api_key_env"):
        ProbeConfig.from_inputs(bad, messages())

    bad = config()
    del bad["db_source"]
    with pytest.raises(ProbeConfigError, match="db_source"):
        ProbeConfig.from_inputs(bad, messages())

    bad = messages()
    bad["messages"] = []
    with pytest.raises(ProbeConfigError, match="至少"):
        ProbeConfig.from_inputs(config(), bad)


def test_selected_environment_reads_only_named_variable(tmp_path, monkeypatch):
    requested_names = []

    def fake_get_key(path, name):
        requested_names.append(name)
        return "selected-value"

    monkeypatch.setattr("apps.voice_emotion_probe.get_key", fake_get_key)
    parsed = SimpleNamespace(api_key_env="SELECTED_API_KEY")

    assert resolve_api_key(parsed, env_file=tmp_path / ".env") == "selected-value"
    assert requested_names == ["SELECTED_API_KEY"]


def test_main_unexpected_error_does_not_leak_details(monkeypatch, capsys):
    async def failing_probe(*_args):
        raise RuntimeError("secret-key postgres://secret-dsn raw-model-response")

    monkeypatch.setattr("apps.voice_emotion_probe.run_probe", failing_probe)

    exit_code = main(
        ["--config", "c.json", "--messages", "m.json", "--output", "o.json"]
    )

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "probe failed: RuntimeError" in output
    assert "secret-key" not in output
    assert "postgres://" not in output
    assert "raw-model-response" not in output


def test_sanitization_removes_secrets_context_and_raw_model_response():
    evaluation = {
        "status": "valid",
        "value": {
            "label": "happy",
            "raw_business_value": "happy",
            "wordset_version": "v1",
            "logical_call_count": 1,
            "transport_attempts": None,
        },
    }
    sanitized_evaluation = sanitize_evaluation(evaluation)
    assert sanitized_evaluation["label"] == "happy"
    assert "raw_business_value" not in json.dumps(sanitized_evaluation)

    hydration = SimpleNamespace(
        per_response_instructions="完整上下文不应出现在证据中",
        instructions_hash="a" * 64,
        hydrate_ms=12.5,
        memory_count=2,
    )
    sanitized_hydration = sanitize_hydration(hydration)
    assert sanitized_hydration["available"] is True
    assert sanitized_hydration["instructions_length"] == len(
        hydration.per_response_instructions
    )
    assert "完整上下文" not in json.dumps(sanitized_hydration)


def test_summary_reports_actual_distribution_and_small_sample_limit():
    records = []
    for status, eval_ms, hydrate_ms, gate_ms in (
        ("valid", 100, 10, 100),
        ("error", 200, 20, 200),
        ("timeout", 300, 30, 300),
    ):
        records.append(
            {
                "evaluation": {"status": status},
                "metrics": {
                    "eval_ms": eval_ms,
                    "hydrate_ms": hydrate_ms,
                    "gate_wait_ms": gate_ms,
                    "release_reason": "model_timeout"
                    if status == "timeout"
                    else f"evaluation_{status}",
                },
            }
        )

    summary = summarize_samples(records)
    assert summary["sample_count"] == 3
    assert summary["eval_ms"]["p50"] == 200
    assert summary["eval_ms"]["p95"] == 300
    assert summary["hydrate_ms"]["p50"] == 20
    assert summary["gate_wait_ms"]["p95"] == 300
    assert summary["success_count"] == 1
    assert summary["failure_count"] == 1
    assert summary["timeout_release_count"] == 1
    assert summary["unknown_count"] == 0
    assert summary["sample_sufficient"] is False
    assert "不外推" in summary["note"]


@pytest.mark.asyncio(loop_scope="function")
async def test_behavior_sink_exact_retry_deduplicates_and_conflict_fails_loud():
    seen = {}

    async def recorder(pool, event):
        canonical = json.dumps(event.payload, sort_keys=True)
        if event.event_id in seen:
            if seen[event.event_id] != canonical:
                raise RuntimeError("event content conflict")
            return {"seq": 1, "deduplicated": True}
        seen[event.event_id] = canonical
        return {"seq": 1, "deduplicated": False}

    sink = ABehaviorEventSink(pool=object(), recorder=recorder)
    value = {
        "session_id": str(uuid4()),
        "input_turn_id": str(uuid4()),
        "input_order": 0,
        "stimulus_revision": 0,
    }
    event = sink.make_event(
        value,
        event_type="voice_emotion_probe.started",
        producer_seq=0,
        payload={"stage": "started"},
    )
    first = await sink.record(event)
    retry = await sink.record(event)
    assert first["deduplicated"] is False
    assert retry["deduplicated"] is True

    changed = sink.make_event(
        value,
        event_type="voice_emotion_probe.started",
        producer_seq=0,
        payload={"stage": "changed"},
    )
    assert changed.event_id == event.event_id
    with pytest.raises(RuntimeError, match="content conflict"):
        await sink.record(changed)


def gate_result(released, context_ready, visual_window):
    return VoiceEmotionGateResult(
        identity={
            "session_id": str(uuid4()),
            "input_turn_id": str(uuid4()),
            "input_order": 0,
            "stimulus_revision": 0,
        },
        released=released,
        context_ready=context_ready,
        evaluation={"status": "valid"},
        hydration=None,
        visual_window=visual_window,
        metrics={
            "eval_ms": 1,
            "hydrate_ms": 1,
            "gate_wait_ms": 1,
            "release_reason": "evaluation_complete",
        },
        late_diagnostics=(),
    )


def test_b_adapter_requires_both_facts_and_preserves_visual_facts():
    calls = []
    adapter = BFirstResponseAdapter(response_create=lambda: calls.append("called"))

    not_released = gate_result(False, True, {"status": "open"})
    outcome = adapter.apply(not_released.to_b_gate_facts())
    assert outcome["ready"] is False
    assert outcome["response_create_called"] is False
    assert not_released.visual_window["status"] == "open"

    no_context = gate_result(True, False, {"status": "open"})
    outcome = adapter.apply(no_context.to_b_gate_facts())
    assert outcome["ready"] is False
    assert outcome["response_create_called"] is False

    timeout = gate_result(
        True,
        True,
        {"status": "closed", "reason": "model_timeout"},
    )
    outcome = adapter.apply(timeout.to_b_gate_facts())
    assert outcome["ready"] is True
    assert outcome["response_create_called"] is True
    assert timeout.visual_window["status"] == "closed"

    ordinary_error = gate_result(True, True, {"status": "open"})
    outcome = adapter.apply(ordinary_error.to_b_gate_facts())
    assert outcome["ready"] is True
    assert ordinary_error.visual_window["status"] == "open"


def test_output_is_created_exclusively(tmp_path):
    path = tmp_path / "evidence.json"
    write_evidence_exclusively(path, {"schema": "voice-emotion-probe-v1"})
    assert json.loads(path.read_text())["schema"] == "voice-emotion-probe-v1"
    with pytest.raises(FileExistsError):
        write_evidence_exclusively(path, {"schema": "overwrite"})
