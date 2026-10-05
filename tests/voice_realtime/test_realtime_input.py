from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from services.qwen_realtime_protocol import ProtocolEvent
from services.voice_input_authorization import AuthorizationConflict, CaptureRange
from services.voice_realtime_session import VoiceRealtimeSession

pytestmark = [pytest.mark.asyncio(loop_scope="function")]


class FakeClient:
    def __init__(self, events: list[ProtocolEvent]):
        self.events_value = events
        self.commit_audio_calls = 0

    async def connect(self, _instructions: str, *, tools=None) -> str:
        return "cloud-session"

    async def events(self) -> AsyncIterator[ProtocolEvent]:
        for event in self.events_value:
            yield event

    async def commit_audio(self) -> None:
        self.commit_audio_calls += 1

    async def close(self) -> None:
        return None


def source(*, media_session_id: str = "media-1") -> CaptureRange:
    return CaptureRange(
        session_id=SESSION_ID,
        media_session_id=media_session_id,
        capture_instance_id=CAPTURE_ID,
        start_frame=100,
        end_frame=420,
        start_monotonic_ms=50,
        end_monotonic_ms=70,
        audio_sha256="b" * 64,
    )


SESSION_ID = uuid4()
CAPTURE_ID = uuid4()


def make_client(transcripts: tuple[str, ...]) -> FakeClient:
    events = []
    for index, transcript in enumerate(transcripts, start=1):
        item_id = f"item-{index}"
        events.extend(
            [
                ProtocolEvent(type="input_audio_buffer_committed", item_id=item_id),
                ProtocolEvent(
                    type="input_transcript_final",
                    item_id=item_id,
                    transcript=transcript,
                    is_final=True,
                ),
            ]
        )
    return FakeClient(events)


async def make_session(client: FakeClient) -> VoiceRealtimeSession:
    session = VoiceRealtimeSession(
        session_id=SESSION_ID,
        media_session_id="media-1",
        client=client,
    )
    await session.start("stable instructions")
    asyncio.create_task(session.run_receiver())
    await asyncio.sleep(0)
    return session


async def test_old_media_range_cannot_reach_cloud_commit():
    client = make_client(("hello",))
    session = await make_session(client)

    with pytest.raises(RuntimeError, match="old media session"):
        await session.process_utterance(source(media_session_id="old-media"))

    assert client.commit_audio_calls == 0


async def test_empty_cloud_final_is_diagnostic_without_authorization():
    client = make_client(("  ",))
    session = await make_session(client)
    events = []

    async def sink(event_type, payload, *_args):
        events.append((event_type, payload))

    session._event_sink = sink
    authorized = []

    with pytest.raises(RuntimeError, match="no usable final transcript"):
        await session.process_utterance(
            source(), on_authorized=lambda receipt: authorized.append(receipt)
        )

    assert authorized == []
    assert [name for name, _payload in events].count(
        "speech_without_usable_transcript"
    ) == 1
    assert "voice_input_committed" not in [name for name, _payload in events]


async def test_same_range_with_conflicting_final_cannot_authorize_twice():
    client = make_client(("first wording", "changed wording"))
    session = await make_session(client)
    authorized = []

    await session.process_utterance(
        source(), on_authorized=lambda receipt: authorized.append(receipt)
    )
    with pytest.raises(AuthorizationConflict):
        await session.process_utterance(
            source(), on_authorized=lambda receipt: authorized.append(receipt)
        )

    assert len(authorized) == 1
    assert client.commit_audio_calls == 2
