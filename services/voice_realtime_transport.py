"""Explicit local JSON contracts for the browser realtime media path."""

from __future__ import annotations

import base64
import binascii
import hashlib
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


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


ClientMessage = Annotated[
    Union[
        CaptureOpen,
        CaptureSuspend,
        CaptureResume,
        ClientAudioChunk,
        UtteranceEnd,
        ClientControl,
    ],
    Field(discriminator="type"),
]
_client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


class ServerEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    type: str
    session_id: UUID
    media_session_id: str = Field(min_length=1, max_length=200)
    producer_seq: int = Field(default=0, ge=0, strict=True)


class ServerReady(ServerEnvelope):
    type: Literal["ready"]
    cloud_session_id: str = Field(min_length=1, max_length=200)
    instructions_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ServerAudioChunk(ServerEnvelope):
    type: Literal["audio_chunk"]
    response_id: str = Field(min_length=1, max_length=200)
    output_epoch: int = Field(ge=0, strict=True)
    segment_id: str = Field(min_length=1, max_length=200)
    segment_index: int = Field(ge=0, strict=True)
    chunk_seq: int = Field(gt=0, strict=True)
    pcm_base64: str = Field(min_length=1, max_length=1_000_000)


class ServerGeneration(ServerEnvelope):
    type: Literal["generation"]
    response_id: str = Field(min_length=1, max_length=200)
    output_epoch: int = Field(ge=0, strict=True)
    kind: Literal["text_delta", "audio_transcript_delta", "done", "failed"]
    text: str | None = Field(default=None, max_length=100_000)
    reason: str | None = Field(default=None, max_length=2_000)


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
    Union[ServerReady, ServerAudioChunk, ServerGeneration, ServerEvent, ServerError],
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
    ) -> None:
        self.session_id = session_id
        self.media_session_id = media_session_id
        self.max_audio_bytes = max_audio_bytes
        self._last_producer_seq = -1
        self._captures: dict[UUID, dict[str, Any]] = {}

    def accept(self, message: ClientMessage) -> dict[str, Any]:
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
        raise ClientTransportError(f"unsupported client message: {type(message).__name__}")

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
