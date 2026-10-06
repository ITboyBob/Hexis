"""Repeatable preflight and capability probes for the M4 voice chain."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
import wave
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import get_key

from apps.voice_realtime_harness import (
    EvidenceWebSocket,
    _default_connect_factory,
    read_wav,
)
from core.agent_api import db_dsn_from_env
from services.qwen_realtime_client import QwenRealtimeClient
from services.voice_context_hydration import hydrate_voice_context


SCHEMA = "voice-m4-probe-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_BASELINE = "3aa3dfe8c0135078899622359e524ecb863e69b9"
_MODES = {"preflight", "protocol", "devices", "calibration"}
_SELECTED_REALTIME_CONFIG = Path(
    "/Users/itboybob/.cache/hexis/batch3-serial-voice-2026-10-05/config-real-2.json"
)
_M4_INSTRUCTIONS = Path(
    "/Users/itboybob/Project/geek_interview/docs/附录-语音对话层系统指令-M4测试版.md"
)
_STABLE_ANCHOR = "M4_STABLE_ANCHOR"
_TURN_ANCHOR = "M4_TURN_ANCHOR"
_STAGES = {
    "preflight": (
        "worktree",
        "wav",
        "dependencies",
        "playwright",
        "database",
        "evidence_coverage",
    ),
    "protocol": (
        "wav",
        "cloud_input",
        "cloud_response",
        "cancel",
        "continuation",
        "history",
        "instruction_layers",
    ),
    "devices": ("reference_wav", "browser_probe"),
    "calibration": ("reference_wav", "playback", "capture"),
}
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|password|secret|token|dsn|database[_-]?url)",
    re.IGNORECASE,
)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ProbeStageError(RuntimeError):
    """A probe stage failed and later stages must not run."""

    def __init__(
        self, stage: str, reason: str, details: dict[str, Any] | None = None
    ):
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.details = details


class ProbeBaselineError(RuntimeError):
    """The code baseline no longer matches the plan's authorized baseline."""


@dataclass(frozen=True)
class ProbeRequest:
    mode: str
    wav: Path | None
    reference_wav: Path | None
    output: Path
    expected_baseline: str = EXPECTED_BASELINE

    def validate(self) -> None:
        if self.mode not in _MODES:
            raise ValueError(f"未知 probe 模式: {self.mode}")
        if self.mode in {"preflight", "protocol"} and self.wav is None:
            raise ValueError(f"{self.mode} 模式必须提供 --wav")
        if self.mode in {"devices", "calibration"} and self.reference_wav is None:
            raise ValueError(f"{self.mode} 模式必须提供 --reference-wav")
        if not self.expected_baseline.strip():
            raise ValueError("expected_baseline 必须是非空字符串")


@dataclass(frozen=True)
class ProbeRunResult:
    ok: bool
    evidence: dict[str, Any]


