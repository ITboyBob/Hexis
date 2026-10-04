from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

import apps.hexis_api as hexis_api
from services.playback_history import PlaybackHistory, PlaybackIdentity

pytestmark = pytest.mark.asyncio(loop_scope="session")

SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _body(action: str, **overrides):
    body = {
        "action": action,
        "session_id": SESSION_ID,
        "response_id": "resp-1",
        "playback_id": "play-1",
        "output_epoch": 1,
        "media_session_id": "media-1",
    }
    body.update(overrides)
    return body


@pytest.fixture
async def client(monkeypatch):
    pool = object()
    monkeypatch.setattr(hexis_api, "_pool", pool)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=hexis_api.app),
        base_url="http://test",
    ) as value:
        yield value


async def test_playback_api_dispatches_every_action(client, monkeypatch):
    recorded = []

    async def fake_open(self, *, start_ms):
        recorded.append(("open", start_ms))
        return {"event_id": "1", "seq": 1, "deduplicated": False}

    async def fake_audible(self, *, audible_ms):
        recorded.append(("audible", audible_ms))
        return {"event_id": "2", "seq": 2, "deduplicated": False}

    async def fake_progress(self, *, played_ms, interval_ms, phase):
        recorded.append(("progress", played_ms, interval_ms, phase))
        return {"event_id": "3", "seq": 3, "deduplicated": False}

    async def fake_end(self, *, scope, played_ms):
        recorded.append(("end", scope, played_ms))
        return {"event_id": "4", "seq": 4, "deduplicated": False}

    async def fake_interrupt(self, *, reason, played_ms):
        recorded.append(("interrupt", reason, played_ms))
        return {"event_id": "5", "seq": 5, "deduplicated": False}

    monkeypatch.setattr(PlaybackHistory, "open", fake_open)
    monkeypatch.setattr(PlaybackHistory, "audible", fake_audible)
    monkeypatch.setattr(PlaybackHistory, "progress", fake_progress)
    monkeypatch.setattr(PlaybackHistory, "end", fake_end)
    monkeypatch.setattr(PlaybackHistory, "interrupt", fake_interrupt)

    responses = [
        await client.post("/api/voice/playback", json=_body("open", start_ms=100)),
        await client.post("/api/voice/playback", json=_body("audible", audible_ms=150)),
        await client.post(
            "/api/voice/playback",
            json=_body("progress", played_ms=500, interval_ms=100, phase="playing"),
        ),
        await client.post(
            "/api/voice/playback", json=_body("end", scope="partial", played_ms=900)
        ),
        await client.post(
            "/api/voice/playback",
            json=_body("interrupt", reason="user-stop", played_ms=700),
        ),
    ]

    assert [response.status_code for response in responses] == [200] * 5
    assert recorded == [
        ("open", 100),
        ("audible", 150),
        ("progress", 500, 100, "playing"),
        ("end", "partial", 900),
        ("interrupt", "user-stop", 700),
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
                    output_epoch=identity.output_epoch,
                    media_session_id=identity.media_session_id,
                    scope="partial",
                    played_ms=900,
                ),
            ),
        ]

    assert [response.status_code for response in responses] == [200, 200, 200]
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
