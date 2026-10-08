from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from services.voice_realtime_session import VoiceRealtimeSession

pytestmark = pytest.mark.asyncio(loop_scope="function")


class _Client:
    def __init__(self) -> None:
        self.cancel_calls = 0

    async def cancel_response(self) -> None:
        self.cancel_calls += 1


async def test_queued_old_cancel_does_not_retarget_new_foreground_response():
    client = _Client()
    session = VoiceRealtimeSession(
        session_id=uuid4(),
        media_session_id="media-1",
        client=client,
    )
    first = await session._output_arbiter.claim("response-a", session.media_session_id)
    session._response_claims[first.response_id] = first
    session._playback_ids[first.response_id] = "playback-a"
    session._response_queues[first.response_id] = asyncio.Queue()
    session._playback_state[first.response_id] = {
        "output_epoch": first.output_epoch,
        "playback_id": "playback-a",
        "scheduled_frames": 0,
        "scheduled_ms": 0,
        "opened": False,
        "terminal": False,
        "last_seq": 0,
        "receipts": {},
        "played_ms": 0,
        "invalidated": False,
    }
    session._responses_idle.clear()
    cancel_waiting = asyncio.Event()

    async with session._foreground_command_lock:
        cancellation = asyncio.create_task(session.invalidate_foreground_output())
        await asyncio.sleep(0)
        cancel_waiting.set()
        await cancel_waiting.wait()

        await session._release_response_locked("response-a")
        second = await session._output_arbiter.claim("response-b", session.media_session_id)
        session._response_claims[second.response_id] = second
        session._playback_ids[second.response_id] = "playback-b"
        session._response_queues[second.response_id] = asyncio.Queue()
        session._playback_state[second.response_id] = {
            "output_epoch": second.output_epoch,
            "playback_id": "playback-b",
            "scheduled_frames": 0,
            "scheduled_ms": 0,
            "opened": False,
            "terminal": False,
            "last_seq": 0,
            "receipts": {},
            "played_ms": 0,
            "invalidated": False,
        }
        session._responses_idle.clear()

    assert await asyncio.wait_for(cancellation, timeout=1) is None
    assert session.active_response_id == "response-b"
    assert not session.response_is_invalidated("response-b")
    assert client.cancel_calls == 0
