from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.qwen_realtime_protocol import ProtocolEvent
from services.voice_realtime_session import (
    ActiveVoiceSessionExists,
    VoiceRealtimeSession,
    VoiceRealtimeSessionError,
    VoiceRealtimeSessionRegistry,
    VoiceTranscriptUnavailable,
)
from services.voice_input_authorization import CaptureRange

pytestmark = [pytest.mark.asyncio(loop_scope="function")]


class FakeClient:
    def __init__(self, events: list[ProtocolEvent], *, session_id: str = "qwen-1"):
        self.events_value = list(events)
        self.session_id = session_id
        self.closed = False
        self.commit_audio_calls = 0
        self.responses: list[tuple[str, str]] = []
        self.instruction_updates: list[str] = []
        self.function_outputs: list[tuple[str, str]] = []

    async def connect(self, _instructions: str, *, tools=None) -> str:
        self.tools = tools
        return self.session_id

    async def events(self) -> AsyncIterator[ProtocolEvent]:
        for event in self.events_value:
            yield event

    async def close(self) -> None:
        self.closed = True

    async def commit_audio(self) -> None:
        self.commit_audio_calls += 1

    async def cancel_response(self) -> None:
        self.cancel_calls = getattr(self, "cancel_calls", 0) + 1

    async def send_function_call_output(self, call_id: str, output: str) -> None:
        self.function_outputs.append((call_id, output))

    async def update_session_instructions(self, instructions: str) -> None:
        self.instruction_updates.append(instructions)

    async def create_first_response(self, response_id: str) -> None:
        self.responses.append((response_id, self.instruction_updates[-1]))


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


async def test_recent_closed_media_session_remains_addressable_for_playback_receipts():
    registry = VoiceRealtimeSessionRegistry()
    session_id = uuid4()
    session = make_session(FakeClient([]))
    registry.register(session_id, session)
    registry.remove(session_id, session)

    assert registry.get(session_id) is None
    assert registry.get_playback_session(session_id, session.media_session_id) is session
    assert registry.get_playback_session(uuid4(), session.media_session_id) is None


async def test_session_updated_is_required():
    session = make_session(FakeClient([], session_id=""))

    with pytest.raises(VoiceRealtimeSessionError, match="session.updated"):
        await session.start("稳定层")


async def test_barge_in_invalidates_foreground_before_cloud_cancel():
    client = FakeClient([])
    session = make_session(client)
    session._ready = True
    claim = await session._output_arbiter.claim("resp-1", session.media_session_id)
    session._response_claims["resp-1"] = claim
    session._playback_ids["resp-1"] = "play-1"
    session._playback_state["resp-1"] = {
        "output_epoch": claim.output_epoch,
        "playback_id": "play-1",
        "scheduled_frames": 120,
        "scheduled_ms": 5,
        "opened": True,
        "terminal": False,
        "last_seq": 1,
        "receipts": {},
        "played_ms": 0,
        "invalidated": False,
    }

    assert await session.invalidate_foreground_output() == "resp-1"
    assert session.response_is_invalidated("resp-1")
    assert session.active_response_id is None
    assert client.cancel_calls == 1
    with pytest.raises(VoiceRealtimeSessionError, match="invalidated"):
        session.record_output_chunk("resp-1", b"\x00\x00")


async def test_invalidated_playback_accepts_only_one_bound_terminal_receipt():
    session = make_session(FakeClient([]))
    input_turn_id = str(uuid4())
    turn_id = uuid4()
    session._voice_turns[input_turn_id] = {"turn_id": turn_id}
    session._response_input_ids["resp-1"] = input_turn_id
    session._playback_ids["resp-1"] = "play-1"
    session._playback_state["resp-1"] = {
        "output_epoch": 1,
        "playback_id": "play-1",
        "scheduled_frames": 4800,
        "scheduled_ms": 200,
        "opened": True,
        "terminal": False,
        "last_seq": 1,
        "receipts": {},
        "played_ms": 20,
        "invalidated": True,
    }

    def receipt(action, seq, **values):
        data = {
            "action": action,
            "session_id": session.session_id,
            "response_id": "resp-1",
            "playback_id": "play-1",
            "producer_seq": seq,
            "output_epoch": 1,
            "media_session_id": session.media_session_id,
            "turn_id": turn_id,
            "model_dump": lambda **_kwargs: {"action": action, "producer_seq": seq, **values},
        }
        data.update(values)
        return SimpleNamespace(**data)

    with pytest.raises(VoiceRealtimeSessionError, match="invalidated"):
        session.accept_playback_receipt(receipt("progress", 2, played_ms=30))
    assert session.accept_playback_receipt(
        receipt("interrupt", 2, played_ms=30, reason="barge_in")
    )
    assert session._playback_state["resp-1"]["terminal"] is True


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


