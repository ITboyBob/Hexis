from __future__ import annotations

import base64
import hashlib
from uuid import uuid4

import pytest

from services.voice_realtime_transport import (
    ClientTransportError,
    VoiceClientTransport,
    parse_client_message,
)


def envelope(kind: str, capture_id: str, seq: int, **payload):
    return parse_client_message(
        {
            "schema_version": 1,
            "type": kind,
            "session_id": str(SESSION_ID),
            "media_session_id": "media-1",
            "producer_seq": seq,
            "capture_instance_id": capture_id,
            **payload,
        }
    )


SESSION_ID = uuid4()


def test_capture_range_accumulates_contiguous_chunks_until_utterance_end():
    capture_id = str(uuid4())
    transport = VoiceClientTransport(
        session_id=SESSION_ID, media_session_id="media-1"
    )
    transport.accept(
        envelope(
            "capture_open",
            capture_id,
            0,
            sample_rate=16_000,
            channels=1,
            sample_format="s16le",
        )
    )
    first = b"\x01\x00" * 160
    second = b"\x02\x00" * 240
    for seq, start, end, start_ms, end_ms, pcm in (
        (1, 0, 160, 0, 10, first),
        (2, 160, 400, 10, 25, second),
    ):
        transport.accept(
            envelope(
                "audio_chunk",
                capture_id,
                seq,
                start_frame=start,
                end_frame=end,
                start_monotonic_ms=start_ms,
                end_monotonic_ms=end_ms,
                pcm_base64=base64.b64encode(pcm).decode(),
            )
        )

    result = transport.accept(
        envelope(
            "utterance_end",
            capture_id,
            3,
            start_frame=0,
            end_frame=400,
            start_monotonic_ms=0,
            end_monotonic_ms=25,
            audio_sha256=hashlib.sha256(first + second).hexdigest(),
        )
    )

    assert result["accepted"] is True
    assert result["start_frame"] == 0
    assert result["end_frame"] == 400


def test_capture_range_rejects_gaps_inside_an_utterance():
    capture_id = str(uuid4())
    transport = VoiceClientTransport(
        session_id=SESSION_ID, media_session_id="media-1"
    )
    transport.accept(
        envelope(
            "capture_open",
            capture_id,
            0,
            sample_rate=16_000,
            channels=1,
            sample_format="s16le",
        )
    )
    transport.accept(
        envelope(
            "audio_chunk",
            capture_id,
            1,
            start_frame=0,
            end_frame=160,
            start_monotonic_ms=0,
            end_monotonic_ms=10,
            pcm_base64=base64.b64encode(b"\x00\x00" * 160).decode(),
        )
    )
    with pytest.raises(ClientTransportError, match="expected_start"):
        transport.accept(
            envelope(
                "audio_chunk",
                capture_id,
                2,
                start_frame=1,
                end_frame=321,
                start_monotonic_ms=10,
                end_monotonic_ms=30,
                pcm_base64=base64.b64encode(b"\x00\x00" * 320).decode(),
            )
        )
