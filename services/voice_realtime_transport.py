"""Explicit local JSON contracts for the browser realtime media path."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)


class ClientTransportError(ValueError):
    """A syntactically valid client message violates transport state."""


class ClientEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    type: str
    session_id: UUID
    media_session_id: str = Field(min_length=1, max_length=200)
    producer_seq: int = Field(default=0, ge=0, strict=True)


class CaptureOpen(ClientEnvelope):
    type: Literal["capture_open"]
    capture_instance_id: UUID
    sample_rate: Literal[16000]
    channels: Literal[1]
    sample_format: Literal["s16le"]


class CaptureSuspend(ClientEnvelope):
    type: Literal["capture_suspend"]
    capture_instance_id: UUID
    control_id: UUID


class CaptureResume(ClientEnvelope):
    type: Literal["capture_resume"]
    capture_instance_id: UUID
    control_id: UUID


class ClientAudioChunk(ClientEnvelope):
    type: Literal["audio_chunk"]
    capture_instance_id: UUID
    start_frame: int = Field(ge=0, strict=True)
    end_frame: int = Field(ge=0, strict=True)
    start_monotonic_ms: int = Field(ge=0, strict=True)
    end_monotonic_ms: int = Field(ge=0, strict=True)
    pcm_base64: str = Field(min_length=1, max_length=1_000_000)

    @field_validator("pcm_base64")
    @classmethod
    def _validate_base64(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("pcm_base64 must be valid base64") from exc
        if not decoded or len(decoded) % 2:
            raise ValueError("pcm_base64 must contain 16-bit mono frames")
        return value


class UtteranceEnd(ClientEnvelope):
    type: Literal["utterance_end"]
    capture_instance_id: UUID
    start_frame: int = Field(ge=0, strict=True)
    end_frame: int = Field(ge=0, strict=True)
    start_monotonic_ms: int = Field(ge=0, strict=True)
    end_monotonic_ms: int = Field(ge=0, strict=True)
    audio_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ClientControl(ClientEnvelope):
    type: Literal["control"]
    action: Literal["close", "barge_in"]
    reason: str = Field(min_length=1, max_length=200)


class PlaybackEventRequest(BaseModel):
    """One exact HTTP playback receipt, also carried inside confirmed barge-in."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["open", "audible", "progress", "end", "interrupt"]
    session_id: UUID
    response_id: str = Field(min_length=1, max_length=200)
    playback_id: str = Field(min_length=1, max_length=200)
    producer_seq: int = Field(ge=0, strict=True)
    output_epoch: int = Field(ge=0)
    media_session_id: str = Field(min_length=1, max_length=200)
    turn_id: UUID | None = None
    start_ms: int | None = Field(default=None, ge=0)
    audible_ms: int | None = Field(default=None, ge=0)
    played_ms: int | None = Field(default=None, ge=0)
    interval_ms: int | None = Field(default=None, ge=0)
    phase: str | None = Field(default=None, min_length=1, max_length=40)
    scope: Literal["complete", "partial", "unplayed", "unknown"] | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def _validate_action_fields(self) -> "PlaybackEventRequest":
        required = {
            "open": ("start_ms",),
            "audible": ("audible_ms",),
            "progress": ("played_ms", "interval_ms", "phase"),
            "end": ("scope", "played_ms"),
            "interrupt": ("played_ms", "reason"),
        }[self.action]
        if any(getattr(self, name) is None for name in required):
            raise ValueError(f"playback {self.action} receipt is missing a required field")
        return self