async def test_one_receiver_binds_supplier_response_id_to_one_pending_local_response():
    client = FakeClient(
        [
            ProtocolEvent(type="response.created", response_id="supplier-response-1"),
            ProtocolEvent(
                type="response.audio.delta",
                response_id="supplier-response-1",
                item_id="segment-1",
                output_index=0,
                audio="AQIDBA==",
            ),
            ProtocolEvent(
                type="response.done",
                response_id="supplier-response-1",
                status="completed",
            ),
        ]
    )
    session = make_session(client)
    await session.start("stable")
    output = session.subscribe_response("local-response-1")

    await session.run_receiver()

    events = [await output.get(), await output.get(), await output.get()]
    assert [event.type for event in events] == [
        "response.created",
        "response.audio.delta",
        "response.done",
    ]
    assert events[1].response_id == "supplier-response-1"


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


async def test_response_instructions_use_only_current_accepted_c_gate(monkeypatch):
    session = make_session(FakeClient([]))
    identity = {
        "session_id": str(session.session_id),
        "input_turn_id": "turn-1",
        "input_order": 0,
        "stimulus_revision": 0,
    }
    hydration = SimpleNamespace(
        temporal_context="2026-10-06 12:00 (Asia/Shanghai)",
        persona_context="Name: Hexis\nValues: honesty",
        continuity_context="## Conversation Continuity Packet\nNo prior turns.",
        memory_context="## Memory Context\nNo relevant memories.",
        instructions_hash="a" * 64,
    )
    session._input_gate_results["turn-1"] = SimpleNamespace(
        identity=identity,
        released=True,
        context_ready=True,
        hydration=hydration,
        evaluation={
            "status": "valid",
            "accepted": True,
            "value": {"label": "happy"},
        },
        visual_window={"status": "accepted", "owner": "service"},
    )

    instructions, facts = session.response_instructions("turn-1")

    assert "## Emotion\nhappy" in instructions
    assert "## Active Persona\nName: Hexis" in instructions
    assert facts["emotion_included"] is True
    assert facts["per_turn_token_count"] > 0


async def test_response_instructions_omit_invalid_label_without_blocking(monkeypatch):
    session = make_session(FakeClient([]))
    identity = {
        "session_id": str(session.session_id),
        "input_turn_id": "turn-2",
        "input_order": 1,
        "stimulus_revision": 0,
    }
    session._input_gate_results["turn-2"] = SimpleNamespace(
        identity=identity,
        released=True,
        context_ready=True,
        hydration=SimpleNamespace(
            temporal_context="2026-10-06 12:00 (Asia/Shanghai)",
            persona_context="Name: Hexis",
            continuity_context="## Conversation Continuity Packet\nNo prior turns.",
            memory_context="## Memory Context\nNo relevant memories.",
            instructions_hash="a" * 64,
        ),
        evaluation={"status": "valid", "accepted": True, "value": {"label": "Happy"}},
        visual_window={"status": "accepted", "owner": "service"},
    )

    instructions, facts = session.response_instructions("turn-2")

    assert "## Emotion" not in instructions
    assert "## Active Persona" in instructions
    assert facts["emotion_included"] is False