def load_selected_config() -> dict[str, Any]:
    if not _SELECTED_REALTIME_CONFIG.is_file():
        raise ProbeStageError("config", "已选择的 Qwen Realtime 配置文件不存在")
    value = json.loads(_SELECTED_REALTIME_CONFIG.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ProbeStageError("config", "已选择的 Qwen Realtime 配置必须是 JSON object")
    return value


def load_m4_stable_instructions() -> str:
    if not _M4_INSTRUCTIONS.is_file():
        raise ProbeStageError("instructions", "M4 测试指令文件不存在")
    text = _M4_INSTRUCTIONS.read_text(encoding="utf-8")
    match = re.search(
        r"## 2\. M4 测试稳定层全文.*?```markdown\n(.*?)\n```",
        text,
        re.DOTALL,
    )
    if match is None or not match.group(1).strip():
        raise ProbeStageError("instructions", "M4 稳定层指令无法解析")
    return match.group(1)


def _text_summary(value: str) -> dict[str, Any]:
    return {
        "length": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
    }


class ProtocolProbe:
    """Run live M4 protocol checks and defer the instruction gate to the end."""

    def __init__(self) -> None:
        self.config = load_selected_config()
        self.base_stable_instructions = load_m4_stable_instructions()
        self.stable_instructions = (
            f"{self.base_stable_instructions}\n\n"
            f"Capability probe stable anchor: include the exact token "
            f"{_STABLE_ANCHOR} in every reply."
        )
        self.protocol_events: list[dict[str, Any]] = []
        self.unknown_events: list[dict[str, Any]] = []
        self.client: QwenRealtimeClient | None = None
        self.pool: Any | None = None
        self.audio: Any | None = None
        self.per_response_instructions: str | None = None
        self.final_transcript: str | None = None
        self.last_response_transcript: str | None = None
        self.first_response_transcript: str | None = None
        self.instruction_anchor_results: list[dict[str, Any]] = []

    async def __call__(self, stage: str, _probe_request: ProbeRequest) -> dict[str, Any]:
        if stage == "wav":
            return await self._wav_stage(_probe_request)
        if stage == "cloud_input":
            return await self._cloud_input_stage(_probe_request)
        if stage == "cloud_response":
            return await self._cloud_response_stage()
        if stage == "cancel":
            return await self._cancel_stage()
        if stage == "continuation":
            return await self._continuation_stage()
        if stage == "history":
            return await self._history_stage()
        if stage == "instruction_layers":
            return await self._instruction_layers_stage()
        raise ProbeStageError(stage, f"未知 protocol 阶段: {stage}")

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def _wav_stage(self, probe_request: ProbeRequest) -> dict[str, Any]:
        self.audio = read_wav(probe_request.wav or "")
        facts = self.audio.facts
        return {
            "path": str(probe_request.wav),
            "sha256": facts.audio_sha256,
            "frame_count": facts.frame_count,
            "duration_seconds": facts.duration_seconds,
            "sample_rate": facts.sample_rate,
            "channels": facts.channels,
            "sample_width": facts.sample_width,
        }


    async def _cloud_input_stage(self, _probe_request: ProbeRequest) -> dict[str, Any]:
        api_key = selected_env_value(self.config["api_key_env"])
        if not api_key:
            raise ProbeStageError(
                "cloud_input", f"环境变量 {self.config['api_key_env']} 未提供 API Key"
            )

        async def evidence_factory(endpoint: str, *, extra_headers: dict[str, str]):
            websocket = await _default_connect_factory(
                endpoint, extra_headers=extra_headers
            )
            return EvidenceWebSocket(
                websocket,
                protocol_events=self.protocol_events,
                unknown_events=self.unknown_events,
                source="real",
            )

        self.client = QwenRealtimeClient(
            endpoint=self.config["endpoint"],
            model=self.config["model"],
            voice=self.config["voice"],
            api_key=api_key,
            connect_factory=evidence_factory,
        )
        timeout = float(self.config["timeout"])
        async with asyncio.timeout(timeout):
            cloud_session_id = await self.client.connect(self.stable_instructions)
            await self.client.append_audio(
                base64.b64encode(self.audio.pcm_bytes).decode("ascii")
            )
            await self.client.commit_audio()
            committed = await self.client.wait_for_input_committed()
            final = await self.client.wait_for_final(committed.item_id)

        if not isinstance(final.transcript, str) or not final.transcript.strip():
            raise ProbeStageError("cloud_input", "云端未返回可用 final 转录")
        self.final_transcript = final.transcript

        import asyncpg

        self.pool = await asyncpg.create_pool(db_dsn_from_env(), min_size=1, max_size=1)
        hydration = await hydrate_voice_context(
            self.pool,
            session_id=uuid.UUID(str(self.config["session_id"])),
            message=self.final_transcript,
            stable_instructions_hash=hashlib.sha256(
                self.base_stable_instructions.encode("utf-8")
            ).hexdigest(),
            memory_limit=int(self.config.get("memory_limit", 10)),
        )
        self.per_response_instructions = (
            f"{self.stable_instructions}\n\n"
            f"{hydration.per_response_instructions}\n\n"
            f"Capability probe turn anchor: include the exact token "
            f"{_TURN_ANCHOR} in this reply."
        )
        return {
            "cloud_session_id": cloud_session_id,
            "item_id": committed.item_id,
            "final_transcript": _text_summary(self.final_transcript),
            "hydration": {
                "hydrate_ms": hydration.hydrate_ms,
                "memory_count": hydration.memory_count,
                "instructions": _text_summary(hydration.per_response_instructions),
            },
            "stable_instructions": _text_summary(self.stable_instructions),
            "m4_stable_instructions": _text_summary(self.base_stable_instructions),
        }

    async def _cloud_response_stage(self) -> dict[str, Any]:
        if self.client is None or self.per_response_instructions is None:
            raise ProbeStageError("cloud_response", "云输入阶段未完成")
        response_id = f"m4-probe-{uuid.uuid4()}"
        started = time.monotonic_ns() // 1_000_000
        await self.client.update_session_instructions(self.per_response_instructions)
        await self.client.create_first_response(response_id)
        result = await self._consume_response(response_id, started)
        self.first_response_transcript = self.last_response_transcript
        result["protocol_events"] = self.protocol_events
        result["unknown_events"] = self.unknown_events
        return result

    async def _cancel_stage(self) -> dict[str, Any]:
        if self.client is None or self.per_response_instructions is None:
            raise ProbeStageError("cancel", "云输入阶段未完成")
        response_id = f"m4-cancel-{uuid.uuid4()}"
        instructions = (
            f"{self.per_response_instructions}\n\n"
            "Capability probe: count slowly from one to twenty."
        )
        started = time.monotonic_ns() // 1_000_000
        await self.client.update_session_instructions(instructions)
        await self.client.create_first_response(response_id)
        created = await self._wait_for_response_created(response_id)
        if created is None:
            raise ProbeStageError("cancel", "云端未确认待取消 response")
        await self.client.cancel_response()
        terminal = await self.client.wait_for_response_terminal(
            created.response_id, timeout=float(self.config["timeout"])
        )
        supported = terminal.type == "response.done" and terminal.status == "cancelled"
        if not supported:
            reason = (
                terminal.error_message
                or f"取消终态未确认: type={terminal.type} status={terminal.status}"
            )
            raise ProbeStageError("cancel", reason)
        return {
            "response_id": response_id,
            "status": terminal.status,
            "elapsed_ms": time.monotonic_ns() // 1_000_000 - started,
            "protocol_events": self.protocol_events,
            "unknown_events": self.unknown_events,
        }

    async def _continuation_stage(self) -> dict[str, Any]:
        if self.client is None or self.audio is None or self.per_response_instructions is None:
            raise ProbeStageError("continuation", "前置云会话未完成")
        response_id = f"m4-continuation-{uuid.uuid4()}"
        started = time.monotonic_ns() // 1_000_000
        await self.client.append_audio(
            base64.b64encode(self.audio.pcm_bytes).decode("ascii")
        )
        await self.client.commit_audio()
        committed = await self.client.wait_for_input_committed()
        final = await self.client.wait_for_final(committed.item_id)
        if not isinstance(final.transcript, str) or not final.transcript.strip():
            raise ProbeStageError("continuation", "续问未返回可用 final 转录")
        await self.client.update_session_instructions(self.per_response_instructions)
        await self.client.create_first_response(response_id)
        result = await self._consume_response(response_id, started)
        result["final_transcript"] = _text_summary(final.transcript)
        result["protocol_events"] = self.protocol_events
        result["unknown_events"] = self.unknown_events
        return result

    async def _history_stage(self) -> dict[str, Any]:
        if (
            self.client is None
            or self.audio is None
            or self.per_response_instructions is None
            or self.final_transcript is None
            or self.first_response_transcript is None
        ):
            raise ProbeStageError("history", "受控重建缺少已确认历史")
        await self.client.close()
        self.client = None

        api_key = selected_env_value(self.config["api_key_env"])
        if not api_key:
            raise ProbeStageError(
                "history", f"环境变量 {self.config['api_key_env']} 未提供 API Key"
            )

        async def evidence_factory(endpoint: str, *, extra_headers: dict[str, str]):
            websocket = await _default_connect_factory(
                endpoint, extra_headers=extra_headers
            )
            return EvidenceWebSocket(
                websocket,
                protocol_events=self.protocol_events,
                unknown_events=self.unknown_events,
                source="real",
            )

        self.client = QwenRealtimeClient(
            endpoint=self.config["endpoint"],
            model=self.config["model"],
            voice=self.config["voice"],
            api_key=api_key,
            connect_factory=evidence_factory,
        )
        response_id = f"m4-history-{uuid.uuid4()}"
        started = time.monotonic_ns() // 1_000_000
        async with asyncio.timeout(float(self.config["timeout"])):
            cloud_session_id = await self.client.connect(self.stable_instructions)
            await self.client.append_audio(
                base64.b64encode(self.audio.pcm_bytes).decode("ascii")
            )
            await self.client.commit_audio()
            committed = await self.client.wait_for_input_committed()
            final = await self.client.wait_for_final(committed.item_id)
        if not isinstance(final.transcript, str) or not final.transcript.strip():
            raise ProbeStageError("history", "受控重建未返回可用 final 转录")

        reconstructed_instructions = (
            f"{self.per_response_instructions}\n\n"
            "## Confirmed Prior Conversation\n"
            f"User: {self.final_transcript}\n"
            f"Assistant: {self.first_response_transcript}\n"
            "The prior conversation above is confirmed text history. Do not "
            "replay its audio or recall it as a new user request."
        )
        await self.client.update_session_instructions(reconstructed_instructions)
        await self.client.create_first_response(response_id)
        result = await self._consume_response(response_id, started)
        result.update(
            {
                "history_mode": "controlled_rebuild",
                "cloud_session_id": cloud_session_id,
                "final_transcript": _text_summary(final.transcript),
                "confirmed_history": {
                    "user": _text_summary(self.final_transcript),
                    "assistant": _text_summary(self.first_response_transcript),
                },
                "protocol_events": self.protocol_events,
                "unknown_events": self.unknown_events,
            }
        )
        return result

    async def _instruction_layers_stage(self) -> dict[str, Any]:
        observations = self.instruction_anchor_results
        if not observations:
            raise ProbeStageError("instruction_layers", "没有可判定的指令层锚点回执")
        failed = [
            index
            for index, anchors in enumerate(observations, start=1)
            if anchors.get("stable") is not True or anchors.get("per_turn") is not True
        ]
        if failed:
            raise ProbeStageError(
                "instruction_layers",
                "稳定层与每轮层指令锚点未同时生效: "
                f"failed_responses={failed}",
                details={
                    "observations": observations,
                    "response_transcript": _text_summary(
                        self.last_response_transcript or ""
                    ),
                },
            )
        return {
            "status": "passed",
            "observations": observations,
            "rendering": "stable_plus_per_turn",
        }

    async def _wait_for_response_created(self, response_id: str) -> Any:
        async for event in self.client.events():
            if event.type == "response.created" and event.response_id:
                return event
            if event.type in {"response.done", "response_failed"}:
                return None
        return None

    async def _consume_response(self, response_id: str, started_ms: int) -> dict[str, Any]:
        audio_chunks = 0
        audio_bytes = 0
        transcript_parts: list[str] = []
        first_delta_ms: int | None = None
        terminal: Any | None = None
        async for event in self.client.events():
            elapsed_ms = time.monotonic_ns() // 1_000_000 - started_ms
            if event.type in {
                "response.audio.delta",
                "response_audio_transcript_delta",
                "response_audio_transcript_done",
            } and first_delta_ms is None:
                first_delta_ms = elapsed_ms
            if event.type == "response.audio.delta" and event.audio:
                audio_chunks += 1
                audio_bytes += len(event.audio) * 3 // 4
            if event.type == "response_audio_transcript_delta" and event.text:
                transcript_parts.append(event.text)
            if event.type == "response_audio_transcript_done" and event.text:
                transcript_parts = [event.text]
            if event.type in {"response.done", "response_failed"}:
                terminal = event
                break
        if terminal is None:
            raise ProbeStageError("cloud_response", "云端连接结束前未收到终态")
        if terminal.type != "response.done":
            raise ProbeStageError(
                "cloud_response", terminal.error_message or "云端 response 未成功完成"
            )
        transcript = "".join(transcript_parts)
        self.last_response_transcript = transcript or None
        anchors = {
            "stable": _STABLE_ANCHOR in transcript,
            "per_turn": _TURN_ANCHOR in transcript,
        }
        self.instruction_anchor_results.append(anchors)
        return {
            "response_id": terminal.response_id or response_id,
            "status": terminal.status,
            "audio_chunks": audio_chunks,
            "audio_bytes": audio_bytes,
            "transcript": _text_summary(transcript) if transcript else None,
            "instruction_anchors": anchors,
            "first_delta_ms": first_delta_ms,
            "terminal_ms": time.monotonic_ns() // 1_000_000 - started_ms,
        }

class DevicesProbe:
    """Run the real browser microphone and local playback capability probe."""

    def __init__(self) -> None:
        self.reference: dict[str, Any] | None = None

    async def __call__(self, stage: str, probe_request: ProbeRequest) -> dict[str, Any]:
        if stage == "reference_wav":
            return self._reference_wav_stage(probe_request)
        if stage == "browser_probe":
            return self._browser_probe_stage(probe_request)
        raise ProbeStageError(stage, f"未知 devices 阶段: {stage}")

    def _reference_wav_stage(self, probe_request: ProbeRequest) -> dict[str, Any]:
        path = probe_request.reference_wav
        if path is None:
            raise ProbeStageError("reference_wav", "缺少参考 WAV")
        try:
            with wave.open(str(path), "rb") as handle:
                channels = handle.getnchannels()
                sample_width = handle.getsampwidth()
                sample_rate = handle.getframerate()
                frame_count = handle.getnframes()
                if channels != 1 or sample_width != 2 or sample_rate != 24000:
                    raise ProbeStageError(
                        "reference_wav", "参考 WAV 必须是 24kHz mono 16-bit PCM"
                    )
        except wave.Error as exc:
            raise ProbeStageError("reference_wav", f"参考 WAV 读取失败: {exc}") from exc
        except FileNotFoundError as exc:
            raise ProbeStageError("reference_wav", f"参考 WAV 不存在: {path}") from exc
        self.reference = {
            "path": str(path),
            "sha256": _sha256_file(path),
            "sample_rate": sample_rate,
            "channels": channels,
            "sample_width": sample_width,
            "frame_count": frame_count,
            "duration_seconds": frame_count / sample_rate,
        }
        return self.reference

    def _browser_probe_stage(self, _probe_request: ProbeRequest) -> dict[str, Any]:
        if self.reference is None:
            raise ProbeStageError("browser_probe", "参考 WAV 阶段未完成")
        evidence_path = Path(
            "/Users/itboybob/.cache/hexis/m4-round1/devices-playwright.json"
        )
        environment = os.environ.copy()
        environment["M4_REFERENCE_WAV"] = self.reference["path"]
        environment["M4_DEVICE_EVIDENCE"] = str(evidence_path)
        environment["M4_DEVICE_CASE"] = "speaker"
        result = subprocess.run(
            [
                "npm",
                "run",
                "test:e2e",
                "--",
                "e2e/voice-capability.spec.ts",
                "--headed",
            ],
            cwd=PROJECT_ROOT / "hexis-ui",
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            reason = result.stderr.strip() or result.stdout.strip()
            if evidence_path.is_file():
                browser_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                capture = browser_evidence.get("capture", {})
                if capture.get("status") != "granted":
                    reason = capture.get("reason") or reason
            raise ProbeStageError("browser_probe", f"真实设备 probe 失败: {reason}")
        if not evidence_path.is_file():
            raise ProbeStageError("browser_probe", "真实设备 probe 未落证据")
        return json.loads(evidence_path.read_text(encoding="utf-8"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one explicitly selected M4 voice capability probe."
    )
    parser.add_argument(
        "--mode", required=True, choices=sorted(_MODES), help="probe capability group"
    )
    parser.add_argument("--wav", help="16kHz mono PCM16 input WAV")
    parser.add_argument("--reference-wav", help="24kHz mono PCM16 reference WAV")
    parser.add_argument("--output", required=True, help="sanitized JSON evidence path")
    return parser.parse_args(argv)


def selected_env_value(name: str) -> str | None:
    value = get_key(PROJECT_ROOT / ".env", name)
    if value is None:
        value = os.environ.get(name)
    return value


def _config_summary() -> dict[str, Any]:
    selected: dict[str, Any] = {}
    if _SELECTED_REALTIME_CONFIG.is_file():
        selected = json.loads(_SELECTED_REALTIME_CONFIG.read_text(encoding="utf-8"))
    endpoint = selected.get("endpoint") or selected_env_value(
        "QWEN_OMNI_REALTIME_URL"
    )
    model = selected.get("model") or selected_env_value("QWEN_REALTIME_MODEL")
    voice = selected.get("voice") or selected_env_value("QWEN_REALTIME_VOICE")
    api_key_env = selected.get("api_key_env") or "DASHSCOPE_API_KEY"
    value = {
        "endpoint": endpoint,
        "model": model,
        "voice": voice,
        "api_key_env": api_key_env,
        "db_source": selected.get("db_source") or "hexis-env",
        "source": str(_SELECTED_REALTIME_CONFIG),
    }
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True)
    value["config_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return value


def _collect_secrets(value: Any) -> list[str]:
    secrets: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE_KEY.search(key_text) and isinstance(item, str) and item:
                secrets.append(item)
            secrets.extend(_collect_secrets(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            secrets.extend(_collect_secrets(item))
    return secrets


def sanitize_probe_evidence(value: Any) -> Any:
    """Redact sensitive keys and remove secret values from diagnostic strings."""
    secrets = sorted(set(_collect_secrets(value)), key=len, reverse=True)

    def sanitize(item: Any) -> Any:
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, nested in item.items():
                key_text = str(key)
                if _SENSITIVE_KEY.search(key_text) and not key_text.lower().endswith(
                    "_env"
                ):
                    result[str(key)] = "[REDACTED]"
                else:
                    result[str(key)] = sanitize(nested)
            return result
        if isinstance(item, (list, tuple)):
            return [sanitize(nested) for nested in item]
        if isinstance(item, str):
            text = item
            for secret in secrets:
                text = text.replace(secret, "[REDACTED]")
            return text
        return item

    return sanitize(value)


def _git_head() -> dict[str, Any]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return {"head": head, "dirty": bool(status), "changed_files": status}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _dependency_version(module_name: str) -> str:
    module = importlib.import_module(module_name)
    return str(getattr(module, "__version__", "unknown"))


def _playwright_version() -> str:
    result = subprocess.run(
        ["npm", "exec", "playwright", "--", "--version"],
        cwd=PROJECT_ROOT / "hexis-ui",
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise ProbeStageError(
            "playwright",
            f"Playwright 不可用: {result.stderr.strip() or result.stdout.strip()}",
        )
    return result.stdout.strip()


async def _database_status() -> dict[str, Any]:
    import asyncpg
    from core.migrations import migration_status

    dsn = db_dsn_from_env()
    conn = await asyncpg.connect(dsn)
    try:
        status = await migration_status(conn)
    finally:
        await conn.close()
    return {
        "applied_count": len(status["applied"]),
        "pending": status["pending"],
        "drifted": status["drifted"],
        "latest_applied": status["applied"][-1] if status["applied"] else None,
    }


def _evidence_coverage() -> dict[str, Any]:
    expected = {
        "batch3_success": Path(
            "/Users/itboybob/.cache/hexis/batch3-serial-voice-2026-10-05/real-cloud-2.json"
        ),
        "batch3_audio": Path(
            "/Users/itboybob/.cache/hexis/batch3-serial-voice-2026-10-05/cloud-output-2.wav"
        ),
        "m4_manifest": Path(
            "/Users/itboybob/.cache/hexis/m4-test-audio-2026-10-05/manifest.json"
        ),
    }
    files = []
    gaps = []
    for name, path in expected.items():
        if path.is_file():
            files.append({"name": name, "path": str(path), "sha256": _sha256_file(path)})
        else:
            gaps.append({"name": name, "path": str(path), "status": "missing"})
    if gaps:
        raise ProbeStageError(
            "evidence_coverage",
            "M2/M3/M4 素材证据缺失: " + ", ".join(item["name"] for item in gaps),
        )
    return {"files": files, "gaps": []}


async def _preflight_stage(stage: str, probe_request: ProbeRequest) -> dict[str, Any]:
    if stage == "worktree":
        return _git_head()
    if stage == "wav":
        audio = read_wav(probe_request.wav or "")
        return {
            "path": str(probe_request.wav),
            "sha256": audio.facts.audio_sha256,
            "frame_count": audio.facts.frame_count,
            "duration_seconds": audio.facts.duration_seconds,
            "sample_rate": audio.facts.sample_rate,
            "channels": audio.facts.channels,
            "sample_width": audio.facts.sample_width,
        }
    if stage == "dependencies":
        return {
            "asyncpg": _dependency_version("asyncpg"),
            "websockets": _dependency_version("websockets"),
            "pydantic": _dependency_version("pydantic"),
        }
    if stage == "playwright":
        return {"version": _playwright_version()}
    if stage == "database":
        return await _database_status()
    if stage == "evidence_coverage":
        return _evidence_coverage()
    raise ProbeStageError(stage, f"未知 preflight 阶段: {stage}")


async def _not_implemented_stage(stage: str, _probe_request: ProbeRequest) -> dict[str, Any]:
    raise ProbeStageError(stage, "该能力探针尚未实现，不能伪造通过")


def _stage_runner(mode: str) -> Callable[[str, ProbeRequest], Awaitable[dict[str, Any]]]:
    if mode == "preflight":
        return _preflight_stage
    if mode == "protocol":
        return ProtocolProbe()
    if mode == "devices":
        return DevicesProbe()
    return _not_implemented_stage


def write_probe_evidence(path: Path, evidence: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


async def run_probe(
    probe_request: ProbeRequest,
    *,
    stage_runner: Callable[[str, ProbeRequest], Awaitable[dict[str, Any]]],
    head_provider: Callable[[], Mapping[str, Any]] = _git_head,
) -> ProbeRunResult:
    probe_request.validate()
    git_state = dict(head_provider())
    if git_state.get("head") != probe_request.expected_baseline:
        raise ProbeBaselineError(
            "代码基线与 M4 计划不一致: "
            f"expected={probe_request.expected_baseline} actual={git_state.get('head')}"
        )

    evidence: dict[str, Any] = {
        "schema": SCHEMA,
        "mode": probe_request.mode,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "baseline": {
            "expected": probe_request.expected_baseline,
            "actual": git_state.get("head"),
            "dirty": bool(git_state.get("dirty")),
        },
        "config": _config_summary(),
        "stages": [],
    }
    failed = False
    for stage in _STAGES[probe_request.mode]:
        try:
            details = await stage_runner(stage, probe_request)
            evidence["stages"].append(
                {"stage": stage, "status": "passed", "details": details}
            )
        except ProbeStageError as exc:
            failed_stage: dict[str, Any] = {
                "stage": stage,
                "status": "failed",
                "reason": exc.reason,
            }
            if exc.details is not None:
                failed_stage["details"] = exc.details
            evidence["stages"].append(failed_stage)
            failed = True
            break
        except Exception as exc:
            failed_stage = {
                "stage": stage,
                "status": "failed",
                "reason": f"{type(exc).__name__}: {exc}",
            }
            protocol_events = getattr(stage_runner, "protocol_events", None)
            unknown_events = getattr(stage_runner, "unknown_events", None)
            if protocol_events or unknown_events:
                failed_stage["details"] = {
                    "protocol_events": protocol_events or [],
                    "unknown_events": unknown_events or [],
                }
            evidence["stages"].append(failed_stage)
            failed = True
            break

    evidence["status"] = "failed" if failed else "passed"
    if hasattr(stage_runner, "close"):
        await stage_runner.close()
    sanitized = sanitize_probe_evidence(evidence)
    write_probe_evidence(probe_request.output, sanitized)
    return ProbeRunResult(ok=not failed, evidence=sanitized)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    probe_request = ProbeRequest(
        mode=args.mode,
        wav=Path(args.wav).expanduser() if args.wav else None,
        reference_wav=(
            Path(args.reference_wav).expanduser() if args.reference_wav else None
        ),
        output=Path(args.output).expanduser(),
    )
    try:
        result = asyncio.run(
            run_probe(
                probe_request,
                stage_runner=_stage_runner(probe_request.mode),
            )
        )
    except (ProbeBaselineError, ValueError) as exc:
        print(f"M4 probe 拒绝执行: {exc}", file=sys.stderr)
        return 2
    if not result.ok:
        print(f"M4 probe 失败，证据已写入: {probe_request.output}", file=sys.stderr)
        return 1
    print(f"M4 probe 通过，证据已写入: {probe_request.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
