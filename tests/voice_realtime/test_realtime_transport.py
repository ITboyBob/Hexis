from __future__ import annotations

import ast
import base64
import hashlib
from pathlib import Path
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
    first_chunk = parse_client_message(
        envelope(
            "audio_chunk",
            1,
            session_id=session_id,
            capture_instance_id=str(capture_instance),
            start_frame=0,
            end_frame=2,
            start_monotonic_ms=0,
            end_monotonic_ms=10,
            pcm_base64=base64.b64encode(b"\x01\x00\x02\x00").decode(),
        )
    )
    transport.accept(first_chunk)
    conflict = parse_client_message(
        envelope(
            "audio_chunk",
            2,
            session_id=session_id,
            capture_instance_id=str(capture_instance),
            start_frame=3,
            end_frame=5,
            start_monotonic_ms=11,
            end_monotonic_ms=20,
            pcm_base64=base64.b64encode(b"\x01\x00\x02\x00").decode(),
        )
    )
    with pytest.raises(ClientTransportError, match="range"):
        transport.accept(conflict)

    bad_length = parse_client_message(
        envelope(
            "audio_chunk",
            3,
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


def test_transport_allows_silence_gaps_between_voice_utterances():
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

    def send_audio(seq: int, start: int, end: int, start_ms: int, end_ms: int):
        return transport.accept(
            parse_client_message(
                envelope(
                    "audio_chunk",
                    seq,
                    session_id=session_id,
                    capture_instance_id=str(capture_instance),
                    start_frame=start,
                    end_frame=end,
                    start_monotonic_ms=start_ms,
                    end_monotonic_ms=end_ms,
                    pcm_base64=base64.b64encode(b"\x01\x00" * (end - start)).decode(),
                )
            )
        )

    def end_utterance(seq: int, start: int, end: int, start_ms: int, end_ms: int):
        pcm = b"\x01\x00" * (end - start)
        message = parse_client_message(
            envelope(
                "utterance_end",
                seq,
                session_id=session_id,
                capture_instance_id=str(capture_instance),
                start_frame=start,
                end_frame=end,
                start_monotonic_ms=start_ms,
                end_monotonic_ms=end_ms,
                audio_sha256=hashlib.sha256(pcm).hexdigest(),
            )
        )
        return transport.accept(message)

    send_audio(1, 2_045_264, 2_045_280, 10_000, 10_001)
    end_utterance(2, 2_045_264, 2_045_280, 10_000, 10_001)
    send_audio(3, 2_400_000, 2_400_016, 20_000, 20_001)
    end_utterance(4, 2_400_000, 2_400_016, 20_000, 20_001)

    with pytest.raises(ClientTransportError, match="overlaps an earlier utterance"):
        send_audio(5, 2_400_008, 2_400_024, 20_002, 20_003)


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
    capture_instance_id = uuid4()
    control_id = uuid4()
    ready = parse_server_message(
        {
            "schema_version": 1,
            "type": "ready",
            "session_id": str(session_id),
            "media_session_id": "media-1",
            "producer_seq": 0,
            "cloud_session_id": "qwen-1",
            "instructions_hash": "a" * 64,
            "barge_in_capability": {
                "supported": True,
                "configuration_id": "voice-barge-in-playback-initial-v1",
                "maximum_receipt_prefix_items": 256,
            },
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
            "playback_id": "playback-1",
            "turn_id": None,
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
            "playback_id": "playback-1",
            "turn_id": None,
            "output_epoch": 1,
            "kind": "text_delta",
            "text": "你好",
        }
    )
    generation_done = parse_server_message(
        {
            "schema_version": 1,
            "type": "generation",
            "session_id": str(session_id),
            "media_session_id": "media-1",
            "producer_seq": 3,
            "response_id": "resp-1",
            "playback_id": "playback-1",
            "turn_id": str(uuid4()),
            "output_epoch": 1,
            "kind": "done",
            "reason": None,
        }
    )
    memory_recall = parse_server_message(
        {
            "schema_version": 1,
            "type": "memory_recall",
            "session_id": str(session_id),
            "media_session_id": "media-1",
            "producer_seq": 4,
            "input_turn_id": str(uuid4()),
            "stimulus_revision": 1,
            "count": 1,
            "memories": [
                {
                    "id": str(uuid4()),
                    "type": "semantic",
                    "content": "用户偏好中文",
                    "similarity": 0.91,
                    "source": "vector",
                }
            ],
            "hydrate_ms": 12.5,
            "recall_performed": True,
        }
    )
    selection = parse_server_message(
        {
            "schema_version": 1,
            "type": "m5_selection_opened",
            "session_id": str(session_id),
            "media_session_id": "media-1",
            "producer_seq": 4,
            "selection": {
                "selection_id": "selection-1",
                "version": 2,
                "message": "请选择下一步",
                "state": "open",
                "origin_session_id": str(session_id),
            },
        }
    )
    suspended = parse_server_message(
        {
            "schema_version": 1,
            "type": "capture_suspended",
            "session_id": str(session_id),
            "media_session_id": "media-1",
            "producer_seq": 5,
            "capture_instance_id": str(capture_instance_id),
            "control_id": str(control_id),
            "discarded_range": {"start_frame": 256, "end_frame": 512},
        }
    )
    resumed = parse_server_message(
        {
            "schema_version": 1,
            "type": "capture_resumed",
            "session_id": str(session_id),
            "media_session_id": "media-1",
            "producer_seq": 6,
            "capture_instance_id": str(capture_instance_id),
            "control_id": str(uuid4()),
        }
    )
    error = parse_server_message(
        {
            "schema_version": 1,
            "type": "error",
            "session_id": str(session_id),
            "media_session_id": "media-1",
            "producer_seq": 7,
            "stage": "cloud",
            "code": "unavailable",
            "message": "Cloud unavailable",
            "recoverable": True,
        }
    )

    assert ready.type == "ready"
    assert audio.type == "audio_chunk"
    assert audio.playback_id == "playback-1"
    assert audio.turn_id is None
    assert generation.type == "generation"
    assert generation.turn_id is None
    assert generation_done.reason is None
    assert memory_recall.type == "memory_recall"
    assert memory_recall.count == 1
    assert memory_recall.memories[0].content == "用户偏好中文"
    assert selection.type == "m5_selection_opened"
    assert selection.selection.version == 2
    assert suspended.type == "capture_suspended"
    assert suspended.discarded_range.start_frame == 256
    assert resumed.type == "capture_resumed"
    assert error.type == "error"


def test_server_media_messages_reject_unknown_fields():
    with pytest.raises(ValidationError):
        parse_server_message(
            {
                "schema_version": 1,
                "type": "capture_resumed",
                "session_id": str(uuid4()),
                "media_session_id": "media-1",
                "producer_seq": 1,
                "capture_instance_id": str(uuid4()),
                "control_id": str(uuid4()),
                "unsupported": True,
            }
        )


def test_server_memory_recall_rejects_count_or_policy_conflicts():
    base = {
        "schema_version": 1,
        "type": "memory_recall",
        "session_id": str(uuid4()),
        "media_session_id": "media-1",
        "producer_seq": 1,
        "input_turn_id": str(uuid4()),
        "stimulus_revision": 0,
        "count": 0,
        "memories": [],
        "hydrate_ms": 1.0,
        "recall_performed": True,
    }
    with pytest.raises(ValidationError, match="count"):
        parse_server_message({
            **base,
            "count": 1,
        })
    with pytest.raises(ValidationError, match="disabled state"):
        parse_server_message({
            **base,
            "count": 1,
            "memories": [{
                "id": str(uuid4()),
                "type": "semantic",
                "content": "不应出现",
            }],
            "recall_performed": False,
        })


def test_server_generation_variants_require_their_production_fields_and_reject_mixed_shapes():
    base = {
        "schema_version": 1,
        "type": "generation",
        "session_id": str(uuid4()),
        "media_session_id": "media-1",
        "producer_seq": 1,
        "response_id": "response-1",
        "playback_id": "playback-1",
        "turn_id": None,
        "output_epoch": 1,
    }
    with pytest.raises(ValidationError):
        parse_server_message({**base, "kind": "text_delta"})
    with pytest.raises(ValidationError):
        parse_server_message({**base, "kind": "done"})
    with pytest.raises(ValidationError):
        parse_server_message({**base, "kind": "done", "reason": None, "text": "unexpected"})
    with pytest.raises(ValidationError):
        parse_server_message({**base, "kind": "text_delta", "text": "你好", "reason": None})


def test_m5_recall_done_production_shape_uses_explicit_null_reason():
    source = Path(__file__).resolve().parents[2] / "apps/hexis_api.py"
    done_senders = []
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Dict):
            continue
        literals = {
            key.value: value.value
            for key, value in zip(node.keys, node.values)
            if isinstance(key, ast.Constant) and isinstance(value, ast.Constant)
        }
        if literals.get("type") == "generation" and literals.get("kind") == "done":
            done_senders.append(node)
    assert len(done_senders) == 1
    bindings = envelope(
        "generation", 1, response_id="m5-response", playback_id="m5-playback",
        turn_id=None, output_epoch=1,
    )
    fixture = {
        key.value: value.value if isinstance(value, ast.Constant) else bindings[key.value]
        for key, value in zip(done_senders[0].keys, done_senders[0].values)
    }
    message = parse_server_message(fixture)
    assert message.kind == "done"
    assert message.reason is None
    assert "reason" in message.model_fields_set


def test_failed_generation_preserves_the_explicit_provider_reason():
    fixture = envelope(
        "generation", 1, response_id="response-1", playback_id="playback-1",
        turn_id=None, output_epoch=1, kind="failed", reason="provider unavailable",
    )
    assert parse_server_message(fixture).reason == "provider unavailable"
    with pytest.raises(ValidationError):
        parse_server_message({key: value for key, value in fixture.items() if key != "reason"})
