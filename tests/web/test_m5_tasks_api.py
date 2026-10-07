from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from starlette.requests import Request

import apps.hexis_api as api
from apps.hexis_api import app
from services.m5_selection_service import M5SelectionService
from services.m5_task_service import M5TaskService


pytestmark = [pytest.mark.asyncio(loop_scope="session")]


async def test_cancelled_sse_returns_its_database_connection(db_pool):
    """复现Starlette取消作用域中UNLISTEN打断后连接未归还。"""
    import anyio
    import asyncpg
    from tests.utils import _db_dsn

    origin = await _origin(db_pool, "cancelled-stream")
    pool = await asyncpg.create_pool(_db_dsn(), min_size=1, max_size=1)
    iterator = api._stream_m5_task_events(pool, origin, 0)
    try:
        assert "m5-connected" in await anext(iterator)
        with anyio.CancelScope() as scope:
            scope.cancel()
            await iterator.aclose()
        async with pool.acquire(timeout=0.3) as conn:
            assert await conn.fetchval("SELECT 1") == 1
    finally:
        pool.terminate()


async def _origin(db_pool, label):
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT (get_or_create_chat_session(NULL,'chat',$1,'{}'::jsonb)->>'session_id')::uuid",
            f"m5-task-api-{label}-{uuid4()}",
        )


async def _submit(db_pool, origin, label):
    return await M5TaskService(db_pool).submit_m5_task(
        origin,
        uuid4(),
        f"Task {label}",
        f"Original request {label}",
        {
            "origin_session_id": str(origin),
            "input_turn_id": str(uuid4()),
            "input_order": 1,
            "stimulus_revision": 0,
            "response_id": f"response-{label}-{uuid4()}",
            "supplier_response_id": f"supplier-{label}",
            "request_id": f"request-{label}-{uuid4()}",
            "actual_call_id": f"call-{label}-{uuid4()}",
        },
    )


async def _get_json(db_pool, path):
    previous_pool = api._pool
    api._pool = db_pool
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.get(path)
    finally:
        api._pool = previous_pool


async def _post_json(db_pool, path, body):
    previous_pool = api._pool
    api._pool = db_pool
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.post(path, json=body)
    finally:
        api._pool = previous_pool


async def _begin_selection(db_pool, origin, message):
    return await M5SelectionService(db_pool).begin_pend_message(
        origin_session_id=origin,
        command_id=uuid4(),
        authorization={
            "origin_session_id": str(origin),
            "input_turn_id": str(uuid4()),
            "input_order": 2,
            "stimulus_revision": 0,
            "response_id": f"selection-response-{uuid4()}",
            "supplier_response_id": f"selection-supplier-{uuid4()}",
            "request_id": f"selection-event-{uuid4()}",
            "actual_call_id": f"selection-call-{uuid4()}",
        },
        message=message,
    )