class ClientBargeInConfirmed(ClientEnvelope):
    type: Literal["barge_in_confirmed"]
    command_id: UUID
    capture_instance_id: UUID
    response_id: str = Field(min_length=1, max_length=200)
    output_epoch: int = Field(ge=0, strict=True)
    playback_id: str = Field(min_length=1, max_length=200)
    local_playback_state: Literal["started", "not_started"]
    receipt_prefix: list[PlaybackEventRequest]

    @model_validator(mode="after")
    def _validate_receipt_prefix_identity(self) -> "ClientBargeInConfirmed":
        if self.local_playback_state == "not_started" and self.receipt_prefix:
            raise ValueError("not_started barge-in must not include playback receipts")
        previous_seq: int | None = None
        for receipt in self.receipt_prefix:
            if (
                receipt.session_id != self.session_id
                or receipt.media_session_id != self.media_session_id
                or receipt.response_id != self.response_id
                or receipt.playback_id != self.playback_id
                or receipt.output_epoch != self.output_epoch
            ):
                raise ValueError("playback receipt prefix identity does not match the confirmed target")
            if previous_seq is not None and receipt.producer_seq <= previous_seq:
                raise ValueError("playback receipt prefix sequence must increase")
            previous_seq = receipt.producer_seq
        return self


ClientMessage = Annotated[
    Union[
        CaptureOpen,
        CaptureSuspend,
        CaptureResume,
        ClientAudioChunk,
        UtteranceEnd,
        ClientControl,
        ClientBargeInConfirmed,
    ],
    Field(discriminator="type"),
]
_client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)

C7_DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config/voice-barge-in/playback-coordination.json"


class PlaybackCoordinationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    configuration_id: str = Field(min_length=1, max_length=200)
    validation_status: Literal["unvalidated", "validated"]
    maximum_receipt_prefix_items: int = Field(gt=0, strict=True)
    recent_response_limit: int = Field(gt=0, strict=True)
    recent_response_retention_ms: int = Field(gt=0, strict=True)
    settlement_timeout_ms: int = Field(gt=0, strict=True)
    timeout_classification: Literal["engineering_failure_boundary_not_experience_acceptance"]


class BargeInCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    supported: bool = Field(strict=True)
    configuration_id: str = Field(min_length=1, max_length=200)
    maximum_receipt_prefix_items: int = Field(gt=0, strict=True)


class RealtimeSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: UUID
    media_session_id: str = Field(min_length=1, max_length=200)
    websocket_url: str = Field(min_length=1, max_length=2_000)
    barge_in_capability: BargeInCapability


