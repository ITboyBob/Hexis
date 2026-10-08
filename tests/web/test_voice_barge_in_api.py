from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

import apps.hexis_api as hexis_api
from services.behavior_events import query_behavior_events
from services.voice_realtime_session import VoiceRealtimeSession, VoiceRealtimeSessionError
from services.voice_realtime_transport import (
    VoiceClientTransport,
    build_barge_in_capability,
    load_playback_coordination_config,
    parse_client_message,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _Client:
    def __init__(self) -> None:
        self.cancel_calls = 0

    async def cancel_response(self) -> None:
        self.cancel_calls += 1


def _confirmed(
    session: VoiceRealtimeSession,
    capture_id,
    *,
    producer_seq: int,
    command_id: str,
    local_playback_state: str,
    receipt_prefix: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "type": "barge_in_confirmed",
        "session_id": str(session.session_id),
        "media_session_id": session.media_session_id,
        "producer_seq": producer_seq,
        "command_id": command_id,
        "capture_instance_id": str(capture_id),
        "response_id": "response-a",
        "output_epoch": 1,
        "playback_id": "playback-a",
        "local_playback_state": local_playback_state,
        "receipt_prefix": receipt_prefix or [],
    }


def _receipt(session: VoiceRealtimeSession, *, action: str, producer_seq: int, **fields):
    return {
        "action": action,
        "session_id": str(session.session_id),
        "response_id": "response-a",
        "playback_id": "playback-a",
        "producer_seq": producer_seq,
        "output_epoch": 1,
        "media_session_id": session.media_session_id,
        **fields,
    }


async def _active_session(db_pool, monkeypatch):
    monkeypatch.setattr(hexis_api, "_pool", db_pool)
    session_id = uuid4()
    media_session_id = f"media-{uuid4()}"
    config = load_playback_coordination_config()
    client = _Client()
    session = VoiceRealtimeSession(
        session_id=session_id,
        media_session_id=media_session_id,
        client=client,
        barge_in_capability=build_barge_in_capability(config, supported=True),
        playback_coordination_config=config,
    )
    claim = await session._output_arbiter.claim("response-a", media_session_id)
    session._response_claims["response-a"] = claim
    session._response_queues["response-a"] = asyncio.Queue()
    session._responses_idle.clear()
    session._playback_ids["response-a"] = "playback-a"
    session._playback_state["response-a"] = {
        "output_epoch": 1,
        "playback_id": "playback-a",
        "scheduled_frames": 4800,
        "scheduled_ms": 200,
        "opened": False,
        "terminal": False,
        "generation_done": False,
        "generation_done_event": asyncio.Event(),
        "last_seq": 0,
        "receipts": {},
        "played_ms": 0,
        "invalidated": False,
    }
    transport = VoiceClientTransport(
        session_id=session_id,
        media_session_id=media_session_id,
        max_receipt_prefix_items=config.maximum_receipt_prefix_items,
    )
    capture_id = uuid4()
    transport.accept(parse_client_message({
        "schema_version": 1,
        "type": "capture_open",
        "session_id": str(session_id),
        "media_session_id": media_session_id,
        "producer_seq": 0,
        "capture_instance_id": str(capture_id),
        "sample_rate": 16000,
        "channels": 1,
        "sample_format": "s16le",
    }))
    return session, transport, capture_id, client


async def test_confirm_persists_exact_prefix_cancels_once_and_caches_semantic_result(db_pool, monkeypatch):
    session, transport, capture_id, client = await _active_session(db_pool, monkeypatch)
    prefix = [
        _receipt(session, action="open", producer_seq=1, start_ms=12),
        _receipt(session, action="interrupt", producer_seq=2, played_ms=32, reason="barge_in"),
    ]
    raw = _confirmed(
        session,
        capture_id,
        producer_seq=1,
        command_id=str(uuid4()),
        local_playback_state="started",
        receipt_prefix=prefix,
    )
    message = parse_client_message(raw)
    transport.accept(message, raw_message=raw)

    result, error = await hexis_api._confirm_voice_barge_in(session, transport, message, raw)

    assert error is None
    assert result["status"] == "accepted"
    assert client.cancel_calls == 1
    assert session._playback_state["response-a"]["invalidated"] is True
    assert session._playback_state["response-a"]["persisted_receipts"] == {
        1: prefix[0],
        2: prefix[1],
    }

    retry = {**raw, "producer_seq": 2}
    retry_message = parse_client_message(retry)
    transport.accept(retry_message, raw_message=retry)
    assert await hexis_api._confirm_voice_barge_in(session, transport, retry_message, retry) == (result, None)
    assert client.cancel_calls == 1
    rows = (await query_behavior_events(db_pool, session_id=session.session_id, response_id="response-a"))["events"]
    assert len(rows) == 2

    changed = {**retry, "producer_seq": 3, "capture_instance_id": str(uuid4())}
    changed_message = parse_client_message(changed)
    transport.accept(changed_message, raw_message=changed)
    with pytest.raises(VoiceRealtimeSessionError, match="different.*content"):
        await hexis_api._confirm_voice_barge_in(session, transport, changed_message, changed)
    assert client.cancel_calls == 1


async def test_http_first_released_target_only_allows_exact_terminal_readback(db_pool, monkeypatch):
    session, transport, capture_id, client = await _active_session(db_pool, monkeypatch)
    session._playback_state["response-a"]["generation_done"] = True
    open_request = _receipt(session, action="open", producer_seq=1, start_ms=12)
    interrupt_request = _receipt(session, action="interrupt", producer_seq=2, played_ms=32, reason="barge_in")
    await hexis_api._record_voice_playback_for_session(
        session,
        hexis_api.PlaybackEventRequest.model_validate(open_request),
        open_request,
    )
    await hexis_api._record_voice_playback_for_session(
        session,
        hexis_api.PlaybackEventRequest.model_validate(interrupt_request),
        interrupt_request,
    )
    assert session.playback_snapshot("response-a") is None

    raw = _confirmed(
        session,
        capture_id,
        producer_seq=1,
        command_id=str(uuid4()),
        local_playback_state="started",
        receipt_prefix=[interrupt_request],
    )
    message = parse_client_message(raw)
    transport.accept(message, raw_message=raw)
    result, error = await hexis_api._confirm_voice_barge_in(session, transport, message, raw)

    assert error is None
    assert result["status"] == "playback_only"
    assert client.cancel_calls == 0
    rows = (await query_behavior_events(db_pool, session_id=session.session_id, response_id="response-a"))["events"]
    assert len(rows) == 2
