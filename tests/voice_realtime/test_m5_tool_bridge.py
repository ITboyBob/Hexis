from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.voice_realtime_session import (
    VoiceRealtimeSession,
    VoiceRealtimeSessionError,
)


class _Client:
    def __init__(self):
        self.function_outputs = []
        self.responses = []

    async def connect(self, _instructions, *, tools=None):
        self.tools = tools
        return "qwen-session"

    async def send_function_call_output(self, call_id, output):
        self.function_outputs.append((call_id, output))

    async def update_session_instructions(self, instructions):
        self.instructions = instructions

    async def create_first_response(self, response_id):
        self.responses.append(response_id)


async def _authorized_session(*, tool_surface="m5", input_order=4, revision=2):
    from services.voice_instructions import load_m5_instructions

    client = _Client()
    session = VoiceRealtimeSession(
        session_id=uuid4(),
        media_session_id="media-1",
        client=client,
    )
    stable = load_m5_instructions().stable if tool_surface == "m5" else "M4 stable"
    await session.start(stable, tool_surface=tool_surface)
    input_turn_id = str(uuid4())
    response_id = "response-authorized"
    identity = {
        "session_id": str(session.session_id),
        "input_turn_id": input_turn_id,
        "input_order": input_order,
        "stimulus_revision": revision,
    }
    session._input_gate_results[input_turn_id] = SimpleNamespace(
        identity=identity,
        released=True,
        context_ready=True,
        hydration=SimpleNamespace(instructions_hash="a" * 64),
    )
    session._authorized_input_ids.add(input_turn_id)
    session._response_input_ids[response_id] = input_turn_id
    session._supplier_response_map["provider-response-authorized"] = response_id
    session._voice_turns[input_turn_id] = {
        "turn_id": uuid4(),
        "input_order": input_order,
        "stimulus_revision": revision,
        "response_ids": [],
        "playback_ids": [],
    }
    return session, client, input_turn_id, response_id


@pytest.mark.asyncio
async def test_multiple_calls_bind_actual_ids_to_one_authorized_voice_input():
    session, _client, input_turn_id, response_id = await _authorized_session()
    from services.m5_tool_bridge import bind_tool_event
    from services.qwen_realtime_protocol import ProtocolEvent

    pending = {}
    first_event = ProtocolEvent(
        type="response.function_call_arguments.done",
        event_id="event-1",
        response_id="provider-response-authorized",
        call_id="call-submit-1",
        tool_name="submit",
        tool_arguments='{"task_description":"  create a plan  "}',
    )
    second_event = ProtocolEvent(
        type="response.function_call_arguments.done",
        event_id="event-2",
        response_id="provider-response-authorized",
        call_id="call-remember-1",
        tool_name="remember",
        tool_arguments='{"content":"keep this fact"}',
    )
    first = bind_tool_event(
        session, pending, expected_response_id=response_id, event=first_event
    )
    second = bind_tool_event(
        session, pending, expected_response_id=response_id, event=second_event
    )

    assert list(pending) == ["call-submit-1", "call-remember-1"]
    assert first.actual_call_id == "call-submit-1"
    assert first.raw_arguments == '{"task_description":"  create a plan  "}'
    assert first.origin_session_id == session.session_id
    assert first.input_turn_id == input_turn_id
    assert first.input_order == 4
    assert first.stimulus_revision == 2
    assert first.response_id == response_id
    assert first.supplier_response_id == "provider-response-authorized"
    assert first.request_id == "event-1"
    assert second.actual_call_id == "call-remember-1"
    assert bind_tool_event(
        session, pending, expected_response_id=response_id, event=first_event
    ) is first

    with pytest.raises(ValueError, match="response id"):
        bind_tool_event(
            session,
            {},
            expected_response_id=response_id,
            event=ProtocolEvent(
                type="response.function_call_arguments.done",
                response_id="stale-response",
                call_id="stale-event-call",
                tool_name="submit",
                tool_arguments='{"task_description":"stale"}',
            ),
        )


def test_protocol_preserves_request_response_and_actual_call_ids():
    from services.qwen_realtime_protocol import parse_event

    event = parse_event(
        {
            "type": "response.function_call_arguments.done",
            "event_id": "event-actual-1",
            "response_id": "response-actual-1",
            "call_id": "call-actual-1",
            "name": "submit",
            "arguments": '{"task_description":"new work"}',
        }
    )

    assert event.event_id == "event-actual-1"
    assert event.response_id == "response-actual-1"
    assert event.call_id == "call-actual-1"


