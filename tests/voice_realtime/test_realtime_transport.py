from __future__ import annotations

import base64
from uuid import uuid4

import pytest
from pydantic import ValidationError

from services.voice_realtime_transport import (
    ClientTransportError,
    VoiceClientTransport,
    parse_client_message,
    parse_server_message,
)


def envelope(
    message_type: str,
    producer_seq: int,
    *,
    session_id=None,
    **payload,
):
    return {
        "schema_version": 1,
        "type": message_type,
        "session_id": str(session_id or uuid4()),
        "media_session_id": "media-1",
        "producer_seq": producer_seq,
        **payload,
    }


def test_parses_client_message_union_and_rejects_unknown_type():
    capture = parse_client_message(
        envelope(
            "capture_open",
            0,
            capture_instance_id=str(uuid4()),
            sample_rate=16000,
            channels=1,
            sample_format="s16le",
        )
    )
    assert capture.type == "capture_open"

    with pytest.raises(ValidationError):
        parse_client_message(envelope("vendor.unknown", 0))


def test_client_message_rejects_bool_masquerading_as_integer():
    value = envelope(
        "audio_chunk",
        0,
        capture_instance_id=str(uuid4()),
        start_frame=0,
        end_frame=True,
        start_monotonic_ms=0,
        end_monotonic_ms=10,
        pcm_base64="AAAA",
    )

    with pytest.raises(ValidationError):
        parse_client_message(value)


def test_transport_rejects_duplicate_or_out_of_order_producer_seq():
    session_id = uuid4()
    transport = VoiceClientTransport(
        session_id=session_id, media_session_id="media-1"
    )
    capture_instance = uuid4()
    capture = parse_client_message(
        envelope(
            "capture_open",
            0,
            session_id=session_id,
            capture_instance_id=str(capture_instance),
            sample_rate=16000,
            channels=1,
            sample_format="s16le",
        )
    )
    transport.accept(capture)

    duplicate = parse_client_message(
        envelope(
            "capture_open",
            0,
            session_id=session_id,
            capture_instance_id=str(capture_instance),
            sample_rate=16000,
            channels=1,
            sample_format="s16le",
        )
    )
    with pytest.raises(ClientTransportError, match="producer_seq"):
        transport.accept(duplicate)


def test_transport_rejects_range_conflict_and_bad_pcm_length():
    session_id = uuid4()
    transport = VoiceClientTransport(
        session_id=session_id, media_session_id="media-1"
    )
    capture_instance = uuid4()
    transport.accept(
        parse_client_message(
            envelope(
                "capture_open",
                0,
                session_id=session_id,
                capture_instance_id=str(capture_instance),
                sample_rate=16000,
                channels=1,
                sample_format="s16le",
            )
        )
    )
    conflict = parse_client_message(
        envelope(
            "audio_chunk",
            1,
            session_id=session_id,
            capture_instance_id=str(capture_instance),
            start_frame=2,
            end_frame=4,
            start_monotonic_ms=0,
            end_monotonic_ms=10,
            pcm_base64=base64.b64encode(b"\x01\x00\x02\x00").decode(),
        )
    )
    with pytest.raises(ClientTransportError, match="range"):
        transport.accept(conflict)

    bad_length = parse_client_message(
        envelope(
            "audio_chunk",
            2,
            session_id=session_id,
            capture_instance_id=str(capture_instance),
            start_frame=0,
            end_frame=2,
            start_monotonic_ms=0,
            end_monotonic_ms=10,
            pcm_base64=base64.b64encode(b"\x01\x00").decode(),
        )
    )
    with pytest.raises(ClientTransportError, match="PCM"):
        transport.accept(bad_length)


def test_transport_rejects_oversized_base64_and_wrong_identity():
    session_id = uuid4()
    transport = VoiceClientTransport(
        session_id=session_id, media_session_id="media-1"
    )
    capture_instance = uuid4()
    transport.accept(
        parse_client_message(
            envelope(
                "capture_open",
                0,
                session_id=session_id,
                capture_instance_id=str(capture_instance),
                sample_rate=16000,
                channels=1,
                sample_format="s16le",
            )
        )
    )
    oversized = envelope(
        "audio_chunk",
        1,
        session_id=session_id,
        capture_instance_id=str(capture_instance),
        start_frame=0,
        end_frame=2,
        start_monotonic_ms=0,
        end_monotonic_ms=10,
        pcm_base64=base64.b64encode(b"\x01\x00\x02\x00").decode() + "A" * 1_400_000,
    )
    with pytest.raises(ValidationError):
        parse_client_message(oversized)

    wrong_identity = parse_client_message(
        envelope(
            "control",
            1,
            action="close",
            reason="manual",
        )
    )
    with pytest.raises(ClientTransportError, match="identity"):
        transport.accept(wrong_identity)


def test_parses_server_ready_audio_generation_event_and_error():
    session_id = uuid4()
    ready = parse_server_message(
        {
            "schema_version": 1,
            "type": "ready",
            "session_id": str(session_id),
            "media_session_id": "media-1",
            "producer_seq": 0,
            "cloud_session_id": "qwen-1",
            "instructions_hash": "a" * 64,
        }
    )
    audio = parse_server_message(
        {
            "schema_version": 1,
            "type": "audio_chunk",
            "session_id": str(session_id),
            "media_session_id": "media-1",
            "producer_seq": 1,
            "response_id": "resp-1",
            "output_epoch": 1,
            "segment_id": "segment-1",
            "segment_index": 0,
            "chunk_seq": 1,
            "pcm_base64": "AAAA",
        }
    )
    generation = parse_server_message(
        {
            "schema_version": 1,
            "type": "generation",
            "session_id": str(session_id),
            "media_session_id": "media-1",
            "producer_seq": 2,
            "response_id": "resp-1",
            "output_epoch": 1,
            "kind": "text_delta",
            "text": "你好",
        }
    )
    error = parse_server_message(
        {
            "schema_version": 1,
            "type": "error",
            "session_id": str(session_id),
            "media_session_id": "media-1",
            "producer_seq": 3,
            "stage": "cloud",
            "code": "unavailable",
            "message": "Cloud unavailable",
            "recoverable": True,
        }
    )

    assert ready.type == "ready"
    assert audio.type == "audio_chunk"
    assert generation.type == "generation"
    assert error.type == "error"
