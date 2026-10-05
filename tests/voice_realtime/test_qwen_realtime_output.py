from __future__ import annotations

import base64

import pytest

from services.qwen_realtime_output import (
    NativeAudioChunk,
    OutputMappingError,
    QwenRealtimeOutputAdapter,
)
from services.qwen_realtime_protocol import parse_event


def _delta(
    *,
    response_id: str = "resp-supplier",
    item_id: str = "item-1",
    output_index: int = 0,
    pcm: bytes = b"\x01\x00\x02\x00",
):
    return parse_event(
        {
            "type": "response.audio.delta",
            "response_id": response_id,
            "item_id": item_id,
            "output_index": output_index,
            "delta": base64.b64encode(pcm).decode("ascii"),
        }
    )


def _created(response_id: str = "resp-supplier"):
    return parse_event(
        {"type": "response.created", "response": {"id": response_id}}
    )


def test_protocol_exposes_audio_delta_identity_and_payload():
    event = _delta()

    assert event.type == "response.audio.delta"
    assert event.response_id == "resp-supplier"
    assert event.item_id == "item-1"
    assert event.output_index == 0
    assert event.audio == base64.b64encode(b"\x01\x00\x02\x00").decode("ascii")


def test_adapter_maps_deltas_to_local_response_epoch_segment_and_chunk():
    adapter = QwenRealtimeOutputAdapter(
        response_id="resp-local",
        output_epoch=7,
        media_session_id="media-1",
    )
    adapter.bind(_created())

    first = adapter.accept(_delta())
    second = adapter.accept(_delta())
    other_segment = adapter.accept(_delta(item_id="item-2", output_index=1))

    assert first == NativeAudioChunk(
        response_id="resp-local",
        output_epoch=7,
        segment_id="item-1",
        segment_index=0,
        chunk_seq=1,
        media_session_id="media-1",
        pcm_bytes=b"\x01\x00\x02\x00",
    )
    assert second.chunk_seq == 2
    assert other_segment.segment_id == "item-2"
    assert other_segment.segment_index == 1
    assert other_segment.chunk_seq == 1


def test_adapter_rejects_unbound_or_mismatched_identity():
    adapter = QwenRealtimeOutputAdapter(
        response_id="resp-local",
        output_epoch=1,
        media_session_id="media-1",
    )

    with pytest.raises(OutputMappingError, match="response.created"):
        adapter.accept(_delta())
    adapter.bind(_created())
    with pytest.raises(OutputMappingError, match="supplier response_id"):
        adapter.accept(_delta(response_id="resp-old"))
    with pytest.raises(OutputMappingError, match="item_id"):
        adapter.accept(_delta(item_id=""))
    with pytest.raises(OutputMappingError, match="output_index"):
        adapter.accept(_delta(output_index=-1))
    with pytest.raises(OutputMappingError, match="delta"):
        adapter.accept(_delta(pcm=b""))


def test_adapter_rejects_invalid_pcm_and_late_delta_after_done():
    adapter = QwenRealtimeOutputAdapter(
        response_id="resp-local",
        output_epoch=1,
        media_session_id="media-1",
    )
    adapter.bind(_created())

    with pytest.raises(OutputMappingError, match="PCM"):
        adapter.accept(_delta(pcm=b"\x01"))

    done = parse_event(
        {"type": "response.done", "response": {"id": "resp-supplier"}}
    )
    assert adapter.finish(done) is True
    with pytest.raises(OutputMappingError, match="closed"):
        adapter.accept(_delta())
