from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from apps.voice_m4_probe import (
    EXPECTED_BASELINE,
    _STAGES,
    ProbeBaselineError,
    ProbeRequest,
    ProbeStageError,
    run_probe,
    sanitize_probe_evidence,
)


def write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x01\x00" * 160)


def request(tmp_path: Path, **overrides):
    wav = tmp_path / "input.wav"
    write_wav(wav)
    value = {
        "mode": "protocol",
        "wav": wav,
        "reference_wav": None,
        "output": tmp_path / "evidence.json",
        "expected_baseline": EXPECTED_BASELINE,
    }
    value.update(overrides)
    return ProbeRequest(**value)


async def test_probe_records_stage_failure_without_retrying_input(tmp_path):
    calls = []

    async def stage_runner(stage, probe_request):
        calls.append(stage)
        if stage == "cloud_input":
            raise ProbeStageError(stage, "cloud rejected the committed input")
        return {"status": "passed"}

    result = await run_probe(
        request(tmp_path),
        stage_runner=stage_runner,
        head_provider=lambda: {"head": EXPECTED_BASELINE, "dirty": False},
    )

    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert result.ok is False
    assert calls == ["wav", "cloud_input"]
    assert evidence["status"] == "failed"
    assert evidence["stages"][-1]["stage"] == "cloud_input"
    assert evidence["stages"][-1]["status"] == "failed"
    assert "cloud rejected" in evidence["stages"][-1]["reason"]


def test_probe_redacts_secrets_recursively():
    value = {
        "api_key": "secret-key",
        "authorization": "Bearer secret-token",
        "password": "secret-password",
        "nested": {"client_secret": "secret-client", "safe": "visible"},
        "reason": "used secret-key while connecting",
    }

    sanitized = sanitize_probe_evidence(value)
    text = json.dumps(sanitized, ensure_ascii=False)

    assert sanitized["nested"]["safe"] == "visible"
    assert "secret-key" not in text
    assert "secret-token" not in text
    assert "secret-password" not in text
    assert "secret-client" not in text
    assert sanitized["api_key"] == "[REDACTED]"


async def test_probe_rejects_mismatched_baseline(tmp_path):
    async def stage_runner(stage, probe_request):
        raise AssertionError("stage must not run after baseline mismatch")

    with pytest.raises(ProbeBaselineError):
        await run_probe(
            request(tmp_path),
            stage_runner=stage_runner,
            head_provider=lambda: {"head": "different-head", "dirty": False},
        )

    assert not (tmp_path / "evidence.json").exists()


def test_protocol_probe_defers_instruction_gate_until_after_capabilities():
    stages = _STAGES["protocol"]

    assert stages[-1] == "instruction_layers"
    assert stages.index("cancel") < stages.index("instruction_layers")
    assert stages.index("continuation") < stages.index("instruction_layers")
    assert stages.index("history") < stages.index("instruction_layers")