async def test_authorized_response_uses_server_identity_and_binds_playback_receipts():
    session = make_session(FakeClient([]))
    await session.start("stable")
    identity = {
        "session_id": str(session.session_id),
        "input_turn_id": "turn-output",
        "input_order": 2,
        "stimulus_revision": 0,
    }
    session._input_gate_results["turn-output"] = SimpleNamespace(
        identity=identity,
        released=True,
        context_ready=True,
        hydration=SimpleNamespace(
            temporal_context="2026-10-06 12:00 (Asia/Shanghai)",
            persona_context="Name: Hexis",
            continuity_context="## Conversation Continuity Packet\nNo prior turns.",
            memory_context="## Memory Context\nNo relevant memories.",
            instructions_hash="a" * 64,
        ),
        evaluation={"status": "missing"},
        visual_window={"status": "closed", "owner": "service"},
    )
    turn_id = uuid4()
    session.bind_voice_turn(
        input_turn_id="turn-output",
        input_order=2,
        user_text="authorized user request",
        context_version="a" * 64,
        turn_id=turn_id,
    )

    output = await session.start_authorized_response("turn-output")
    assert output["input_turn_id"] == "turn-output"
    assert session.authorize_voice_recall_call(
        response_id=output["response_id"],
        call_id="call-1",
        name="voice_recall",
        arguments='{"query":"project decision"}',
    ) == "turn-output"
    with pytest.raises(VoiceRealtimeSessionError, match="authorized input"):
        session.authorize_voice_recall_call(
            response_id="other-response",
            call_id="call-2",
            name="voice_recall",
            arguments='{"query":"project decision"}',
        )
    session.record_output_chunk(output["response_id"], b"\x00\x00" * 6_000)

    def receipt(action, seq, **extra):
        value = {
            "action": action,
            "session_id": session.session_id,
            "response_id": output["response_id"],
            "playback_id": output["playback_id"],
                "producer_seq": seq,
                "output_epoch": output["output_epoch"],
                "media_session_id": session.media_session_id,
                "turn_id": turn_id,
                **extra,
        }
        return SimpleNamespace(**value, model_dump=lambda mode="json": {
            **value,
            "session_id": str(value["session_id"]),
        })

    opened = receipt("open", 1, start_ms=50)
    assert session.accept_playback_receipt(opened) is True
    progress = receipt("progress", 2, played_ms=250, interval_ms=250, phase="playing")
    assert session.accept_playback_receipt(progress) is True
    assert session.accept_playback_receipt(progress) is False
    with pytest.raises(VoiceRealtimeSessionError, match="scheduled PCM"):
        session.accept_playback_receipt(
            receipt("progress", 3, played_ms=501, interval_ms=250, phase="playing")
        )
    ended = receipt("end", 3, played_ms=250, scope="complete")
    assert session.accept_playback_receipt(ended) is True
    await session.mark_generation_done("turn-output", output["response_id"])
    await session.finish_response_playback(output["response_id"])
    assert session._response_claims == {}


@pytest.mark.parametrize("terminal_first", [False, True])
async def test_audio_response_can_start_next_turn_after_generation_and_playback_finish(terminal_first):
    session = make_session(FakeClient([]))
    await session.start("stable")

    def authorize(input_turn_id):
        session._input_gate_results[input_turn_id] = SimpleNamespace(
            identity={
                "session_id": str(session.session_id),
                "input_turn_id": input_turn_id,
                "input_order": 1,
                "stimulus_revision": 0,
            },
            released=True,
            context_ready=True,
            hydration=SimpleNamespace(
                temporal_context="2026-10-06 12:00 (Asia/Shanghai)",
                persona_context="Name: Hexis",
                continuity_context="## Conversation Continuity Packet\nNo prior turns.",
                memory_context="## Memory Context\nNo relevant memories.",
                instructions_hash="a" * 64,
            ),
            evaluation={"status": "missing"},
            visual_window={"status": "closed", "owner": "service"},
        )

    authorize("turn-1")
    first = await session.start_authorized_response("turn-1")
    session.record_output_chunk(first["response_id"], b"\x00\x00" * 2400)
    receipt = SimpleNamespace(
        action="open",
        session_id=session.session_id,
        response_id=first["response_id"],
        playback_id=first["playback_id"],
        producer_seq=1,
        output_epoch=first["output_epoch"],
        media_session_id=session.media_session_id,
        turn_id=None,
        start_ms=10,
        model_dump=lambda mode="json": {},
    )
    assert session.accept_playback_receipt(receipt)
    ended = SimpleNamespace(
        action="end",
        session_id=session.session_id,
        response_id=first["response_id"],
        playback_id=first["playback_id"],
        producer_seq=2,
        output_epoch=first["output_epoch"],
        media_session_id=session.media_session_id,
        turn_id=None,
        played_ms=100,
        model_dump=lambda mode="json": {},
    )
    if terminal_first:
        assert session.accept_playback_receipt(ended)
        await session.finish_response_playback(first["response_id"])
        assert not session._responses_idle.is_set()
        await session.mark_generation_done("turn-1", first["response_id"])
    else:
        await session.mark_generation_done("turn-1", first["response_id"])
        assert not session._responses_idle.is_set()
        assert session.accept_playback_receipt(ended)
        await session.finish_response_playback(first["response_id"])

    assert session._responses_idle.is_set()
    authorize("turn-2")
    second = await asyncio.wait_for(session.start_authorized_response("turn-2"), timeout=1)
    assert second["input_turn_id"] == "turn-2"


