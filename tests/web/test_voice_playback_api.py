from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import httpx
import pytest

import apps.hexis_api as hexis_api
from services.behavior_events import query_behavior_events
from services.playback_history import PlaybackHistory, PlaybackIdentity
from services.voice_realtime_session import VoiceRealtimeSession, VoiceRealtimeSessionRegistry

pytestmark = pytest.mark.asyncio(loop_scope="session")

SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _body(action: str, producer_seq: int = 1, **overrides):
    body = {
        "action": action,
        "session_id": SESSION_ID,
        "response_id": "resp-1",
        "playback_id": "play-1",
        "producer_seq": producer_seq,
        "output_epoch": 1,
        "media_session_id": "media-1",
    }
    body.update(overrides)
    return body


def _register_playback_session(monkeypatch, identity: PlaybackIdentity):
    class Client:
        async def close(self):
            return None

    registry = VoiceRealtimeSessionRegistry()
    session = VoiceRealtimeSession(
        session_id=identity.session_id,
        media_session_id=identity.media_session_id,
        client=Client(),
    )
    session._playback_ids[identity.response_id] = identity.playback_id
    session._playback_state[identity.response_id] = {
        "output_epoch": identity.output_epoch,
        "playback_id": identity.playback_id,
        "scheduled_frames": 480_000,
        "scheduled_ms": 10_000,
        "opened": False,
        "terminal": False,
        "last_seq": 0,
        "receipts": {},
        "played_ms": 0,
        "invalidated": False,
    }
    registry.register(identity.session_id, session)
    monkeypatch.setattr(hexis_api, "_voice_realtime_registry", registry)
    return session


@pytest.fixture
async def client(monkeypatch):
    pool = object()
    monkeypatch.setattr(hexis_api, "_pool", pool)
    identity = PlaybackIdentity(
        session_id=UUID(SESSION_ID),
        response_id="resp-1",
        playback_id="play-1",
        output_epoch=1,
        media_session_id="media-1",
    )
    session = _register_playback_session(monkeypatch, identity)
    session.accept_playback_receipt = lambda _request: True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=hexis_api.app),
        base_url="http://test",
    ) as value:
        yield value


async def test_playback_api_dispatches_every_action(client, monkeypatch):
    recorded = []

    async def fake_open(self, *, start_ms, producer_seq):
        recorded.append(("open", start_ms, producer_seq))
        return {"event_id": "1", "seq": 1, "deduplicated": False}

    async def fake_audible(self, *, audible_ms, producer_seq):
        recorded.append(("audible", audible_ms, producer_seq))
        return {"event_id": "2", "seq": 2, "deduplicated": False}

    async def fake_progress(self, *, played_ms, interval_ms, phase, producer_seq):
        recorded.append(("progress", played_ms, interval_ms, phase, producer_seq))
        return {"event_id": "3", "seq": 3, "deduplicated": False}

    async def fake_end(self, *, scope, played_ms, producer_seq):
        recorded.append(("end", scope, played_ms, producer_seq))
        return {"event_id": "4", "seq": 4, "deduplicated": False}

    async def fake_interrupt(self, *, reason, played_ms, producer_seq):
        recorded.append(("interrupt", reason, played_ms, producer_seq))
        return {"event_id": "5", "seq": 5, "deduplicated": False}

    monkeypatch.setattr(PlaybackHistory, "open", fake_open)
    monkeypatch.setattr(PlaybackHistory, "audible", fake_audible)
    monkeypatch.setattr(PlaybackHistory, "progress", fake_progress)
    monkeypatch.setattr(PlaybackHistory, "end", fake_end)
    monkeypatch.setattr(PlaybackHistory, "interrupt", fake_interrupt)

    responses = [
        await client.post(
            "/api/voice/playback", json=_body("open", producer_seq=1, start_ms=100)
        ),
        await client.post(
            "/api/voice/playback",
            json=_body("audible", producer_seq=2, audible_ms=150),
        ),
        await client.post(
            "/api/voice/playback",
            json=_body(
                "progress",
                producer_seq=3,
                played_ms=500,
                interval_ms=100,
                phase="playing",
            ),
        ),
        await client.post(
            "/api/voice/playback",
            json=_body("end", producer_seq=4, scope="partial", played_ms=900),
        ),
        await client.post(
            "/api/voice/playback",
            json=_body(
                "interrupt", producer_seq=5, reason="user-stop", played_ms=700
            ),
        ),
    ]

    assert [response.status_code for response in responses] == [200] * 5
    assert recorded == [
        ("open", 100, 1),
        ("audible", 150, 2),
        ("progress", 500, 100, "playing", 3),
        ("end", "partial", 900, 4),
        ("interrupt", "user-stop", 700, 5),
    ]


