"""Host-side authorization for one committed realtime voice input."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class InputIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: UUID
    input_turn_id: UUID
    input_order: int = Field(ge=0)
    stimulus_revision: int = Field(ge=0)


class CaptureRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: UUID
    media_session_id: str = Field(min_length=1)
    capture_instance_id: UUID
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    start_monotonic_ms: int = Field(ge=0)
    end_monotonic_ms: int = Field(ge=0)
    audio_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FinalTranscript(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: InputIdentity
    capture_instance_id: UUID
    media_session_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    transcript: str
    transcript_version: int = Field(gt=0)
    is_final: bool


class AuthorizationConflict(RuntimeError):
    """The same acoustic range was retried with conflicting producer facts."""


@dataclass(frozen=True)
class InputCommitReceipt:
    event_id: UUID
    identity: InputIdentity
    transcript: str
    range_key: str
    deduplicated: bool

    def payload(self) -> dict[str, Any]:
        return {
            "type": "input_committed",
            "identity": self.identity.model_dump(mode="json"),
            "capture_instance_id": self.range_key.split(":", 1)[0],
            "transcript": self.transcript,
            "deduplicated": self.deduplicated,
        }


def _range_key(source: CaptureRange) -> str:
    return f"{source.capture_instance_id}:{source.start_frame}:{source.end_frame}:{source.audio_sha256}"


def _fingerprint(source: CaptureRange, value: FinalTranscript) -> str:
    body = {
        "range": source.model_dump(mode="json"),
        "final": value.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_final(value: FinalTranscript, source: CaptureRange) -> FinalTranscript:
    if not value.is_final:
        raise ValueError("只有 final 转录可以进入正式授权")
    if not value.transcript.strip():
        raise ValueError("final 转录不能为空")
    if value.identity.session_id != source.session_id:
        raise ValueError("final 转录会话与采集范围不一致")
    if value.capture_instance_id != source.capture_instance_id:
        raise ValueError("final 转录采集实例与本地范围不一致")
    if value.media_session_id != source.media_session_id:
        raise ValueError("final 转录媒体会话与本地范围不一致")
    if source.end_frame < source.start_frame:
        raise ValueError("采集范围结束帧早于开始帧")
    if source.end_monotonic_ms < source.start_monotonic_ms:
        raise ValueError("采集范围单调时钟结束值早于开始值")
    return value


class InputAuthorizationService:
    """Serialize local authorization; A owns durable event deduplication."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._by_range: dict[str, tuple[str, InputCommitReceipt]] = {}

    async def commit(
        self, value: FinalTranscript, source: CaptureRange
    ) -> InputCommitReceipt:
        validate_final(value, source)
        key = _range_key(source)
        fingerprint = _fingerprint(source, value)
        async with self._lock:
            existing = self._by_range.get(key)
            if existing is not None:
                old_fingerprint, receipt = existing
                if old_fingerprint != fingerprint:
                    raise AuthorizationConflict("同一采集范围不能提交不同内容")
                return InputCommitReceipt(
                    event_id=receipt.event_id,
                    identity=receipt.identity,
                    transcript=receipt.transcript,
                    range_key=receipt.range_key,
                    deduplicated=True,
                )
            receipt = InputCommitReceipt(
                event_id=uuid4(),
                identity=value.identity,
                transcript=value.transcript,
                range_key=key,
                deduplicated=False,
            )
            self._by_range[key] = (fingerprint, receipt)
            return receipt
