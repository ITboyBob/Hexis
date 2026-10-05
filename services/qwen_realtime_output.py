"""Explicit mapping from Qwen Realtime audio deltas to native playback identity."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

from services.qwen_realtime_protocol import ProtocolEvent


class OutputMappingError(RuntimeError):
    """A vendor audio delta cannot be bound to the local output identity."""


@dataclass(frozen=True)
class NativeAudioChunk:
    response_id: str
    output_epoch: int
    segment_id: str
    segment_index: int
    chunk_seq: int
    media_session_id: str
    pcm_bytes: bytes


@dataclass(frozen=True)
class OutputPlaybackFacts:
    started: bool = False
    start_ms: int | None = None
    audible_ms: int | None = None
    terminal: str | None = None
    scope: str | None = None
    reason: str | None = None
    played_ms: int = 0


class QwenRealtimeOutputAdapter:
    def __init__(
        self,
        *,
        response_id: str,
        output_epoch: int,
        media_session_id: str,
    ) -> None:
        if not isinstance(response_id, str) or not response_id.strip():
            raise OutputMappingError("response_id must be a non-empty string")
        if not isinstance(output_epoch, int) or isinstance(output_epoch, bool) or output_epoch < 0:
            raise OutputMappingError("output_epoch must be a non-negative integer")
        if not isinstance(media_session_id, str) or not media_session_id.strip():
            raise OutputMappingError("media_session_id must be a non-empty string")
        self._local_response_id = response_id
        self._supplier_response_id: str | None = None
        self._output_epoch = output_epoch
        self._media_session_id = media_session_id
        self._segment_indexes: dict[str, int] = {}
        self._chunk_seqs: dict[str, int] = {}
        self._closed = False

    def bind(self, event: ProtocolEvent) -> None:
        if event.type != "response.created":
            return
        if not isinstance(event.response_id, str) or not event.response_id.strip():
            raise OutputMappingError("response.created requires a supplier response_id")
        if (
            self._supplier_response_id is not None
            and self._supplier_response_id != event.response_id
        ):
            raise OutputMappingError(
                "supplier response_id changed within one local response"
            )
        self._supplier_response_id = event.response_id

    def accept(self, event: ProtocolEvent) -> NativeAudioChunk | None:
        if event.type != "response.audio.delta":
            return None
        if self._closed:
            raise OutputMappingError("response is closed; late audio delta rejected")
        if self._supplier_response_id is None:
            raise OutputMappingError("response.created must bind the supplier response_id")
        if event.response_id != self._supplier_response_id:
            raise OutputMappingError(
                f"supplier response_id mismatch: expected={self._supplier_response_id} "
                f"actual={event.response_id}"
            )
        if not isinstance(event.item_id, str) or not event.item_id.strip():
            raise OutputMappingError("response.audio.delta requires a non-empty item_id")
        if (
            not isinstance(event.output_index, int)
            or isinstance(event.output_index, bool)
            or event.output_index < 0
        ):
            raise OutputMappingError(
                "response.audio.delta requires a non-negative integer output_index"
            )
        if not isinstance(event.audio, str) or not event.audio:
            raise OutputMappingError("response.audio.delta requires a non-empty delta")

        segment_id = event.item_id
        previous_index = self._segment_indexes.get(segment_id)
        if previous_index is not None and previous_index != event.output_index:
            raise OutputMappingError(
                f"output_index changed for segment {segment_id}: "
                f"expected={previous_index} actual={event.output_index}"
            )
        self._segment_indexes[segment_id] = event.output_index

        try:
            pcm_bytes = base64.b64decode(event.audio, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise OutputMappingError("response.audio.delta delta is not valid base64") from exc
        if not pcm_bytes or len(pcm_bytes) % 2:
            raise OutputMappingError(
                "response.audio.delta PCM must be non-empty 16-bit mono frames"
            )

        chunk_seq = self._chunk_seqs.get(segment_id, 0) + 1
        self._chunk_seqs[segment_id] = chunk_seq
        return NativeAudioChunk(
            response_id=self._local_response_id,
            output_epoch=self._output_epoch,
            segment_id=segment_id,
            segment_index=event.output_index,
            chunk_seq=chunk_seq,
            media_session_id=self._media_session_id,
            pcm_bytes=pcm_bytes,
        )

    def finish(self, event: ProtocolEvent) -> bool:
        if event.type not in {"response.done", "response.failed", "response_failed"}:
            return False
        expected_response_id = self._supplier_response_id or self._local_response_id
        if event.response_id is not None and event.response_id != expected_response_id:
            return False
        self._closed = True
        return True


__all__ = [
    "NativeAudioChunk",
    "OutputPlaybackFacts",
    "OutputMappingError",
    "QwenRealtimeOutputAdapter",
]