@pytest.mark.parametrize(
    ("action", "overrides"),
    [
        ("open", {}),
        ("audible", {}),
        ("progress", {"interval_ms": 100, "phase": "playing"}),
        ("progress", {"played_ms": 100, "phase": "playing"}),
        ("progress", {"played_ms": 100, "interval_ms": 100}),
        ("end", {"played_ms": 100}),
        ("end", {"scope": "partial"}),
        ("interrupt", {"played_ms": 100}),
        ("interrupt", {"reason": "user-stop"}),
    ],
)
async def test_playback_api_requires_action_fields(client, action, overrides):
    response = await client.post("/api/voice/playback", json=_body(action, **overrides))

    assert response.status_code == 422


async def test_playback_api_rejects_unknown_action_and_extra_fields(client):
    unknown = await client.post("/api/voice/playback", json=_body("unknown"))
    extra = await client.post(
        "/api/voice/playback", json=_body("open", start_ms=0, unexpected=True)
    )

    assert unknown.status_code == 422
    assert extra.status_code == 422


async def test_playback_api_requires_a_nonnegative_producer_seq(client):
    missing_body = _body("open", start_ms=0)
    missing_body.pop("producer_seq")
    missing = await client.post("/api/voice/playback", json=missing_body)
    negative = await client.post(
        "/api/voice/playback", json=_body("open", producer_seq=-1, start_ms=0)
    )
    non_integer = await client.post(
        "/api/voice/playback", json=_body("open", producer_seq="1", start_ms=0)
    )

    assert missing.status_code == 422
    assert negative.status_code == 422
    assert non_integer.status_code == 422


async def test_playback_api_returns_503_before_database_is_ready(monkeypatch):
    monkeypatch.setattr(hexis_api, "_pool", None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=hexis_api.app),
        base_url="http://test",
    ) as http:
        response = await http.post(
            "/api/voice/playback", json=_body("open", start_ms=0)
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "database not ready"


async def test_playback_api_records_real_events_through_shared_history(
    db_pool, monkeypatch
):
    identity = PlaybackIdentity(
        session_id=uuid4(),
        response_id=f"resp-{uuid4()}",
        playback_id=f"play-{uuid4()}",
        output_epoch=3,
        media_session_id=f"media-{uuid4()}",
    )
    monkeypatch.setattr(hexis_api, "_pool", db_pool)
    _register_playback_session(monkeypatch, identity)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=hexis_api.app),
        base_url="http://test",
    ) as http:
        responses = [
            await http.post(
                "/api/voice/playback",
                json=_body(
                    "open",
                    session_id=str(identity.session_id),
                    response_id=identity.response_id,
                    playback_id=identity.playback_id,
                    producer_seq=1,
                    output_epoch=identity.output_epoch,
                    media_session_id=identity.media_session_id,
                    start_ms=100,
                ),
            ),
            await http.post(
                "/api/voice/playback",
                json=_body(
                    "progress",
                    session_id=str(identity.session_id),
                    response_id=identity.response_id,
                    playback_id=identity.playback_id,
                    producer_seq=2,
                    output_epoch=identity.output_epoch,
                    media_session_id=identity.media_session_id,
                    played_ms=500,
                    interval_ms=100,
                    phase="playing",
                ),
            ),
            await http.post(
                "/api/voice/playback",
                json=_body(
                    "end",
                    session_id=str(identity.session_id),
                    response_id=identity.response_id,
                    playback_id=identity.playback_id,
                    producer_seq=3,
                    output_epoch=identity.output_epoch,
                    media_session_id=identity.media_session_id,
                    scope="partial",
                    played_ms=900,
                ),
            ),
        ]

    assert [response.status_code for response in responses] == [200, 200, 200]
    events = (
        await query_behavior_events(
            db_pool, session_id=identity.session_id, response_id=identity.response_id
        )
    )["events"]
    assert [event["producer_seq"] for event in sorted(
        events, key=lambda event: event["producer_seq"]
    )] == [1, 2, 3]
    result = await PlaybackHistory(db_pool, identity).get()
    assert result == {
        "playback_id": identity.playback_id,
        "response_id": identity.response_id,
        "output_epoch": identity.output_epoch,
        "media_session_id": identity.media_session_id,
        "scope": "partial",
        "played_ms": 900,
        "event_count": 3,
    }