def load_playback_coordination_config(
    path: Path | str | None = None,
) -> PlaybackCoordinationConfig:
    selected_path = C7_DEFAULT_PATH if path is None else Path(path)
    try:
        value = json.loads(selected_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Playback coordination config is missing: {selected_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Playback coordination config cannot be read: {selected_path}") from exc
    return PlaybackCoordinationConfig.model_validate(value)


def build_barge_in_capability(
    config: PlaybackCoordinationConfig,
    *,
    supported: bool,
) -> BargeInCapability:
    return BargeInCapability(
        supported=supported,
        configuration_id=config.configuration_id,
        maximum_receipt_prefix_items=config.maximum_receipt_prefix_items,
    )


class ServerEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    type: str
    session_id: UUID
    media_session_id: str = Field(min_length=1, max_length=200)
    producer_seq: int = Field(default=0, ge=0, strict=True)


class ServerReady(ServerEnvelope):
    type: Literal["ready"]
    producer_seq: Literal[0] = 0
    cloud_session_id: str = Field(min_length=1, max_length=200)
    instructions_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    barge_in_capability: BargeInCapability


class ServerAudioRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_frame: int = Field(ge=0, strict=True)
    end_frame: int = Field(ge=0, strict=True)


class ServerAudioChunk(ServerEnvelope):
    type: Literal["audio_chunk"]
    response_id: str = Field(min_length=1, max_length=200)
    playback_id: str = Field(min_length=1, max_length=200)
    turn_id: UUID | None
    output_epoch: int = Field(ge=0, strict=True)
    segment_id: str = Field(min_length=1, max_length=200)
    segment_index: int = Field(ge=0, strict=True)
    chunk_seq: int = Field(gt=0, strict=True)
    pcm_base64: str = Field(min_length=1, max_length=1_000_000)


class ServerGeneration(ServerEnvelope):
    type: Literal["generation"]
    response_id: str = Field(min_length=1, max_length=200)
    playback_id: str = Field(min_length=1, max_length=200)
    turn_id: UUID | None
    output_epoch: int = Field(ge=0, strict=True)
    kind: Literal["text_delta", "audio_transcript_delta", "done", "failed"]
    text: str | None = Field(default=None, max_length=100_000)
    reason: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def _validate_generation_variant(self) -> "ServerGeneration":
        fields = self.model_fields_set
        if self.kind in {"text_delta", "audio_transcript_delta"}:
            if "text" not in fields or "reason" in fields:
                raise ValueError("generation text variants require text and forbid reason")
        elif "reason" not in fields or "text" in fields:
            raise ValueError("generation terminal variants require reason and forbid text")
        return self


class ServerMemoryPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: UUID
    type: str = Field(min_length=1, max_length=40)
    content: str = Field(min_length=1, max_length=260)
    similarity: float | None = None
    relevance_score: float | None = None
    importance: float | None = None
    trust_level: float | None = None
    confidence: float | None = None
    source: str | None = Field(default=None, min_length=1, max_length=100)


class ServerMemoryRecall(ServerEnvelope):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    type: Literal["memory_recall"]
    input_turn_id: str = Field(min_length=1, max_length=200)
    stimulus_revision: int = Field(ge=0, strict=True)
    count: int = Field(ge=0, le=10, strict=True)
    memories: list[ServerMemoryPreview] = Field(max_length=10)
    hydrate_ms: float = Field(ge=0)
    recall_performed: bool = Field(strict=True)

    @model_validator(mode="after")
    def _validate_memory_recall(self) -> "ServerMemoryRecall":
        if self.count != len(self.memories):
            raise ValueError("memory recall count must match previews")
        if not self.recall_performed and self.count != 0:
            raise ValueError("memory recall disabled state must not include previews")
        return self


class ServerM5Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection_id: str = Field(min_length=1, max_length=200)
    version: int = Field(gt=0, strict=True)
    message: str = Field(min_length=1, max_length=100_000)
    state: Literal["open"]
    origin_session_id: UUID


class ServerM5SelectionOpened(ServerEnvelope):
    type: Literal["m5_selection_opened"]
    selection: ServerM5Selection


class ServerCaptureSuspended(ServerEnvelope):
    type: Literal["capture_suspended"]
    capture_instance_id: UUID
    control_id: UUID
    discarded_range: ServerAudioRange | None


class ServerCaptureResumed(ServerEnvelope):
    type: Literal["capture_resumed"]
    capture_instance_id: UUID
    control_id: UUID


class ServerBargeInResult(ServerEnvelope):
    type: Literal["barge_in_result"]
    command_id: UUID
    capture_instance_id: UUID
    response_id: str = Field(min_length=1, max_length=200)
    output_epoch: int = Field(ge=0, strict=True)
    playback_id: str = Field(min_length=1, max_length=200)
    status: Literal["accepted", "playback_only", "stale_target", "unknown"]


class ServerEvent(ServerEnvelope):
    type: Literal["event"]
    event: dict[str, Any]


class ServerError(ServerEnvelope):
    type: Literal["error"]
    stage: str = Field(min_length=1, max_length=100)
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=2_000)
    recoverable: bool


ServerMessage = Annotated[
    Union[
        ServerReady,
        ServerAudioChunk,
        ServerGeneration,
        ServerMemoryRecall,
        ServerM5SelectionOpened,
        ServerCaptureSuspended,
        ServerCaptureResumed,
        ServerBargeInResult,
        ServerEvent,
        ServerError,
    ],
    Field(discriminator="type"),
]
_server_adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)


def parse_client_message(value: Any) -> ClientMessage:
    return _client_adapter.validate_python(value)


def parse_server_message(value: Any) -> ServerMessage:
    return _server_adapter.validate_python(value)


