"""One-shot controlled evidence harness for the BOB-101 voice path."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import time
import wave
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol
from uuid import UUID, uuid5

from core.agent_api import db_dsn_from_env
from services.behavior_events import BehaviorEvent, record_behavior_event
from services.avatar_emotion import evaluate as evaluate_emotion
from services.qwen_realtime_client import (
    QwenRealtimeClient,
    QwenRealtimeConnectionError,
)
from services.qwen_realtime_protocol import ProtocolEvent
from services.qwen_realtime_output import (
    NativeAudioChunk,
    OutputMappingError,
    OutputPlaybackFacts,
    QwenRealtimeOutputAdapter,
)
from services.voice_emotion_gate import VoiceEmotionCoordinator
from services.voice_context_hydration import hydrate_voice_context
from services.voice_first_response import FirstResponseGate
from services.voice_input_authorization import (
    CaptureRange,
    FinalTranscript,
    InputAuthorizationService,
    InputIdentity,
)

SCHEMA = "voice-bob101-harness-v1"
_REQUIRED_CONFIG = (
    "endpoint",
    "model",
    "voice",
    "api_key_env",
    "wav",
    "session_id",
    "input_turn_id",
    "timeout",
    "stable_instructions",
    "output_epoch",
)
_OPTIONAL_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_KNOWN_EVENT_TYPES = {
    "session.update",
    "session.updated",
    "input_audio_buffer.append",
    "input_audio_buffer.commit",
    "input_audio_buffer.committed",
    "input_audio_transcription.partial",
    "input_audio_transcription.completed",
    "conversation.item.input_audio_transcription.partial",
    "conversation.item.input_audio_transcription.completed",
    "response.create",
    "response.cancel",
    "response.created",
    "response.text.delta",
    "response.audio.delta",
    "response.audio.done",
    "response.audio_transcript.delta",
    "response.audio_transcript.done",
    "response.content_part.added",
    "response.content_part.done",
    "response.output_item.added",
    "response.output_item.done",
    "response.done",
    "response.failed",
    "error",
    "input_audio_transcription.failed",
    "conversation.item.input_audio_transcription.failed",
    "conversation.item.created",
    "conversation.item.input_audio_transcription.delta",
    "session.created",
}
_UUID_NAMESPACE = uuid5(UUID(int=0), "hexis:bob101:harness")


class HarnessStageError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


@dataclass(frozen=True)
class AudioFacts:
    audio_sha256: str
    frame_count: int
    duration_seconds: float
    sample_rate: int
    channels: int
    sample_width: int


@dataclass(frozen=True)
class AudioInput:
    facts: AudioFacts
    pcm_bytes: bytes


@dataclass(frozen=True)
class HarnessRunResult:
    ok: bool
    evidence: dict[str, Any]


class PlaybackBridge(Protocol):
    async def start(self, chunk: NativeAudioChunk) -> OutputPlaybackFacts:
        ...

    async def finish(self) -> OutputPlaybackFacts:
        ...


@dataclass(frozen=True)
class CGateFacts:
    released: bool
    context_ready: bool


@dataclass
class ABehaviorEventSink:
    """Map local facts to producer-stable events; A remains dedup authority."""

    pool: Any
    receipts: list[dict[str, Any]]
    event_types: list[str]

    def __init__(self, pool: Any):
        if pool is None:
            raise ValueError("a database pool is required to record behavior events")
        self.pool = pool
        self.receipts = []
        self.event_types = []

    async def __call__(self, event: BehaviorEvent) -> dict[str, Any]:
        mapped = replace(event, event_id=_stable_behavior_event_id(event))
        receipt = await record_behavior_event(self.pool, mapped)
        self.receipts.append(receipt)
        self.event_types.append(event.event_type)
        return receipt


@dataclass
class CEmotionReleaseAdapter:
    """Expose only C gate facts to B; visual windows stay diagnostic."""

    coordinator: VoiceEmotionCoordinator
    last_result: Any | None = None
    _task: asyncio.Task[Any] | None = None

    def __init__(self, coordinator: VoiceEmotionCoordinator):
        self.coordinator = coordinator
        self.last_result = None
        self._task = None

    def start(self, identity: Mapping[str, Any], message: str) -> None:
        if self._task is not None:
            raise RuntimeError("C release adapter has already started")
        self._task = asyncio.create_task(self.coordinator.run(identity, message))

    async def release(self) -> CGateFacts:
        result = await self._result()
        self.last_result = result
        return CGateFacts(
            released=bool(result.released),
            context_ready=bool(result.context_ready),
        )

    async def hydration(self) -> Any:
        result = await self._result()
        self.last_result = result
        return result.hydration

    async def _result(self) -> Any:
        if self._task is None:
            raise RuntimeError("C release adapter has not started")
        return await self._task


class EvidenceWebSocket:
    def __init__(
        self,
        websocket: Any,
        *,
        protocol_events: list[dict[str, Any]],
        unknown_events: list[dict[str, Any]],
        source: str,
    ) -> None:
        self.websocket = websocket
        self.protocol_events = protocol_events
        self.unknown_events = unknown_events
        self.source = source

    async def send(self, message: str) -> None:
        _record_raw(
            json.loads(message),
            protocol_events=self.protocol_events,
            unknown_events=self.unknown_events,
            source=self.source,
            direction="outgoing",
        )
        await self.websocket.send(message)

    async def recv(self) -> str:
        message = await self.websocket.recv()
        _record_raw(
            json.loads(message),
            protocol_events=self.protocol_events,
            unknown_events=self.unknown_events,
            source=self.source,
            direction="incoming",
        )
        return message

    async def close(self) -> None:
        await self.websocket.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one explicitly configured BOB-101 realtime voice turn."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--wav", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args(argv)


def read_wav(path: str | Path) -> AudioInput:
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            frame_count = handle.getnframes()
            if channels != 1 or sample_width != 2 or sample_rate != 16000:
                raise HarnessStageError("wav", "WAV 必须是 16kHz mono 16-bit PCM")
            pcm = handle.readframes(frame_count)
    except wave.Error as exc:
        raise HarnessStageError("wav", f"WAV 读取失败: {exc}") from exc
    except FileNotFoundError as exc:
        raise HarnessStageError("wav", f"WAV 文件不存在: {path}") from exc

    if frame_count <= 0:
        raise HarnessStageError("wav", "WAV 不包含音频帧")
    if len(pcm) != frame_count * channels * sample_width:
        raise HarnessStageError("wav", "WAV 帧长度不完整")
    return AudioInput(
        facts=AudioFacts(
            audio_sha256=hashlib.sha256(pcm).hexdigest(),
            frame_count=frame_count,
            duration_seconds=frame_count / sample_rate,
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
        ),
        pcm_bytes=pcm,
    )


def _text_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    return {
        "length": len(value),
        "sha256": hashlib.sha256(value.encode()).hexdigest(),
    }


def _preview_summary(value: Any, limit: int = 120) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    return {"length": len(value), "preview": value[:limit]}


def _audio_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    return {
        "base64_length": len(value),
        "sha256": hashlib.sha256(value.encode()).hexdigest(),
    }


def sanitize_protocol_event(
    event: ProtocolEvent, *, source: str, direction: str
) -> dict[str, Any]:
    raw = event.raw or {}
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()
    event_type = event.type
    payload: dict[str, Any] = {}

    if event_type == "session.updated":
        payload["session_id"] = (raw.get("session") or {}).get("id")
        payload["event_id"] = raw.get("event_id")
        payload["instructions"] = _text_summary(
            (raw.get("session") or {}).get("instructions")
        )
    elif event_type == "session.update":
        session = raw.get("session") or {}
        payload["event_id"] = raw.get("event_id")
        payload["instructions"] = _text_summary(session.get("instructions"))
    elif event_type == "input_audio_buffer.append":
        payload["audio"] = _audio_summary(raw.get("audio"))
    elif event_type == "response.create":
        response = raw.get("response") or {}
        payload["event_id"] = raw.get("event_id")
        payload["response_id"] = response.get("id")
    elif event_type == "response.cancel":
        pass
    elif event_type == "response.created":
        response = raw.get("response") or {}
        payload["response_id"] = response.get("id")
    elif event_type == "response.audio.delta":
        payload["audio"] = _audio_summary(raw.get("delta"))
    elif event_type == "response.text.delta":
        payload["text"] = _text_summary(raw.get("delta"))
    elif event_type in {
        "response.audio_transcript.delta",
        "response_audio_transcript_delta",
        "response.audio_transcript.done",
        "response_audio_transcript_done",
    }:
        payload["transcript"] = _preview_summary(
            raw.get("delta")
            if event_type.endswith("delta")
            else raw.get("transcript") or raw.get("text")
        )
    elif event_type in {
        "input_audio_transcription.partial",
        "input_audio_transcription.completed",
        "conversation.item.input_audio_transcription.partial",
        "conversation.item.input_audio_transcription.completed",
    }:
        payload["transcript"] = _preview_summary(raw.get("transcript"))
    elif event_type in {"response.done", "response.failed"}:
        response = raw.get("response") or {}
        payload["response_id"] = response.get("id") or raw.get("response_id")
        if event_type == "response.done" and isinstance(response, dict):
            payload["status"] = response.get("status")
        if event_type == "response.failed":
            error = response.get("error") if isinstance(response, dict) else None
            if isinstance(error, dict):
                payload["error_code"] = error.get("code")
                payload["error_message"] = error.get("message")
            elif isinstance(error, str):
                payload["error_message"] = error
    elif event_type == "error":
        error = raw.get("error")
        if isinstance(error, dict):
            payload["error_code"] = error.get("code")
            payload["error_message"] = error.get("message")
        elif isinstance(error, str):
            payload["error_message"] = error

    return {
        "type": event_type,
        "source": source,
        "direction": direction,
        "raw_size_bytes": len(canonical),
        "raw_sha256": hashlib.sha256(canonical).hexdigest(),
        "payload": payload,
    }


def _record_raw(
    value: Any,
    *,
    protocol_events: list[dict[str, Any]],
    unknown_events: list[dict[str, Any]],
    source: str,
    direction: str,
) -> None:
    event_type = value.get("type") if isinstance(value, dict) else None
    event = ProtocolEvent(
        type=event_type if isinstance(event_type, str) else "invalid",
        raw=value if isinstance(value, dict) else None,
    )
    evidence = sanitize_protocol_event(event, source=source, direction=direction)
    if event_type in _KNOWN_EVENT_TYPES:
        protocol_events.append(evidence)
    else:
        unknown_events.append(evidence)


def _stable_behavior_event_id(event: BehaviorEvent) -> UUID:
    identity = {
        "schema_version": event.schema_version,
        "event_type": event.event_type,
        "stream_id": event.stream_id,
        "session_id": str(event.session_id),
        "source": event.source,
        "producer_id": event.producer_id,
        "producer_seq": event.producer_seq,
        "channel": event.channel,
        "stimulus_revision": event.stimulus_revision,
        "input_turn_id": event.input_turn_id,
        "response_id": event.response_id,
        "media_session_id": event.media_session_id,
        "output_epoch": event.output_epoch,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return uuid5(_UUID_NAMESPACE, canonical)


def _behavior_event(
    *,
    event_type: str,
    session_id: UUID,
    payload: dict[str, Any],
    producer_seq: int,
    input_turn_id: str,
    media_session_id: str,
    stimulus_revision: int = 0,
    response_id: str | None = None,
    output_epoch: int | None = None,
) -> BehaviorEvent:
    return BehaviorEvent(
        event_id=uuid5(_UUID_NAMESPACE, event_type),
        schema_version=1,
        event_type=event_type,
        stream_id="voice-first-response",
        session_id=session_id,
        source="server",
        producer_id="hexis.voice-realtime-harness",
        producer_seq=producer_seq,
        channel="voice",
        payload=payload,
        ts=datetime.now(timezone.utc),
        stimulus_revision=stimulus_revision,
        input_turn_id=input_turn_id,
        response_id=response_id,
        media_session_id=media_session_id,
        output_epoch=output_epoch,
    )


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _resolve_db_dsn(config: Mapping[str, Any]) -> tuple[str, str]:
    """Resolve the explicit DB source without leaking the DSN value."""
    db_dsn_env = config.get("db_dsn_env")
    if db_dsn_env is not None:
        if not isinstance(db_dsn_env, str) or not _OPTIONAL_ENV_NAME.fullmatch(
            db_dsn_env
        ):
            raise HarnessStageError("config", "db_dsn_env 必须是显式环境变量名")
        dsn = os.environ.get(db_dsn_env)
        if not dsn:
            raise HarnessStageError("db", f"环境变量 {db_dsn_env} 未显式提供数据库 DSN")
        return dsn, db_dsn_env

    if "db_source" not in config:
        raise HarnessStageError(
            "config", "必须显式提供 db_source=hexis-env 或 db_dsn_env"
        )
    db_source = config["db_source"]
    if db_source != "hexis-env":
        raise HarnessStageError(
            "config", "db_source 仅支持 hexis-env 或显式 db_dsn_env"
        )
    return db_dsn_from_env(), "hexis-env"


def _emotion_runtime_config(config: Mapping[str, Any]) -> dict[str, Any]:
    emotion = config.get("emotion")
    if not isinstance(emotion, Mapping):
        raise HarnessStageError("config", "emotion 必须是 JSON object")

    required = ("provider", "model", "endpoint", "api_key_env", "timeout_ms")
    missing = [name for name in required if name not in emotion]
    if missing:
        raise HarnessStageError(
            "config", f"emotion 配置缺少必填字段: {', '.join(missing)}"
        )

    for name in ("provider", "model", "endpoint", "api_key_env"):
        value = emotion[name]
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise HarnessStageError("config", f"emotion.{name} 必须是非空字符串")
    if not _OPTIONAL_ENV_NAME.fullmatch(emotion["api_key_env"]):
        raise HarnessStageError("config", "emotion.api_key_env 必须是环境变量名")

    timeout_ms = emotion["timeout_ms"]
    if (
        isinstance(timeout_ms, bool)
        or not isinstance(timeout_ms, (int, float))
        or timeout_ms <= 0
    ):
        raise HarnessStageError("config", "emotion.timeout_ms 必须是正数")

    api_key = os.environ.get(emotion["api_key_env"])
    if api_key is None:
        raise HarnessStageError(
            "emotion_api_key",
            f"环境变量 {emotion['api_key_env']} 未显式提供 API Key",
        )

    return {
        "provider": emotion["provider"],
        "model": emotion["model"],
        "endpoint": emotion["endpoint"],
        "resolved_api_key": api_key,
        "timeout_seconds": timeout_ms / 1000,
        "host_managed_timeout": True,
    }


def _build_runtime(
    config: Mapping[str, Any],
    *,
    pool: Any,
    session_id: UUID,
    stable_instructions: str,
) -> tuple[VoiceEmotionCoordinator, dict[str, Any]]:
    emotion_config = _emotion_runtime_config(config)
    memory_limit = config.get("memory_limit", 10)
    if (
        isinstance(memory_limit, bool)
        or not isinstance(memory_limit, int)
        or memory_limit <= 0
    ):
        raise HarnessStageError("config", "memory_limit 必须是正整数")

    stable_instructions_hash = _sha256_hex(stable_instructions)

    async def context_reader(_identity: Mapping[str, Any]) -> Any:
        async with pool.acquire() as conn:
            from services.avatar_emotion import read_db_context

            return await read_db_context(conn)

    async def evaluator(message: str, context: Any) -> Any:
        return await evaluate_emotion(message, context, emotion_config)

    async def hydrator(identity: Mapping[str, Any], message: str) -> Any:
        return await hydrate_voice_context(
            pool,
            session_id=session_id,
            message=message,
            stable_instructions_hash=stable_instructions_hash,
            memory_limit=memory_limit,
        )

    coordinator = VoiceEmotionCoordinator(
        context_reader=context_reader,
        evaluator=evaluator,
        hydrator=hydrator,
        emotion_timeout_ms=config["emotion"]["timeout_ms"],
    )
    runtime_summary = {
        "db_source": config.get("db_dsn_env") or "hexis-env",
        "memory_limit": memory_limit,
        "emotion": {
            "provider": emotion_config["provider"],
            "model": emotion_config["model"],
            "endpoint": emotion_config["endpoint"],
            "api_key_env": config["emotion"]["api_key_env"],
            "timeout_ms": config["emotion"]["timeout_ms"],
        },
        "stable_instructions_hash": stable_instructions_hash,
    }
    return coordinator, runtime_summary


def _initial_evidence(config: Mapping[str, Any], source: str) -> dict[str, Any]:
    instructions = config.get("stable_instructions")
    return {
        "schema": SCHEMA,
        "source": source,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "cloud": {
            "endpoint": config.get("endpoint"),
            "model": config.get("model"),
            "voice": config.get("voice"),
            "session_id": None,
        },
        "config": {
            "api_key_env": config.get("api_key_env"),
            "wav": config.get("wav"),
            "session_id": config.get("session_id"),
            "input_turn_id": config.get("input_turn_id"),
            "timeout": config.get("timeout"),
            "stable_instructions": _text_summary(instructions),
            "output_epoch": config.get("output_epoch"),
            "playback_id": config.get("playback_id"),
        },
        "audio": None,
        "capture": None,
        "final": None,
        "authorization_receipt": None,
        "c_release": None,
        "hydration": None,
        "behavior_events": [],
        "protocol_events": [],
        "unknown_events": [],
        "response_created": False,
        "response_create_monotonic_ms": None,
        "response_stream": {
            "awaited_terminal": False,
            "created": None,
            "first_delta": None,
            "terminal": None,
        },
        "failure": None,
    }


def _validate_config(config: Mapping[str, Any], wav_path: str | Path) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        raise HarnessStageError("config", "配置必须是 JSON object")
    missing = [name for name in _REQUIRED_CONFIG if name not in config]
    if missing:
        raise HarnessStageError("config", f"配置缺少必填字段: {', '.join(missing)}")

    for name in ("endpoint", "model", "voice", "api_key_env", "stable_instructions"):
        value = config[name]
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise HarnessStageError("config", f"配置字段 {name} 必须是非空字符串")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config["api_key_env"]):
        raise HarnessStageError("config", "api_key_env 必须是显式环境变量名")
    if not config["endpoint"].startswith("wss://"):
        raise HarnessStageError("config", "endpoint 必须是 wss:// WebSocket 地址")

    timeout = config["timeout"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise HarnessStageError("config", "timeout 必须是正数秒")
    if timeout <= 0:
        raise HarnessStageError("config", "timeout 必须是正数秒")

    output_epoch = config["output_epoch"]
    if (
        not isinstance(output_epoch, int)
        or isinstance(output_epoch, bool)
        or output_epoch < 0
    ):
        raise HarnessStageError("config", "output_epoch 必须是非负整数")
    playback_id = config.get("playback_id")
    if playback_id is not None and (
        not isinstance(playback_id, str)
        or not playback_id.strip()
        or playback_id != playback_id.strip()
    ):
        raise HarnessStageError("config", "playback_id 必须是非空 trimmed 字符串")

    try:
        session_id = UUID(str(config["session_id"]))
        input_turn_id = UUID(str(config["input_turn_id"]))
    except (ValueError, TypeError) as exc:
        raise HarnessStageError(
            "config", "session_id/input_turn_id 必须是 UUID"
        ) from exc

    config_wav = Path(config["wav"]).expanduser()
    explicit_wav = Path(wav_path).expanduser()
    if config_wav.resolve() != explicit_wav.resolve():
        raise HarnessStageError("config", "显式 --wav 必须与配置中的 wav 路径一致")

    return {
        "endpoint": config["endpoint"],
        "model": config["model"],
        "voice": config["voice"],
        "api_key_env": config["api_key_env"],
        "wav": config_wav,
        "session_id": session_id,
        "input_turn_id": input_turn_id,
        "timeout": float(timeout),
        "stable_instructions": config["stable_instructions"],
        "output_epoch": output_epoch,
        "playback_id": playback_id,
    }


def _hydration_instructions(value: Any) -> str:
    instructions = getattr(value, "per_response_instructions", None)
    if isinstance(value, Mapping):
        instructions = value.get("per_response_instructions", instructions)
    if not isinstance(instructions, str) or not instructions.strip():
        raise HarnessStageError(
            "hydration", "水合结果缺少可用的 per_response_instructions"
        )
    return instructions


def _hydration_evidence(value: Any) -> dict[str, Any]:
    if value is None:
        return {"available": False}
    getter = value.get if isinstance(value, Mapping) else getattr
    evidence: dict[str, Any] = {"available": True}
    for name in ("hydrate_ms", "memory_count"):
        item = getter(value, name, None)
        if item is not None:
            evidence[name] = item
    instructions_hash = getter(value, "instructions_hash", None)
    if isinstance(instructions_hash, str):
        evidence["instructions_hash"] = instructions_hash
    instructions = getter(value, "per_response_instructions", None)
    if isinstance(instructions, str):
        evidence["instructions"] = _text_summary(instructions)
    return evidence


def _response_stream_event(event: ProtocolEvent, elapsed_ms: int) -> dict[str, Any]:
    value: dict[str, Any] = {
        "type": event.type,
        "response_id": event.response_id,
        "elapsed_ms": elapsed_ms,
    }
    if event.error_code is not None:
        value["error_code"] = event.error_code
    if event.error_message is not None:
        value["error_message"] = event.error_message
    return value


async def _default_connect_factory(endpoint: str, *, extra_headers: dict[str, str]):
    import websockets

    return await websockets.connect(endpoint, additional_headers=extra_headers)


async def run_harness(
    config: Mapping[str, Any],
    wav_path: str | Path,
    *,
    connect_factory: Callable[..., Awaitable[Any]] | None = None,
    coordinator: VoiceEmotionCoordinator,
    behavior_pool: Any,
    source: str = "real",
    playback_bridge: PlaybackBridge | None = None,
) -> HarnessRunResult:
    if source not in {"real", "simulated"}:
        raise ValueError("source must be real or simulated")

    evidence = _initial_evidence(config, source)
    client: QwenRealtimeClient | None = None
    try:
        value = _validate_config(config, wav_path)
        evidence["cloud"].update(
            {
                "endpoint": value["endpoint"],
                "model": value["model"],
                "voice": value["voice"],
            }
        )
        evidence["config"].update(
            {
                "api_key_env": value["api_key_env"],
                "wav": str(value["wav"]),
                "session_id": str(value["session_id"]),
                "input_turn_id": str(value["input_turn_id"]),
                "timeout": value["timeout"],
                "stable_instructions": _text_summary(value["stable_instructions"]),
                "output_epoch": value["output_epoch"],
                "playback_id": value["playback_id"],
            }
        )

        if playback_bridge is not None and not value["playback_id"]:
            raise HarnessStageError(
                "config", "playback_id 是 playback_bridge 的必填身份字段"
            )

        api_key = os.environ.get(value["api_key_env"])
        if api_key is None:
            raise HarnessStageError(
                "api_key", f"环境变量 {value['api_key_env']} 未显式提供 API Key"
            )

        audio = read_wav(value["wav"])
        evidence["audio"] = {
            "sha256": audio.facts.audio_sha256,
            "frame_count": audio.facts.frame_count,
            "duration_seconds": audio.facts.duration_seconds,
            "sample_rate": audio.facts.sample_rate,
            "channels": audio.facts.channels,
            "sample_width": audio.facts.sample_width,
        }

        selected_factory = connect_factory or _default_connect_factory

        async def evidence_factory(endpoint: str, *, extra_headers: dict[str, str]):
            websocket = await selected_factory(endpoint, extra_headers=extra_headers)
            return EvidenceWebSocket(
                websocket,
                protocol_events=evidence["protocol_events"],
                unknown_events=evidence["unknown_events"],
                source=source,
            )

        client = QwenRealtimeClient(
            endpoint=value["endpoint"],
            model=value["model"],
            voice=value["voice"],
            api_key=api_key,
            connect_factory=evidence_factory,
        )

        async with asyncio.timeout(value["timeout"]):
            cloud_session_id = await client.connect(value["stable_instructions"])
            evidence["cloud"]["session_id"] = cloud_session_id

            capture_instance_id = uuid5(
                _UUID_NAMESPACE,
                f"{value['session_id']}:{value['input_turn_id']}:{audio.facts.audio_sha256}",
            )
            capture = CaptureRange(
                session_id=value["session_id"],
                media_session_id=cloud_session_id,
                capture_instance_id=capture_instance_id,
                start_frame=0,
                end_frame=audio.facts.frame_count,
                start_monotonic_ms=0,
                end_monotonic_ms=int(audio.facts.duration_seconds * 1000),
                audio_sha256=audio.facts.audio_sha256,
            )
            evidence["capture"] = {
                "capture_instance_id": str(capture.capture_instance_id),
                "media_session_id": capture.media_session_id,
                "start_frame": capture.start_frame,
                "end_frame": capture.end_frame,
                "start_monotonic_ms": capture.start_monotonic_ms,
                "end_monotonic_ms": capture.end_monotonic_ms,
                "monotonic_reference": "wav_relative",
            }

            sink = ABehaviorEventSink(behavior_pool)
            identity = InputIdentity(
                session_id=value["session_id"],
                input_turn_id=value["input_turn_id"],
                input_order=0,
                stimulus_revision=0,
            )
            input_turn_id = str(value["input_turn_id"])

            await client.append_audio(base64.b64encode(audio.pcm_bytes).decode("ascii"))
            await client.commit_audio()
            await sink(
                _behavior_event(
                    event_type="voice_commit_sent",
                    session_id=value["session_id"],
                    payload={
                    "capture_instance_id": str(capture.capture_instance_id),
                },
                producer_seq=len(sink.event_types),
                input_turn_id=input_turn_id,
                media_session_id=capture.media_session_id,
            )
            )

            committed_event = await client.wait_for_input_committed()
            final_event = await client.wait_for_final(committed_event.item_id)
            if (
                final_event.type != "input_transcript_final"
                or final_event.is_final is not True
                or final_event.item_id != committed_event.item_id
                or not final_event.item_id
                or not isinstance(final_event.transcript, str)
                or not final_event.transcript.strip()
            ):
                raise HarnessStageError("final", "云端未返回可用 final 转录")

            final = FinalTranscript(
                identity=identity,
                capture_instance_id=capture.capture_instance_id,
                media_session_id=capture.media_session_id,
                item_id=final_event.item_id,
                transcript=final_event.transcript,
                transcript_version=1,
                is_final=True,
            )
            evidence["final"] = {
                "item_id": final.item_id,
                "transcript": final.transcript,
                "transcript_version": final.transcript_version,
                "is_final": final.is_final,
            }
            await sink(
                _behavior_event(
                    event_type="voice_transcript_final",
                    session_id=value["session_id"],
                    payload={
                        "input_turn_id": input_turn_id,
                        "transcript": final.transcript,
                        "transcript_version": final.transcript_version,
                        "is_final": final.is_final,
                    },
                    producer_seq=len(sink.event_types),
                    input_turn_id=input_turn_id,
                    media_session_id=capture.media_session_id,
                )
            )

            receipt = await InputAuthorizationService().commit(final, capture)
            evidence["authorization_receipt"] = {
                "event_id": str(receipt.event_id),
                "identity": receipt.identity.model_dump(mode="json"),
                "range_key": receipt.range_key,
                "deduplicated": receipt.deduplicated,
            }
            await sink(
                _behavior_event(
                    event_type="voice_input_committed",
                    session_id=value["session_id"],
                    payload={
                        "capture_instance_id": str(capture.capture_instance_id),
                    "deduplicated": receipt.deduplicated,
                },
                producer_seq=len(sink.event_types),
                input_turn_id=input_turn_id,
                media_session_id=capture.media_session_id,
            )
            )

            gate = FirstResponseGate()
            gate.mark_committed()
            emotion_adapter = CEmotionReleaseAdapter(coordinator)
            emotion_adapter.start(
                {
                    "session_id": str(value["session_id"]),
                    "input_turn_id": input_turn_id,
                    "input_order": 0,
                    "stimulus_revision": 0,
                },
                final.transcript,
            )
            release_task = asyncio.create_task(emotion_adapter.release())
            hydration_task = asyncio.create_task(emotion_adapter.hydration())
            release, hydration = await asyncio.gather(release_task, hydration_task)

            if not release.released:
                raise HarnessStageError("c_release", "C 节点未放行")
            gate.mark_emotion_released()
            if not release.context_ready:
                raise HarnessStageError("c_release", "C 上下文尚未可用")
            instructions = _hydration_instructions(hydration)
            gate.mark_context_ready()
            await gate.wait_until_ready()

            evidence["c_release"] = {
                "released": release.released,
                "context_ready": release.context_ready,
                "release_reason": emotion_adapter.last_result.metrics.get(
                    "release_reason"
                ),
                "evaluation_status": emotion_adapter.last_result.evaluation.get(
                    "status"
                ),
                "visual_window": emotion_adapter.last_result.visual_window,
            }
            evidence["hydration"] = _hydration_evidence(hydration)
            hydration_evidence = evidence["hydration"]
            await sink(
                _behavior_event(
                    event_type="voice_hydrate_done",
                    session_id=value["session_id"],
                    payload={
                        "hydrate_ms": hydration_evidence.get("hydrate_ms"),
                        "instructions_hash": hydration_evidence.get(
                            "instructions_hash"
                        ),
                    },
                    producer_seq=len(sink.event_types),
                    input_turn_id=input_turn_id,
                    media_session_id=capture.media_session_id,
                )
            )

            response_id = f"resp-{uuid5(_UUID_NAMESPACE, input_turn_id)}"
            started = time.monotonic_ns() // 1_000_000
            await client.update_session_instructions(instructions)
            await client.create_first_response(response_id)
            evidence["response_created"] = True
            evidence["response_create_monotonic_ms"] = (
                time.monotonic_ns() // 1_000_000 - started
            )
            await sink(
                _behavior_event(
                    event_type="voice_response_created",
                    session_id=value["session_id"],
                    payload={
                        "response_id": response_id,
                        "output_epoch": value["output_epoch"],
                    },
                    producer_seq=len(sink.event_types),
                    input_turn_id=input_turn_id,
                    media_session_id=capture.media_session_id,
                    response_id=response_id,
                )
            )
            stream_started = time.monotonic_ns() // 1_000_000
            output_adapter = QwenRealtimeOutputAdapter(
                response_id=response_id,
                output_epoch=value["output_epoch"],
                media_session_id=capture.media_session_id,
            )
            first_audio_delta_recorded = False
            playback_started_recorded = False
            first_audible_recorded = False
            playback_bridge_started = False
            async for event in client.events():
                elapsed_ms = time.monotonic_ns() // 1_000_000 - stream_started
                if event.type == "response.created":
                    output_adapter.bind(event)
                    if evidence["response_stream"]["created"] is None:
                        evidence["response_stream"]["created"] = (
                            _response_stream_event(event, elapsed_ms)
                    )
                    continue
                if event.type in {"response.text.delta", "response.audio.delta"}:
                    if evidence["response_stream"]["first_delta"] is None:
                        evidence["response_stream"]["first_delta"] = (
                            _response_stream_event(event, elapsed_ms)
                        )
                    if event.type == "response.audio.delta":
                        chunk = output_adapter.accept(event)
                        if chunk is None:
                            continue
                        if not first_audio_delta_recorded:
                            first_audio_delta_recorded = True
                            await sink(
                                _behavior_event(
                                    event_type="voice_first_audio_delta",
                                    session_id=value["session_id"],
                                    payload={
                                        "response_id": response_id,
                                        "ttfa_ms": elapsed_ms,
                                        "segment_id": chunk.segment_id,
                                        "chunk_seq": chunk.chunk_seq,
                                    },
                                    producer_seq=len(sink.event_types),
                                    input_turn_id=input_turn_id,
                                    media_session_id=capture.media_session_id,
                                    response_id=response_id,
                                    output_epoch=value["output_epoch"],
                                )
                            )
                        if playback_bridge is not None:
                            playback_bridge_started = True
                            facts = await playback_bridge.start(chunk)
                            if facts.started and not playback_started_recorded:
                                playback_started_recorded = True
                                await sink(
                                    _behavior_event(
                                        event_type="voice_playback_started",
                                        session_id=value["session_id"],
                                        payload={
                                            "playback_id": value["playback_id"],
                                            "response_id": response_id,
                                            "start_ms": facts.start_ms or 0,
                                        },
                                        producer_seq=len(sink.event_types),
                                        input_turn_id=input_turn_id,
                                        media_session_id=capture.media_session_id,
                                        response_id=response_id,
                                        output_epoch=value["output_epoch"],
                                    )
                                )
                            if (
                                facts.audible_ms is not None
                                and not first_audible_recorded
                            ):
                                first_audible_recorded = True
                                await sink(
                                    _behavior_event(
                                        event_type="voice_first_audible",
                                        session_id=value["session_id"],
                                        payload={
                                            "playback_id": value["playback_id"],
                                            "audible_ms": facts.audible_ms,
                                        },
                                        producer_seq=len(sink.event_types),
                                        input_turn_id=input_turn_id,
                                        media_session_id=capture.media_session_id,
                                        response_id=response_id,
                                        output_epoch=value["output_epoch"],
                                    )
                                )
                    continue
                if event.type in {
                    "response.done",
                    "response.failed",
                    "response_failed",
                }:
                    output_adapter.finish(event)
                    evidence["response_stream"]["terminal"] = _response_stream_event(
                        event, elapsed_ms
                    )
                    evidence["response_stream"]["awaited_terminal"] = True
                    if event.type == "response.done":
                        await sink(
                            _behavior_event(
                                event_type="voice_response_done",
                                session_id=value["session_id"],
                                payload={
                                    "response_id": response_id,
                                    "done_ms": elapsed_ms,
                                },
                                producer_seq=len(sink.event_types),
                                input_turn_id=input_turn_id,
                                media_session_id=capture.media_session_id,
                                response_id=response_id,
                                output_epoch=value["output_epoch"],
                            )
                        )
                    else:
                        await sink(
                            _behavior_event(
                                event_type="voice_response_failed",
                                session_id=value["session_id"],
                                payload={
                                    "response_id": response_id,
                                    "error_code": event.error_code,
                                    "error_message": event.error_message,
                                },
                                producer_seq=len(sink.event_types),
                                input_turn_id=input_turn_id,
                                media_session_id=capture.media_session_id,
                                response_id=response_id,
                                output_epoch=value["output_epoch"],
                            )
                        )
                    if playback_bridge is not None and playback_bridge_started:
                        facts = await playback_bridge.finish()
                        if facts.terminal == "ended":
                            await sink(
                                _behavior_event(
                                    event_type="voice_playback_ended",
                                    session_id=value["session_id"],
                                    payload={
                                        "playback_id": value["playback_id"],
                                        "scope": facts.scope or "unknown",
                                        "played_ms": facts.played_ms,
                                    },
                                    producer_seq=len(sink.event_types),
                                    input_turn_id=input_turn_id,
                                    media_session_id=capture.media_session_id,
                                    response_id=response_id,
                                    output_epoch=value["output_epoch"],
                                )
                            )
                        elif facts.terminal == "interrupted":
                            await sink(
                                _behavior_event(
                                    event_type="voice_playback_interrupted",
                                    session_id=value["session_id"],
                                    payload={
                                        "playback_id": value["playback_id"],
                                        "reason": facts.reason or "error",
                                        "played_ms": facts.played_ms,
                                    },
                                    producer_seq=len(sink.event_types),
                                    input_turn_id=input_turn_id,
                                    media_session_id=capture.media_session_id,
                                    response_id=response_id,
                                    output_epoch=value["output_epoch"],
                                )
                            )
                    if event.type != "response.done":
                        message = event.error_message or "云端 response 未成功完成"
                        raise HarnessStageError("response_stream", message)
                    break
            evidence["behavior_events"] = [
                {"event_type": event_type, **receipt}
                for event_type, receipt in zip(
                    sink.event_types, sink.receipts, strict=True
                )
            ]
        return HarnessRunResult(ok=True, evidence=evidence)
    except HarnessStageError as exc:
        evidence["failure"] = _failure(exc.stage, exc)
        return HarnessRunResult(ok=False, evidence=evidence)
    except asyncio.TimeoutError as exc:
        evidence["failure"] = _failure("timeout", exc)
        return HarnessRunResult(ok=False, evidence=evidence)
    except QwenRealtimeConnectionError as exc:
        evidence["failure"] = _failure("connection", exc)
        return HarnessRunResult(ok=False, evidence=evidence)
    except OutputMappingError as exc:
        evidence["failure"] = _failure("output_mapping", exc)
        return HarnessRunResult(ok=False, evidence=evidence)
    except Exception as exc:
        stage = getattr(exc, "stage", "turn")
        evidence["failure"] = _failure(stage, exc)
        return HarnessRunResult(ok=False, evidence=evidence)
    finally:
        if client is not None:
            await client.close()


async def _run_real_harness(
    config: Mapping[str, Any],
    wav_path: str | Path,
    playback_bridge: PlaybackBridge | None = None,
) -> HarnessRunResult:
    """Build the real A/C runtime, then run one controlled voice turn."""
    import asyncpg

    value = _validate_config(config, wav_path)
    dsn, _db_source = _resolve_db_dsn(config)
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        coordinator, runtime_summary = _build_runtime(
            config,
            pool=pool,
            session_id=value["session_id"],
            stable_instructions=value["stable_instructions"],
        )
        result = await run_harness(
            config,
            wav_path,
            coordinator=coordinator,
            behavior_pool=pool,
            source="real",
            playback_bridge=playback_bridge,
        )
        result.evidence["runtime"] = runtime_summary
        return result
    finally:
        await pool.close()


def _failure(stage: str, exc: BaseException) -> dict[str, str]:
    return {
        "stage": stage,
        "error_type": type(exc).__name__,
        "message": str(exc) or type(exc).__name__,
    }


def write_evidence(path: str | Path, evidence: Mapping[str, Any]) -> None:
    output = Path(path)
    if output.exists():
        raise RuntimeError(f"证据输出文件已存在，拒绝覆盖: {output}")
    with output.open("x", encoding="utf-8") as handle:
        json.dump(dict(evidence), handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _load_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("配置必须是 JSON object")
    return value


def main(
    argv: list[str] | None = None,
    *,
    runner: Callable[..., Awaitable[HarnessRunResult]] = _run_real_harness,
) -> int:
    args = parse_args(argv)
    try:
        config = _load_config(args.config)
        result = asyncio.run(runner(config, args.wav))
        write_evidence(args.output, result.evidence)
    except Exception as exc:
        print(f"harness 失败: {type(exc).__name__}: {exc}", flush=True)
        return 1

    print(
        f"harness {'成功' if result.ok else '失败'}；证据已写入 {args.output}",
        flush=True,
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