async def test_task_snapshot_is_scoped_and_returns_matching_event_cursor(db_pool):
    first_origin = await _origin(db_pool, "first")
    other_origin = await _origin(db_pool, "other")
    first = await _submit(db_pool, first_origin, "first")
    await _submit(db_pool, other_origin, "other")

    response = await _get_json(
        db_pool, f"/api/m5/tasks?origin_session_id={first_origin}"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["origin_session_id"] == str(first_origin)
    assert body["event_cursor"] == {"m5-tasks": 1}
    assert body["tasks"] == [
        {
            "task_id": str(first["task_id"]),
            "task_title": "Task first",
            "task_state": "working",
            "task_session_id": str(first["task_session_id"]),
            "revision": 1,
            "pending": [],
        }
    ]


async def test_snapshot_projects_open_selection_and_cursor_from_the_bound_origin(db_pool):
    origin = await _origin(db_pool, "selection-snapshot")
    task = await _submit(db_pool, origin, "selection-snapshot")
    selection = await _begin_selection(db_pool, origin, "Continue with the next question.")

    response = await _get_json(
        db_pool, f"/api/m5/tasks?origin_session_id={origin}"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["event_cursor"] == {"m5-tasks": 2}
    assert body["selections"] == [
        {
            "selection_id": selection["selection_id"],
            "version": 1,
            "message": "Continue with the next question.",
        }
    ]
    assert body["tasks"][0]["task_id"] == task["task_id"]


async def test_confirm_and_cancel_selection_routes_keep_origin_server_bound(db_pool):
    confirm_origin = await _origin(db_pool, "selection-confirm-route")
    confirm_task = await _submit(db_pool, confirm_origin, "selection-confirm-route")
    confirmation = await _begin_selection(db_pool, confirm_origin, "Append this request.")

    confirmed = await _post_json(
        db_pool,
        f"/api/m5/selections/{confirmation['selection_id']}/confirm",
        {"version": confirmation["version"], "task_id": confirm_task["task_id"]},
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["accepted"] is True
    assert confirmed.json()["task_id"] == confirm_task["task_id"]

    cancel_origin = await _origin(db_pool, "selection-cancel-route")
    cancellation = await _begin_selection(db_pool, cancel_origin, "Do not enqueue this request.")
    cancelled = await _post_json(
        db_pool,
        f"/api/m5/selections/{cancellation['selection_id']}/cancel",
        {"version": cancellation["version"]},
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "selection_cancelled"
    async with db_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM m5_pending_messages WHERE command_id=$1::uuid",
            cancellation["command_id"],
        ) == 0


async def test_task_snapshot_validates_origin_and_reports_inactive_source(db_pool):
    missing = await _get_json(db_pool, f"/api/m5/tasks?origin_session_id={uuid4()}")
    invalid = await _get_json(db_pool, "/api/m5/tasks?origin_session_id=not-a-uuid")

    assert missing.status_code == 404
    assert invalid.status_code == 422


async def test_task_event_stream_replays_durable_events_from_the_requested_cursor(db_pool):
    origin = await _origin(db_pool, "replay")
    task = await _submit(db_pool, origin, "replay")
    previous_pool = api._pool
    api._pool = db_pool
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/m5/tasks/events",
            "raw_path": b"/api/m5/tasks/events",
            "query_string": f"origin_session_id={origin}&after_seq=0".encode(),
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )
    try:
        response = await api.m5_task_events(request, origin)
        assert response.status_code == 200
        iterator = response.body_iterator
        connected = await anext(iterator)
        replayed = await anext(iterator)
        assert "event: m5-connected" in connected
        assert "id: 1\nevent: m5-event" in replayed
        assert str(task["task_id"]) in replayed
    finally:
        body = locals().get("iterator")
        if body is not None:
            await body.aclose()
        api._pool = previous_pool


async def test_task_event_notification_wakes_the_durable_stream_reader(db_pool):
    origin = await _origin(db_pool, "live-notify")
    previous_pool = api._pool
    api._pool = db_pool
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/m5/tasks/events",
            "raw_path": b"/api/m5/tasks/events",
            "query_string": b"after_seq=0",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )
    try:
        response = await api.m5_task_events(request, origin)
        iterator = response.body_iterator
        connected = await anext(iterator)
        assert "event: m5-connected" in connected
        task = await _submit(db_pool, origin, "live-notify")
        update = await asyncio.wait_for(anext(iterator), timeout=2)
        assert "id: 1\nevent: m5-event" in update
        assert str(task["task_id"]) in update
    finally:
        body = locals().get("iterator")
        if body is not None:
            await body.aclose()
        api._pool = previous_pool


async def test_task_event_stream_requests_snapshot_when_cursor_is_ahead(db_pool):
    origin = await _origin(db_pool, "cursor-ahead")
    await _submit(db_pool, origin, "cursor-ahead")
    previous_pool = api._pool
    api._pool = db_pool
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/m5/tasks/events",
            "raw_path": b"/api/m5/tasks/events",
            "query_string": f"after_seq=2".encode(),
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )
    try:
        response = await api.m5_task_events(request, origin)
        iterator = response.body_iterator
        await anext(iterator)
        reset = await anext(iterator)
        assert "event: m5-reset" in reset
        assert '"reason": "cursor_ahead"' in reset
    finally:
        body = locals().get("iterator")
        if body is not None:
            await body.aclose()
        api._pool = previous_pool


async def test_task_event_stream_detects_a_missing_first_sequence(db_pool, monkeypatch):
    from services import behavior_events

    origin = await _origin(db_pool, "cursor-gap")
    previous_pool = api._pool
    api._pool = db_pool

    async def page_with_leading_gap(_conn, **_kwargs):
        return {
            "events": [
                {
                    "event_id": str(uuid4()),
                    "event_type": "m5_task_changed",
                    "session_id": str(origin),
                    "stream_id": f"m5-tasks:{origin}",
                    "seq": 2,
                    "task_id": str(uuid4()),
                    "payload": {"origin_session_id": str(origin)},
                }
            ],
            "has_more": False,
        }

    monkeypatch.setattr(
        behavior_events, "query_behavior_events_on_connection", page_with_leading_gap
    )
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/m5/tasks/events",
            "raw_path": b"/api/m5/tasks/events",
            "query_string": b"after_seq=0",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )
    try:
        response = await api.m5_task_events(request, origin)
        iterator = response.body_iterator
        await anext(iterator)
        reset = await anext(iterator)
        assert "event: m5-reset" in reset
        assert '"reason": "sequence_gap"' in reset
        assert '"after_seq": 0' in reset
        assert '"before_seq": 2' in reset
    finally:
        body = locals().get("iterator")
        if body is not None:
            await body.aclose()
        api._pool = previous_pool
