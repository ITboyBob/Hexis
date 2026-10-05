from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from services.qwen_realtime_protocol import ProtocolEvent
from services.voice_input_authorization import CaptureRange
from services.voice_realtime_session import (
    VoiceRealtimeSession,
    VoiceRealtimeSessionError,
)


pytestmark = [pytest.mark.asyncio(loop_scope="function")]


class FakeClient:
    def __init__(self) -> None:
        self.commit_started = asyncio.Event()
        self.closed = False

    async def commit_audio(self) -> None:
        self.commit_started.set()

    async def close(self) -> None:
        self.closed = True


def make_source() -> CaptureRange:
    return CaptureRange(
        session_id=uuid4(),
        media_session_id="media-recovery",
        capture_instance_id=uuid4(),
        start_frame=0,
        end_frame=320,
        start_monotonic_ms=100,
        end_monotonic_ms=120,
        audio_sha256="a" * 64,
    )


async def test_disconnect_releases_input_worker_waiting_for_commit_without_canceling_it():
    source = make_source()
    client = FakeClient()
    session = VoiceRealtimeSession(
        session_id=source.session_id,
        media_session_id=source.media_session_id,
        client=client,
    )
    session._ready = True
    worker = asyncio.create_task(session.process_utterance(source))
    session.register_background_task(worker)
    await client.commit_started.wait()

    await session.close("disconnect")

    with pytest.raises(VoiceRealtimeSessionError, match="closed"):
        await asyncio.wait_for(worker, timeout=0.1)
    assert worker.done()
    assert not worker.cancelled()
    assert client.closed


async def test_disconnect_releases_input_worker_waiting_for_final_without_canceling_it():
    source = make_source()
    client = FakeClient()
    session = VoiceRealtimeSession(
        session_id=source.session_id,
        media_session_id=source.media_session_id,
        client=client,
    )
    session._ready = True
    session._committed_events.put_nowait(
        ProtocolEvent(type="input_audio_buffer_committed", item_id="item-1")
    )
    worker = asyncio.create_task(session.process_utterance(source))
    session.register_background_task(worker)
    await client.commit_started.wait()
    while "item-1" not in session._final_futures:
        await asyncio.sleep(0)

    await session.close("disconnect")

    with pytest.raises(VoiceRealtimeSessionError, match="closed"):
        await asyncio.wait_for(worker, timeout=0.1)
    assert worker.done()
    assert not worker.cancelled()
