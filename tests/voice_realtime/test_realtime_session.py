from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from services.qwen_realtime_protocol import ProtocolEvent
from services.voice_realtime_session import (
    ActiveVoiceSessionExists,
    VoiceRealtimeSession,
    VoiceRealtimeSessionError,
    VoiceRealtimeSessionRegistry,
)

pytestmark = [pytest.mark.asyncio(loop_scope="function")]


class FakeClient:
    def __init__(self, events: list[ProtocolEvent], *, session_id: str = "qwen-1"):
        self.events_value = list(events)
        self.session_id = session_id
        self.closed = False

    async def connect(self, _instructions: str) -> str:
        return self.session_id

    async def events(self) -> AsyncIterator[ProtocolEvent]:
        for event in self.events_value:
            yield event

    async def close(self) -> None:
        self.closed = True


def make_session(client: FakeClient) -> VoiceRealtimeSession:
    return VoiceRealtimeSession(
        session_id=uuid4(),
        media_session_id="media-1",
        client=client,
    )


async def test_only_one_active_session_per_chat():
    registry = VoiceRealtimeSessionRegistry()
    session_id = uuid4()
    first = make_session(FakeClient([]))
    second = make_session(FakeClient([]))

    registry.register(session_id, first)
    with pytest.raises(ActiveVoiceSessionExists):
        registry.register(session_id, second)

    registry.remove(session_id, first)
    registry.register(session_id, second)
    assert registry.get(session_id) is second


async def test_session_updated_is_required():
    session = make_session(FakeClient([], session_id=""))

    with pytest.raises(VoiceRealtimeSessionError, match="session.updated"):
        await session.start("稳定层")


async def test_one_receiver_routes_final_and_audio_without_loss():
    response_id = "resp-1"
    client = FakeClient(
        [
            ProtocolEvent(
                type="input_audio_buffer_committed",
                item_id="item-1",
            ),
            ProtocolEvent(
                type="input_transcript_final",
                item_id="item-1",
                transcript="请继续",
                is_final=True,
            ),
            ProtocolEvent(
                type="response.created",
                response_id=response_id,
            ),
            ProtocolEvent(
                type="response.audio.delta",
                response_id=response_id,
                item_id="segment-1",
                output_index=0,
                audio="AQIDBA==",
            ),
            ProtocolEvent(
                type="response.done",
                response_id=response_id,
                status="completed",
            ),
        ]
    )
    session = make_session(client)
    await session.start("稳定层")
    response_queue = session.subscribe_response(response_id)
    receiver = asyncio.create_task(session.run_receiver())

    committed = await session.wait_for_input_committed()
    final = await session.wait_for_final("item-1")
    routed = []
    while True:
        event = await response_queue.get()
        routed.append(event)
        if event.type in {"response.done", "response_failed"}:
            break
    await receiver

    assert committed.item_id == "item-1"
    assert final.transcript == "请继续"
    assert [event.type for event in routed] == [
        "response.created",
        "response.audio.delta",
        "response.done",
    ]
    assert routed[-1].status == "completed"


async def test_close_never_cancels_background_task():
    session = make_session(FakeClient([]))
    background = asyncio.create_task(asyncio.sleep(10))
    session.register_background_task(background)

    await session.close("manual")

    assert session.client.closed is True
    assert not background.cancelled()
    assert not background.done()
    background.cancel()
    with pytest.raises(asyncio.CancelledError):
        await background