@pytest.mark.asyncio
async def test_authorization_rejects_m4_tools_and_stale_response_or_revision():
    m4_session, _client, _input_turn_id, response_id = await _authorized_session(
        tool_surface="m4"
    )
    from services.m5_tool_bridge import execute_bound_tool

    m4_call = m4_session.authorize_voice_tool_call(
        response_id=response_id,
        request_id="event-1",
        call_id="call-submit",
        name="submit",
        arguments='{"task_description":"do work"}',
    )
    refusal = json.loads(await execute_bound_tool(m4_session, m4_call, handlers={}))
    assert refusal["tool_name"] == "submit"
    assert refusal["error"]["code"] == "TOOL_NOT_ALLOWED"
    with pytest.raises(VoiceRealtimeSessionError, match="authorized input"):
        m4_session.authorize_voice_tool_call(
            response_id="old-response",
            request_id="event-2",
            call_id="call-recall-old-response",
            name="voice_recall",
            arguments='{"query":"old response"}',
        )

    session, _client, input_turn_id, response_id = await _authorized_session()
    session._voice_turns[input_turn_id]["stimulus_revision"] = 3
    with pytest.raises(VoiceRealtimeSessionError, match="revision"):
        session.authorize_voice_tool_call(
            response_id=response_id,
            request_id="event-3",
            call_id="call-submit-3",
            name="submit",
            arguments='{"task_description":"do work"}',
        )


@pytest.mark.asyncio
async def test_recall_limit_stays_recall_specific_and_stale_output_is_rejected():
    session, _client, _input_turn_id, response_id = await _authorized_session()
    for index in range(2):
        session.authorize_voice_tool_call(
            response_id=response_id,
            request_id=f"recall-event-{index}",
            call_id=f"recall-{index}",
            name="voice_recall",
            arguments='{"query":"topic"}',
        )
    session.authorize_voice_tool_call(
        response_id=response_id,
        request_id="submit-event",
        call_id="submit-after-recalls",
        name="submit",
        arguments='{"task_description":"new task"}',
    )
    with pytest.raises(VoiceRealtimeSessionError, match="two-call input limit"):
        session.authorize_voice_tool_call(
            response_id=response_id,
            request_id="recall-event-3",
            call_id="recall-3",
            name="voice_recall",
            arguments='{"query":"topic"}',
        )

    session._playback_state[response_id] = {"invalidated": True}
    with pytest.raises(VoiceRealtimeSessionError, match="invalidated"):
        session.authorize_voice_tool_call(
            response_id=response_id,
            request_id="stale-event",
            call_id="stale-call",
            name="submit",
            arguments='{"task_description":"stale"}',
        )


@pytest.mark.asyncio
async def test_one_followup_returns_each_actual_call_id_before_one_response():
    from services.voice_realtime_session import VoiceRealtimeSession

    session, client, input_turn_id, _response_id = await _authorized_session()
    session.response_instructions = lambda _input_id: ("per-turn", {})
    session.authorize_voice_tool_call(
        response_id=_response_id,
        request_id="event-followup-1",
        call_id="actual-call-1",
        name="submit",
        arguments='{"task_description":"first"}',
    )
    session.authorize_voice_tool_call(
        response_id=_response_id,
        request_id="event-followup-2",
        call_id="actual-call-2",
        name="remember",
        arguments='{"content":"second"}',
    )

    followup = await session.start_voice_tool_followup(
        input_turn_id=input_turn_id,
        outputs=[
            ("actual-call-1", json.dumps({"state": "accepted"})),
            ("actual-call-2", json.dumps({"state": "failed"})),
        ],
    )

    assert client.function_outputs == [
        ("actual-call-1", '{"state": "accepted"}'),
        ("actual-call-2", '{"state": "failed"}'),
    ]
    assert len(client.responses) == 1
    assert followup["input_turn_id"] == input_turn_id


@pytest.mark.asyncio
async def test_missing_frontend_handler_fails_with_real_call_receipt_without_side_effect():
    from services.m5_tool_bridge import execute_bound_tool

    session, _client, _input_turn_id, response_id = await _authorized_session()
    call = session.authorize_voice_tool_call(
        response_id=response_id,
        request_id="event-unimplemented",
        call_id="actual-unimplemented-call",
        name="submit",
        arguments='{"task_description":"do work"}',
    )

    output = await execute_bound_tool(session, call, handlers={})
    receipt = json.loads(output)
    assert receipt["call_id"] == "actual-unimplemented-call"
    assert receipt["ok"] is False
    assert receipt["state"] == "failed"
    assert receipt["error"]["code"] == "TOOL_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_unknown_supplier_tool_name_is_refused_without_registering_it():
    from services.m5_tool_bridge import execute_bound_tool

    session, _client, _input_turn_id, response_id = await _authorized_session()
    call = session.authorize_voice_tool_call(
        response_id=response_id,
        request_id="event-unknown",
        call_id="actual-unknown-call",
        name="future_tool",
        arguments="{}",
    )

    receipt = json.loads(await execute_bound_tool(session, call, handlers={}))
    assert receipt["tool_name"] == "future_tool"
    assert receipt["call_id"] == "actual-unknown-call"
    assert receipt["error"]["code"] == "TOOL_NOT_ALLOWED"