class VoiceClientTransport:
    """Validate one producer's identity, sequence, ranges and PCM facts."""

    def __init__(
        self,
        *,
        session_id: UUID,
        media_session_id: str,
        max_audio_bytes: int = 960_000,
        max_receipt_prefix_items: int | None = None,
    ) -> None:
        if max_receipt_prefix_items is not None and (
            isinstance(max_receipt_prefix_items, bool)
            or not isinstance(max_receipt_prefix_items, int)
            or max_receipt_prefix_items <= 0
        ):
            raise ValueError("max_receipt_prefix_items must be a positive integer")
        self.session_id = session_id
        self.media_session_id = media_session_id
        self.max_audio_bytes = max_audio_bytes
        self.max_receipt_prefix_items = max_receipt_prefix_items
        self._last_producer_seq = -1
        self._captures: dict[UUID, dict[str, Any]] = {}

    def accept(
        self, message: ClientMessage, *, raw_message: Any | None = None
    ) -> dict[str, Any]:
        if (
            message.session_id != self.session_id
            or message.media_session_id != self.media_session_id
        ):
            raise ClientTransportError(
                "client message identity does not match the media session"
            )
        if message.producer_seq <= self._last_producer_seq:
            raise ClientTransportError(
                f"producer_seq must increase: last={self._last_producer_seq} "
                f"actual={message.producer_seq}"
            )
        self._last_producer_seq = message.producer_seq

        if isinstance(message, CaptureOpen):
            return self._accept_capture_open(message)
        if isinstance(message, CaptureSuspend):
            return self._accept_capture_suspend(message)
        if isinstance(message, CaptureResume):
            return self._accept_capture_resume(message)
        if isinstance(message, ClientAudioChunk):
            return self._accept_audio_chunk(message)
        if isinstance(message, UtteranceEnd):
            return self._accept_utterance_end(message)
        if isinstance(message, ClientControl):
            return {"accepted": True, "action": message.action}
        if isinstance(message, ClientBargeInConfirmed):
            return self._accept_barge_in_confirmed(message, raw_message=raw_message)
        raise ClientTransportError(f"unsupported client message: {type(message).__name__}")

    def _accept_barge_in_confirmed(
        self,
        message: ClientBargeInConfirmed,
        *,
        raw_message: Any | None = None,
    ) -> dict[str, Any]:
        if self.max_receipt_prefix_items is None:
            raise ClientTransportError("C7 playback coordination config is unavailable")
        if len(message.receipt_prefix) > self.max_receipt_prefix_items:
            raise ClientTransportError("playback receipt prefix exceeds the configured C7 limit")
        raw_prefix = (
            raw_message.get("receipt_prefix")
            if isinstance(raw_message, dict)
            else None
        )
        receipt_prefix = (
            copy.deepcopy(raw_prefix)
            if isinstance(raw_prefix, list)
            else [
                receipt.model_dump(mode="json", exclude_unset=True)
                for receipt in message.receipt_prefix
            ]
        )
        return {
            "accepted": True,
            "action": "barge_in_confirmed",
            "command_id": str(message.command_id),
            "capture_instance_id": str(message.capture_instance_id),
            "response_id": message.response_id,
            "output_epoch": message.output_epoch,
            "playback_id": message.playback_id,
            "local_playback_state": message.local_playback_state,
            "receipt_prefix": receipt_prefix,
        }

    def validate_barge_in_capture(self, capture_instance_id: UUID) -> None:
        capture = self._require_capture(capture_instance_id)
        if capture["suspended"]:
            raise ClientTransportError("confirmed barge-in requires an open, unsuspended capture")

    def _accept_capture_open(self, message: CaptureOpen) -> dict[str, Any]:
        facts = {
            "sample_rate": message.sample_rate,
            "channels": message.channels,
            "sample_format": message.sample_format,
        }
        existing = self._captures.get(message.capture_instance_id)
        if existing is not None:
            if existing["format"] != facts:
                raise ClientTransportError("capture_open conflicts with earlier facts")
            return {"accepted": True, "deduplicated": True}
        self._captures[message.capture_instance_id] = {
            "format": facts,
            "next_frame": 0,
            "range": None,
            "suspended": False,
            "sha256": hashlib.sha256(),
        }
        return {"accepted": True, "deduplicated": False}

    def _accept_capture_suspend(self, message: CaptureSuspend) -> dict[str, Any]:
        capture = self._require_capture(message.capture_instance_id)
        if capture["suspended"]:
            return {"accepted": True, "state": "suspended", "discarded_range": None}
        current = capture["range"]
        discarded = (
            {"start_frame": current["start_frame"], "end_frame": current["end_frame"]}
            if current is not None
            else None
        )
        capture["range"] = None
        capture["sha256"] = hashlib.sha256()
        capture["suspended"] = True
        return {"accepted": True, "state": "suspended", "discarded_range": discarded}

    def _accept_capture_resume(self, message: CaptureResume) -> dict[str, Any]:
        capture = self._require_capture(message.capture_instance_id)
        capture["suspended"] = False
        capture["range"] = None
        capture["sha256"] = hashlib.sha256()
        return {"accepted": True, "state": "capturing"}

    def _accept_audio_chunk(self, message: ClientAudioChunk) -> dict[str, Any]:
        capture = self._require_capture(message.capture_instance_id)
        if capture["suspended"]:
            raise ClientTransportError("audio cannot be accepted while capture is suspended")
        if message.end_frame < message.start_frame:
            raise ClientTransportError("audio range end precedes start")
        if message.end_monotonic_ms < message.start_monotonic_ms:
            raise ClientTransportError("audio monotonic range end precedes start")
        try:
            pcm = base64.b64decode(message.pcm_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ClientTransportError("pcm_base64 is invalid") from exc
        expected_bytes = (message.end_frame - message.start_frame) * 2
        if len(pcm) != expected_bytes:
            raise ClientTransportError(
                f"PCM length mismatch: expected={expected_bytes} actual={len(pcm)}"
            )
        if len(pcm) > self.max_audio_bytes:
            raise ClientTransportError(
                f"PCM chunk exceeds limit: limit={self.max_audio_bytes} actual={len(pcm)}"
            )
        current = capture["range"]
        if current is None:
            expected_start = capture["next_frame"]
            if message.start_frame < expected_start:
                raise ClientTransportError(
                    f"audio range overlaps an earlier utterance: "
                    f"minimum_start={expected_start} actual_start={message.start_frame}"
                )
            capture["range"] = {
                "start_frame": message.start_frame,
                "end_frame": message.end_frame,
                "start_monotonic_ms": message.start_monotonic_ms,
                "end_monotonic_ms": message.end_monotonic_ms,
            }
            capture["sha256"] = hashlib.sha256()
        else:
            if message.start_frame != current["end_frame"]:
                raise ClientTransportError(
                    f"audio range conflict: expected_start={current['end_frame']} "
                    f"actual_start={message.start_frame}"
                )
            if message.start_monotonic_ms < current["end_monotonic_ms"]:
                raise ClientTransportError(
                    "audio monotonic range overlaps the earlier chunk"
                )
            current["end_frame"] = message.end_frame
            current["end_monotonic_ms"] = message.end_monotonic_ms
        capture["sha256"].update(pcm)
        capture["next_frame"] = message.end_frame
        return {
            "accepted": True,
            "start_frame": message.start_frame,
            "end_frame": message.end_frame,
            "pcm_bytes": len(pcm),
        }

    def _accept_utterance_end(self, message: UtteranceEnd) -> dict[str, Any]:
        capture = self._require_capture(message.capture_instance_id)
        if capture["suspended"]:
            raise ClientTransportError("utterance cannot end while capture is suspended")
        current = capture["range"]
        if current is None:
            raise ClientTransportError("utterance_end has no preceding audio range")
        if (
            message.start_frame != current["start_frame"]
            or message.end_frame != current["end_frame"]
            or message.start_monotonic_ms != current["start_monotonic_ms"]
            or message.end_monotonic_ms != current["end_monotonic_ms"]
        ):
            raise ClientTransportError("utterance_end range does not match received audio")
        digest = capture["sha256"].hexdigest()
        if message.audio_sha256 != digest:
            raise ClientTransportError("utterance_end audio hash does not match received PCM")
        capture["range"] = None
        capture["sha256"] = hashlib.sha256()
        return {
            "accepted": True,
            "start_frame": message.start_frame,
            "end_frame": message.end_frame,
            "audio_sha256": digest,
        }

    def _require_capture(self, capture_instance_id: UUID) -> dict[str, Any]:
        capture = self._captures.get(capture_instance_id)
        if capture is None:
            raise ClientTransportError("capture_instance_id was not opened")
        return capture
