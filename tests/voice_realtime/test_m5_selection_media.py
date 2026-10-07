from __future__ import annotations

import base64
import hashlib
import json
from uuid import uuid4

import pytest

from services.voice_realtime_transport import (
    ClientTransportError,
    VoiceClientTransport,
    parse_client_message,
)


def _envelope(session_id, media_session_id, producer_seq, **fields):
    return {
        "schema_version": 1,
        "session_id": str(session_id),
        "media_session_id": media_session_id,
        "producer_seq": producer_seq,
        **fields,
    }


def _chunk(session_id, media_id, capture_id, seq, start, end, pcm):
    return _envelope(
        session_id,
        media_id,
        seq,
        type="audio_chunk",
        capture_instance_id=str(capture_id),
        start_frame=start,
        end_frame=end,
        start_monotonic_ms=start,
        end_monotonic_ms=end,
        pcm_base64=base64.b64encode(pcm).decode("ascii"),
    )


def test_selection_pause_discards_uncommitted_audio_and_resumes_same_capture_identity():
    session_id = uuid4()
    capture_id = uuid4()
    media_id = "media-selection-pause"
    transport = VoiceClientTransport(
        session_id=session_id,
        media_session_id=media_id,
    )
    transport.accept(
        parse_client_message(
            _envelope(
                session_id,
                media_id,
                0,
                type="capture_open",
                capture_instance_id=str(capture_id),
                sample_rate=16000,
                channels=1,
                sample_format="s16le",
            )
        )
    )
    first_pcm = b"\x01\x00" * 8
    transport.accept(
        parse_client_message(_chunk(session_id, media_id, capture_id, 1, 0, 8, first_pcm))
    )

    suspended = transport.accept(
        parse_client_message(
            _envelope(
                session_id,
                media_id,
                2,
                type="capture_suspend",
                capture_instance_id=str(capture_id),
                control_id=str(uuid4()),
            )
        )
    )

    assert suspended["accepted"] is True
    assert suspended["state"] == "suspended"
    assert suspended["discarded_range"] == {"start_frame": 0, "end_frame": 8}
    with pytest.raises(ClientTransportError, match="suspended"):
        transport.accept(
            parse_client_message(_chunk(session_id, media_id, capture_id, 3, 8, 16, first_pcm))
        )

    resumed = transport.accept(
        parse_client_message(
            _envelope(
                session_id,
                media_id,
                4,
                type="capture_resume",
                capture_instance_id=str(capture_id),
                control_id=str(uuid4()),
            )
        )
    )
    second_pcm = b"\x02\x00" * 8
    transport.accept(
        parse_client_message(_chunk(session_id, media_id, capture_id, 5, 8, 16, second_pcm))
    )
    committed = transport.accept(
        parse_client_message(
            _envelope(
                session_id,
                media_id,
                6,
                type="utterance_end",
                capture_instance_id=str(capture_id),
                start_frame=8,
                end_frame=16,
                start_monotonic_ms=8,
                end_monotonic_ms=16,
                audio_sha256=hashlib.sha256(second_pcm).hexdigest(),
            )
        )
    )

    assert resumed == {"accepted": True, "state": "capturing"}
    assert committed["accepted"] is True
    assert committed["start_frame"] == 8
    assert committed["end_frame"] == 16


def test_selection_resume_rejects_a_different_capture_identity():
    session_id = uuid4()
    capture_id = uuid4()
    transport = VoiceClientTransport(
        session_id=session_id,
        media_session_id="media-selection-identity",
    )
    transport.accept(
        parse_client_message(
            _envelope(
                session_id,
                "media-selection-identity",
                0,
                type="capture_open",
                capture_instance_id=str(capture_id),
                sample_rate=16000,
                channels=1,
                sample_format="s16le",
            )
        )
    )
    transport.accept(
        parse_client_message(
            _envelope(
                session_id,
                "media-selection-identity",
                1,
                type="capture_suspend",
                capture_instance_id=str(capture_id),
                control_id=str(uuid4()),
            )
        )
    )
    with pytest.raises(ClientTransportError, match="was not opened"):
        transport.accept(
            parse_client_message(
                _envelope(
                    session_id,
                    "media-selection-identity",
                    2,
                    type="capture_resume",
                    capture_instance_id=str(uuid4()),
                    control_id=str(uuid4()),
                )
            )
        )
