"""Controlled probe for the realtime emotion evaluation and hydration gate."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

import asyncpg
from dotenv import get_key

from core.agent_api import db_dsn_from_env
from services.avatar_emotion import evaluate as evaluate_emotion
from services.avatar_emotion import read_db_context
from services.behavior_events import BehaviorEvent, record_behavior_event
from services.voice_context_hydration import hydrate_voice_context
from services.voice_emotion_gate import VoiceEmotionCoordinator
from services.voice_first_response import FirstResponseGate, GateNotReady


SCHEMA = "voice-emotion-probe-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_EVENT_NAMESPACE = uuid5(UUID(int=0), "hexis:bob102:emotion-probe")
_PROBE_EVENT_TYPES = (
    "voice_emotion_probe.started",
    "voice_emotion_probe.done",
    "voice_hydrate_probe.started",
    "voice_hydrate_probe.done",
    "voice_gate_probe.released",
)


class ProbeConfigError(ValueError):
    """The explicitly supplied probe inputs are incomplete or invalid."""


@dataclass(frozen=True)
class ProbeConfig:
    provider: str
    model: str
    endpoint: str
    api_key_env: str
    db_source: str
    db_dsn_env: str | None
    emotion_timeout_ms: float
    memory_limit: int
    stable_instructions: str
    sample_count: int

    @classmethod
    def from_inputs(
        cls, config: Mapping[str, Any], messages: Mapping[str, Any]
    ) -> "ProbeConfig":
        if not isinstance(config, Mapping):
            raise ProbeConfigError("config must be a JSON object")
        emotion = config.get("emotion")
        if not isinstance(emotion, Mapping):
            raise ProbeConfigError("config.emotion must be a JSON object")

        provider = _required_text(emotion.get("provider"), "provider")
        model = _required_text(emotion.get("model"), "model")
        endpoint = _required_text(emotion.get("endpoint"), "endpoint")
        api_key_env = _required_text(emotion.get("api_key_env"), "api_key_env")
        if not _ENV_NAME.fullmatch(api_key_env):
            raise ProbeConfigError("api_key_env must be an environment variable name")

        timeout = emotion.get("timeout_ms")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ProbeConfigError("timeout_ms must be a positive number")
        if not math.isfinite(float(timeout)) or timeout <= 0:
            raise ProbeConfigError("timeout_ms must be a positive finite number")

        db_dsn_env = config.get("db_dsn_env")
        db_source = config.get("db_source")
        if db_dsn_env is not None:
            if db_source is not None:
                raise ProbeConfigError("use either db_dsn_env or db_source, not both")
            db_dsn_env = _required_text(db_dsn_env, "db_dsn_env")
            if not _ENV_NAME.fullmatch(db_dsn_env):
                raise ProbeConfigError("db_dsn_env must be an environment variable name")
            db_source = db_dsn_env
        elif db_source != "hexis-env":
            raise ProbeConfigError("db_source must be hexis-env or db_dsn_env")

        memory_limit = config.get("memory_limit")
        if (
            isinstance(memory_limit, bool)
            or not isinstance(memory_limit, int)
            or memory_limit <= 0
        ):
            raise ProbeConfigError("memory_limit must be a positive integer")
        stable_instructions = _required_text(
            config.get("stable_instructions"), "stable_instructions"
        )

        if not isinstance(messages, Mapping):
            raise ProbeConfigError("messages must be a JSON object")
        samples = messages.get("messages")
        if not isinstance(samples, list) or not samples:
            raise ProbeConfigError("messages 必须至少包含一条样本")
        sample_ids = []
        for sample in samples:
            if not isinstance(sample, Mapping):
                raise ProbeConfigError("each message must be a JSON object")
            sample_ids.append(_required_text(sample.get("id"), "message id"))
            _required_text(sample.get("text"), "message text")
        if len(set(sample_ids)) != len(sample_ids):
            raise ProbeConfigError("message ids must be unique")

        return cls(
            provider=provider,
            model=model,
            endpoint=endpoint,
            api_key_env=api_key_env,
            db_source=str(db_source),
            db_dsn_env=db_dsn_env,
            emotion_timeout_ms=float(timeout),
            memory_limit=memory_limit,
            stable_instructions=stable_instructions,
            sample_count=len(samples),
        )


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ProbeConfigError(f"{name} must be a non-empty trimmed string")
    return value


def _load_json(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProbeConfigError(f"input file does not exist: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ProbeConfigError(f"input file is not valid JSON: {source}") from exc
    if not isinstance(value, dict):
        raise ProbeConfigError(f"input file must contain a JSON object: {source}")
    return value


def resolve_db_dsn(parsed: ProbeConfig) -> str:
    if parsed.db_dsn_env is not None:
        dsn = _selected_env_value(parsed.db_dsn_env)
        if not dsn:
            raise ProbeConfigError(
                f"environment variable {parsed.db_dsn_env} does not provide a DSN"
            )
        return dsn
    return db_dsn_from_env()


def _selected_env_value(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
    env_file: str | Path | None = None,
) -> str | None:
    value = get_key(Path(env_file or PROJECT_ROOT / ".env"), name)
    if value is None:
        value = (os.environ if environ is None else environ).get(name)
    return value


def resolve_api_key(
    parsed: ProbeConfig,
    *,
    environ: Mapping[str, str] | None = None,
    env_file: str | Path | None = None,
) -> str:
    value = _selected_env_value(
        parsed.api_key_env, environ=environ, env_file=env_file
    )
    if not value:
        raise ProbeConfigError(
            f"environment variable {parsed.api_key_env} does not provide an API key"
        )
    return value


def _text_fact(value: str) -> dict[str, Any]:
    return {
        "length": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
    }


def sanitize_evaluation(evaluation: Any) -> dict[str, Any]:
    if not isinstance(evaluation, Mapping):
        return {"status": "unknown"}
    status = evaluation.get("status")
    result: dict[str, Any] = {
        "status": status if isinstance(status, str) else "unknown",
    }
    value = evaluation.get("value")
    if isinstance(value, Mapping):
        label = value.get("label")
        if isinstance(label, str):
            result["label"] = label
        wordset_version = value.get("wordset_version")
        if isinstance(wordset_version, str):
            result["wordset_version"] = wordset_version
        if isinstance(value.get("logical_call_count"), int):
            result["logical_call_count"] = value["logical_call_count"]
        transport_attempts = value.get("transport_attempts")
        if transport_attempts is None or isinstance(transport_attempts, int):
            result["transport_attempts"] = transport_attempts
    error_type = evaluation.get("error_type")
    if isinstance(error_type, str):
        result["error_type"] = error_type
    reason = evaluation.get("reason")
    if isinstance(reason, str):
        result["reason"] = reason
    return result


def sanitize_hydration(hydration: Any) -> dict[str, Any]:
    if hydration is None:
        return {"available": False}
    instructions = getattr(hydration, "per_response_instructions", None)
    if not isinstance(instructions, str) or not instructions.strip():
        return {"available": False}
    result: dict[str, Any] = {
        "available": True,
        "instructions_length": len(instructions),
        "instructions_sha256": hashlib.sha256(
            instructions.encode("utf-8")
        ).hexdigest(),
    }
    instructions_hash = getattr(hydration, "instructions_hash", None)
    if isinstance(instructions_hash, str):
        result["instructions_hash"] = instructions_hash
    if isinstance(getattr(hydration, "memory_count", None), int):
        result["memory_count"] = hydration.memory_count
    return result


def sanitize_late_diagnostics(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in value:
        if not hasattr(item, "identity"):
            continue
        diagnostic: dict[str, Any] = {
            "identity": dict(item.identity),
            "status": item.status,
            "accepted": bool(item.accepted),
            "rejection_reason": item.rejection_reason,
            "logical_call_count": item.logical_call_count,
        }
        if item.error_type is not None:
            diagnostic["error_type"] = item.error_type
        if item.status == "valid" and isinstance(item.value, Mapping):
            label = item.value.get("label")
            if isinstance(label, str):
                diagnostic["label"] = label
        result.append(diagnostic)
    return result


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(
        0,
        min(len(ordered) - 1, math.ceil(percentile / 100 * len(ordered)) - 1),
    )
    return ordered[index]


def summarize_samples(records: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(records)
    eval_values = [float(record["metrics"]["eval_ms"]) for record in records]
    hydrate_values = [float(record["metrics"]["hydrate_ms"]) for record in records]
    gate_values = [float(record["metrics"]["gate_wait_ms"]) for record in records]
    success_count = sum(
        record["evaluation"].get("status") == "valid" for record in records
    )
    failure_count = sum(
        record["evaluation"].get("status") == "error" for record in records
    )
    timeout_count = sum(
        record["metrics"].get("release_reason") == "model_timeout"
        for record in records
    )
    unknown_count = sum(
        record["evaluation"].get("status") not in {"valid", "error", "timeout"}
        for record in records
    )
    transport_unknown_count = sum(
        record["evaluation"].get("transport_attempts") is None for record in records
    )

    def distribution(values: list[float]) -> dict[str, float | None]:
        return {"p50": _percentile(values, 50), "p95": _percentile(values, 95)}

    return {
        "sample_count": count,
        "eval_ms": distribution(eval_values),
        "hydrate_ms": distribution(hydrate_values),
        "gate_wait_ms": distribution(gate_values),
        "success_count": success_count,
        "failure_count": failure_count,
        "timeout_release_count": timeout_count,
        "unknown_count": unknown_count,
        "transport_attempts_unknown_count": transport_unknown_count,
        "success_rate": success_count / count if count else None,
        "failure_rate": failure_count / count if count else None,
        "timeout_release_rate": timeout_count / count if count else None,
        "unknown_rate": unknown_count / count if count else None,
        "sample_sufficient": count >= 30,
        "note": "样本不足，分布仅描述本次实际样本，不外推"
        if count < 30
        else "样本满足最小统计要求",
    }


class ABehaviorEventSink:
    """Producer-stable probe events; A remains the dedup authority."""

    def __init__(
        self,
        pool: Any,
        recorder: Callable[
            [Any, BehaviorEvent], Awaitable[dict[str, Any]]
        ] = record_behavior_event,
    ) -> None:
        self.pool = pool
        self.recorder = recorder
        self.receipts: list[dict[str, Any]] = []
        self.event_types: list[str] = []

    async def record(self, event: BehaviorEvent) -> dict[str, Any]:
        receipt = await self.recorder(self.pool, event)
        if not isinstance(receipt, dict):
            raise RuntimeError("behavior event recorder did not return a receipt")
        self.receipts.append(receipt)
        self.event_types.append(event.event_type)
        return receipt

    async def __call__(self, event: BehaviorEvent) -> dict[str, Any]:
        return await self.record(event)

    def make_event(
        self,
        identity: Mapping[str, Any],
        *,
        event_type: str,
        producer_seq: int,
        payload: dict[str, Any],
    ) -> BehaviorEvent:
        if event_type not in _PROBE_EVENT_TYPES:
            raise ValueError(f"unsupported probe event type: {event_type}")
        canonical = json.dumps(
            [
                identity.get("session_id"),
                identity.get("input_turn_id"),
                identity.get("input_order"),
                identity.get("stimulus_revision"),
                event_type,
                producer_seq,
            ],
            sort_keys=True,
        )
        return BehaviorEvent(
            event_id=uuid5(_EVENT_NAMESPACE, canonical),
            schema_version=1,
            event_type=event_type,
            stream_id="voice-emotion-probe",
            session_id=UUID(str(identity["session_id"])),
            source="server",
            producer_id="hexis.voice-emotion-probe",
            producer_seq=producer_seq,
            channel="voice",
            payload=payload,
            ts=datetime.now(timezone.utc),
            stimulus_revision=int(identity["stimulus_revision"]),
            input_turn_id=str(identity["input_turn_id"]),
        )


class BFirstResponseAdapter:
    """Give only released/context_ready facts to the one-shot B gate."""

    def __init__(self, response_create: Callable[[], None] | None = None) -> None:
        self.response_create = response_create

    def apply(self, facts: Mapping[str, Any]) -> dict[str, Any]:
        released = facts.get("released") is True
        context_ready = facts.get("context_ready") is True
        gate = FirstResponseGate()
        gate.mark_committed()
        if released:
            gate.mark_emotion_released()
        if context_ready:
            gate.mark_context_ready()
        ready = False
        try:
            gate.assert_ready()
            ready = True
        except GateNotReady:
            pass
        called = False
        if ready and self.response_create is not None:
            self.response_create()
            called = True
        return {
            "released": released,
            "context_ready": context_ready,
            "ready": ready,
            "response_create_called": called,
        }


def make_sample_record(
    result: Any,
    b_outcome: Mapping[str, Any],
    event_types: list[str],
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "identity": dict(result.identity),
        "evaluation": sanitize_evaluation(result.evaluation),
        "hydration": sanitize_hydration(result.hydration),
        "visual_window": dict(result.visual_window or {}),
        "metrics": dict(result.metrics),
        "late_diagnostics": sanitize_late_diagnostics(result.late_diagnostics),
        "b_gate_facts": {
            "released": bool(result.released),
            "context_ready": bool(result.context_ready),
        },
        "first_response": dict(b_outcome),
        "behavior_events": {
            "types": list(event_types),
            "receipts": [
                {
                    "seq": receipt.get("seq"),
                    "deduplicated": receipt.get("deduplicated"),
                }
                for receipt in receipts
            ],
        },
    }


def write_evidence_exclusively(path: str | Path, evidence: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as handle:
        json.dump(evidence, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _device_conditions() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python_version": platform.python_version(),
    }


def _config_evidence(
    parsed: ProbeConfig,
    *,
    config_path: str | Path,
    messages_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    return {
        "config_path": str(Path(config_path)),
        "config_name": Path(config_path).name,
        "messages_path": str(Path(messages_path)),
        "output_path": str(Path(output_path)),
        "provider": parsed.provider,
        "model": parsed.model,
        "endpoint": parsed.endpoint,
        "api_key_env": parsed.api_key_env,
        "api_key_value_recorded": False,
        "db_source": parsed.db_source,
        "dsn_recorded": False,
        "emotion_timeout_ms": parsed.emotion_timeout_ms,
        "memory_limit": parsed.memory_limit,
        "sample_count": parsed.sample_count,
        "stable_instructions": _text_fact(parsed.stable_instructions),
    }


async def _run_sample(
    *,
    pool: asyncpg.Pool,
    parsed: ProbeConfig,
    api_key: str,
    sink: ABehaviorEventSink,
    adapter: BFirstResponseAdapter,
    stable_instructions_hash: str,
    identity: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    receipt_start = len(sink.receipts)
    event_start = len(sink.event_types)

    async def context_reader(value: Mapping[str, Any]) -> Any:
        await sink.record(
            sink.make_event(
                value,
                event_type="voice_emotion_probe.started",
                producer_seq=0,
                payload={"stage": "context_read"},
            )
        )
        async with pool.acquire() as conn:
            return await read_db_context(conn)

    async def evaluator(text: str, context: Any) -> Any:
        return await evaluate_emotion(
            text,
            context,
            {
                "provider": parsed.provider,
                "model": parsed.model,
                "endpoint": parsed.endpoint,
                "resolved_api_key": api_key,
                "timeout_seconds": parsed.emotion_timeout_ms / 1000,
                "host_managed_timeout": True,
            },
        )

    async def hydrator(value: Mapping[str, Any], text: str) -> Any:
        await sink.record(
            sink.make_event(
                value,
                event_type="voice_hydrate_probe.started",
                producer_seq=0,
                payload={"stage": "hydrate"},
            )
        )
        try:
            hydration = await hydrate_voice_context(
                pool,
                session_id=UUID(str(value["session_id"])),
                message=text,
                stable_instructions_hash=stable_instructions_hash,
                memory_limit=parsed.memory_limit,
            )
        except Exception as exc:
            await sink.record(
                sink.make_event(
                    value,
                    event_type="voice_hydrate_probe.done",
                    producer_seq=1,
                    payload={
                        "available": False,
                        "error_type": type(exc).__name__,
                    },
                )
            )
            raise
        await sink.record(
            sink.make_event(
                value,
                event_type="voice_hydrate_probe.done",
                producer_seq=1,
                payload={
                    "available": True,
                    "hydrate_ms": hydration.hydrate_ms,
                    "memory_count": hydration.memory_count,
                },
            )
        )
        return hydration

    coordinator = VoiceEmotionCoordinator(
        context_reader=context_reader,
        evaluator=evaluator,
        hydrator=hydrator,
        emotion_timeout_ms=parsed.emotion_timeout_ms,
    )
    result = await coordinator.run(identity, message)
    task = coordinator.last_evaluation_task
    if task is not None and not task.done():
        await asyncio.wait({task}, timeout=0.1)

    sanitized_evaluation = sanitize_evaluation(result.evaluation)
    await sink.record(
        sink.make_event(
            identity,
            event_type="voice_emotion_probe.done",
            producer_seq=1,
            payload={
                "status": sanitized_evaluation["status"],
                "label": sanitized_evaluation.get("label"),
                "visual_window": result.visual_window,
                "metrics": result.metrics,
            },
        )
    )
    b_outcome = adapter.apply(result.to_b_gate_facts())
    await sink.record(
        sink.make_event(
            identity,
            event_type="voice_gate_probe.released",
            producer_seq=2,
            payload={
                "released": result.released,
                "context_ready": result.context_ready,
                "first_response_ready": b_outcome["ready"],
            },
        )
    )

    return make_sample_record(
        result,
        b_outcome,
        sink.event_types[event_start:],
        sink.receipts[receipt_start:],
    )


async def run_probe(
    config_path: str | Path,
    messages_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    config_raw = _load_json(config_path)
    messages_raw = _load_json(messages_path)
    parsed = ProbeConfig.from_inputs(config_raw, messages_raw)
    dsn = resolve_db_dsn(parsed)
    api_key = resolve_api_key(parsed)

    output = Path(output_path)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing evidence: {output}")

    stable_instructions_hash = hashlib.sha256(
        parsed.stable_instructions.encode("utf-8")
    ).hexdigest()
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        sink = ABehaviorEventSink(pool)
        adapter = BFirstResponseAdapter(response_create=lambda: None)
        session_id = uuid4()
        records = []
        for input_order, sample in enumerate(messages_raw["messages"]):
            identity = {
                "session_id": str(session_id),
                "input_turn_id": str(uuid4()),
                "input_order": input_order,
                "stimulus_revision": 0,
            }
            records.append(
                await _run_sample(
                    pool=pool,
                    parsed=parsed,
                    api_key=api_key,
                    sink=sink,
                    adapter=adapter,
                    stable_instructions_hash=stable_instructions_hash,
                    identity=identity,
                    message=sample["text"],
                )
            )

        evidence = {
            "schema": SCHEMA,
            "source": "real",
            "input_source": messages_raw.get("source"),
            "message_source": messages_raw.get("source"),
            "requirements": messages_raw.get("requirements", {}),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "started_at": started_at.isoformat(),
            "config": _config_evidence(
                parsed,
                config_path=config_path,
                messages_path=messages_path,
                output_path=output_path,
            ),
            "timeout": {
                "emotion_timeout_ms": parsed.emotion_timeout_ms,
                "start": "after atomic input_committed, when C starts evaluation and hydration",
                "source": "BOB-102 issue update, 2026-10-05",
            },
            "event_naming": {
                "types": list(_PROBE_EVENT_TYPES),
                "scope": "probe-only",
                "note": "仅探针命名；全局事件词表与活动归并以 BOB-102 issue 裁决为准",
            },
            "device": _device_conditions(),
            "network": {
                "evaluation_transport": "real",
                "provider": "aliyun",
                "independent_network_profile": False,
            },
            "records": records,
            "summary": summarize_samples(records),
        }
        write_evidence_exclusively(output, evidence)
        return evidence
    finally:
        await pool.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the explicitly configured BOB-102 emotion/hydration probe."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--messages", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        evidence = asyncio.run(
            run_probe(args.config, args.messages, args.output)
        )
    except (ProbeConfigError, FileExistsError) as exc:
        print(f"probe failed: {exc}")
        print("请修正显式输入后，使用新的输出路径重试。")
        return 1
    except Exception as exc:
        print(f"probe failed: {type(exc).__name__}")
        print("请检查数据库、模型服务与显式输入；证据未写入时使用新的输出路径重试。")
        return 1
    print(f"evidence written: {args.output}")
    print(f"samples: {evidence['summary']['sample_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