async def test_playback_api_retry_deduplicates_with_stable_event_id(
    db_pool, monkeypatch
):
    identity = PlaybackIdentity(
        session_id=uuid4(),
        response_id=f"resp-{uuid4()}",
        playback_id=f"play-{uuid4()}",
        output_epoch=1,
        media_session_id=f"media-{uuid4()}",
    )
    monkeypatch.setattr(hexis_api, "_pool", db_pool)
    _register_playback_session(monkeypatch, identity)
    body = {
        "action": "open",
        "session_id": str(identity.session_id),
        "response_id": identity.response_id,
        "playback_id": identity.playback_id,
        "producer_seq": 1,
        "output_epoch": identity.output_epoch,
        "media_session_id": identity.media_session_id,
        "start_ms": 100,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=hexis_api.app),
        base_url="http://test",
    ) as http:
        first = await http.post("/api/voice/playback", json=body)
        retry = await http.post("/api/voice/playback", json=body)

    assert first.status_code == 200
    assert retry.status_code == 200
    assert first.json()["event_id"] == retry.json()["event_id"]
    assert first.json()["deduplicated"] is False
    assert retry.json()["deduplicated"] is True


async def test_playback_api_concurrent_requests_keep_producer_stream_order(
    db_pool, monkeypatch
):
    identity = PlaybackIdentity(
        session_id=uuid4(),
        response_id=f"resp-{uuid4()}",
        playback_id=f"play-{uuid4()}",
        output_epoch=1,
        media_session_id=f"media-{uuid4()}",
    )
    monkeypatch.setattr(hexis_api, "_pool", db_pool)
    _register_playback_session(monkeypatch, identity)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=hexis_api.app),
        base_url="http://test",
    ) as http:
        open_request = http.post(
            "/api/voice/playback",
            json=_body(
                "open",
                producer_seq=1,
                session_id=str(identity.session_id),
                response_id=identity.response_id,
                playback_id=identity.playback_id,
                output_epoch=identity.output_epoch,
                media_session_id=identity.media_session_id,
                start_ms=0,
            ),
        )
        interrupt_request = http.post(
            "/api/voice/playback",
            json=_body(
                "interrupt",
                producer_seq=2,
                session_id=str(identity.session_id),
                response_id=identity.response_id,
                playback_id=identity.playback_id,
                output_epoch=identity.output_epoch,
                media_session_id=identity.media_session_id,
                reason="user-stop",
                played_ms=500,
            ),
        )
        responses = await asyncio.gather(open_request, interrupt_request)

    assert [response.status_code for response in responses] == [200, 200]
    events = (
        await query_behavior_events(
            db_pool, session_id=identity.session_id, response_id=identity.response_id
        )
    )["events"]
    ordered = sorted(events, key=lambda event: event["producer_seq"])
    assert [event["producer_seq"] for event in ordered] == [1, 2]
    assert [event["event_type"] for event in ordered] == [
        "voice_playback_started",
        "voice_playback_interrupted",
    ]
    result = await PlaybackHistory(db_pool, identity).get()
    assert result["scope"] == "partial"