async def test_voice_recall_followup_requires_the_exact_authorized_call_once():
    client = FakeClient([])
    session = make_session(client)
    await session.start("stable")
    input_turn_id = "turn-recall"
    session._input_gate_results[input_turn_id] = SimpleNamespace(
        identity={
            "session_id": str(session.session_id),
            "input_turn_id": input_turn_id,
            "input_order": 1,
            "stimulus_revision": 0,
        },
        released=True,
        context_ready=True,
        hydration=SimpleNamespace(
            temporal_context="2026-10-06 12:00 (Asia/Shanghai)",
            persona_context="Name: Hexis",
            continuity_context="## Conversation Continuity Packet\nNo prior turns.",
            memory_context="## Memory Context\nNo relevant memories.",
            instructions_hash="a" * 64,
        ),
        evaluation={"status": "missing"},
        visual_window={"status": "closed", "owner": "service"},
    )
    session.bind_voice_turn(
        input_turn_id=input_turn_id,
        input_order=1,
        user_text="authorized user request",
        context_version="a" * 64,
        turn_id=uuid4(),
    )
    output = await session.start_authorized_response(input_turn_id)
    session.authorize_voice_recall_call(
        response_id=output["response_id"],
        call_id="call-1",
        name="voice_recall",
        arguments='{"query":"topic"}',
    )
    await session.release_response(output["response_id"])

    with pytest.raises(VoiceRealtimeSessionError, match="authorized call"):
        await session.start_voice_recall_followup(
            input_turn_id=input_turn_id,
            call_id="unbound-call",
            tool_output='{"status":"no_results"}',
        )
    followup = await session.start_voice_recall_followup(
        input_turn_id=input_turn_id,
        call_id="call-1",
        tool_output='{"status":"no_results"}',
    )
    assert client.function_outputs == [
        ("call-1", '{"status":"no_results"}')
    ]
    assert followup["input_turn_id"] == input_turn_id

    await session.release_response(followup["response_id"])
    with pytest.raises(VoiceRealtimeSessionError, match="authorized call"):
        await session.start_voice_recall_followup(
            input_turn_id=input_turn_id,
            call_id="call-1",
            tool_output='{"status":"no_results"}',
        )


async def test_process_input_binds_cloud_commit_final_and_authorization_once():
    source = CaptureRange(
        session_id=uuid4(),
        media_session_id="media-1",
        capture_instance_id=uuid4(),
        start_frame=0,
        end_frame=320,
        start_monotonic_ms=100,
        end_monotonic_ms=120,
        audio_sha256="a" * 64,
    )
    session_id = source.session_id
    client = FakeClient(
        [
            ProtocolEvent(type="input_audio_buffer_committed", item_id="item-1"),
            ProtocolEvent(
                type="input_transcript_final",
                item_id="item-1",
                transcript="请继续",
                is_final=True,
            ),
        ]
    )
    session = VoiceRealtimeSession(
        session_id=session_id, media_session_id="media-1", client=client
    )
    await session.start("稳定层")
    receiver = asyncio.create_task(session.run_receiver())
    authorized = []

    receipt = await session.process_utterance(
        source,
        on_authorized=lambda value: _append(authorized, value),
    )
    await receiver

    assert receipt.transcript == "请继续"
    assert receipt.deduplicated is False
    assert authorized == [receipt]
    assert client.commit_audio_calls == 1


async def test_empty_cloud_transcript_is_classified_as_retryable_speech_input():
    source = CaptureRange(
        session_id=uuid4(),
        media_session_id="media-1",
        capture_instance_id=uuid4(),
        start_frame=0,
        end_frame=320,
        start_monotonic_ms=100,
        end_monotonic_ms=120,
        audio_sha256="a" * 64,
    )
    client = FakeClient(
        [
            ProtocolEvent(type="input_audio_buffer_committed", item_id="item-1"),
            ProtocolEvent(
                type="input_transcript_final",
                item_id="item-1",
                transcript=" ",
                is_final=True,
            ),
        ]
    )
    session = VoiceRealtimeSession(
        session_id=source.session_id, media_session_id="media-1", client=client
    )
    await session.start("稳定层")
    receiver = asyncio.create_task(session.run_receiver())

    with pytest.raises(VoiceTranscriptUnavailable):
        await session.process_utterance(source)
    await receiver


async def _append(target, value):
    target.append(value)
