from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import parse_qsl, urlsplit

import pytest

from services.qwen_realtime_client import (
    QwenRealtimeClient,
    QwenRealtimeConnectionError,
    QwenRealtimeSessionError,
)

pytestmark = [pytest.mark.asyncio(loop_scope="function")]


class FakeWebSocket:
    def __init__(self, incoming: list[dict]):
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        for value in incoming:
            self.incoming.put_nowait(json.dumps(value))
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True


def make_client(
    incoming: list[dict],
    *,
    connect_factory=None,
    endpoint: str = "wss://qwen.example/realtime",
):
    observed: dict = {}

    async def default_factory(endpoint, *, extra_headers):
        observed["endpoint"] = endpoint
        observed["extra_headers"] = extra_headers
        websocket = FakeWebSocket(incoming)
        observed["websocket"] = websocket
        return websocket

    return (
        QwenRealtimeClient(
            endpoint=endpoint,
            model="qwen-realtime",
            voice="Tina",
            api_key="explicit-key",
            connect_factory=connect_factory or default_factory,
        ),
        observed,
    )


async def test_connect_merges_model_into_endpoint_query():
    client, observed = make_client(
        [{"type": "session.updated", "session": {"id": "qwen-1"}}],
        endpoint="wss://qwen.example/realtime?model=stale&foo=bar&model=older",
    )

    await client.connect("稳定层")

    parsed = urlsplit(observed["endpoint"])
    assert parsed.scheme == "wss"
    assert parsed.netloc == "qwen.example"
    assert parsed.path == "/realtime"
    assert parse_qsl(parsed.query) == [("foo", "bar"), ("model", "qwen-realtime")]
    await client.close()


async def test_connect_waits_for_session_updated_and_does_not_send_response():
    client, observed = make_client(
        [{"type": "session.updated", "session": {"id": "qwen-1"}}]
    )

    session_id = await client.connect("稳定层")

    assert session_id == "qwen-1"
    assert parse_qsl(urlsplit(observed["endpoint"]).query) == [
        ("model", "qwen-realtime")
    ]
    assert observed["extra_headers"] == {"Authorization": "Bearer explicit-key"}
    assert client.websocket.sent[0] == {
        "type": "session.update",
        "session": {
            "modalities": ["text", "audio"],
            "instructions": "稳定层",
            "turn_detection": None,
            "audio": {
                "input": {
                    "format": {
                        "type": "pcm",
                        "sample_rate": 16000,
                        "sample_format": "s16le",
                        "channels": 1,
                        "packing": "interleaved",
                        "channel_layout": "mono",
                    }
                },
                "output": {
                    "voice": "Tina",
                    "format": {"type": "pcm", "sample_rate": 24000},
                },
            },
        },
    }
    assert all(
        message["type"] != "response.create" for message in client.websocket.sent
    )
    await client.close()


async def test_connect_rejects_missing_session_id():
    client, observed = make_client([{"type": "session.updated", "session": {}}])

    with pytest.raises(QwenRealtimeSessionError, match="session id"):
        await client.connect("稳定层")

    assert all(
        message["type"] != "response.create" for message in observed["websocket"].sent
    )
    await client.close()


async def test_audio_methods_send_facts_without_authorization():
    client, _ = make_client([{"type": "session.updated", "session": {"id": "qwen-1"}}])
    await client.connect("稳定层")
    client.websocket.sent.clear()

    await client.append_audio("base64-audio")
    await client.commit_audio()

    assert client.websocket.sent == [
        {
            "type": "input_audio_buffer.append",
            "audio": "base64-audio",
        },
        {"type": "input_audio_buffer.commit"},
    ]
    assert all(
        message["type"] != "input_committed" for message in client.websocket.sent
    )
    await client.close()


async def test_wait_for_final_ignores_partial():
    client, _ = make_client(
        [
            {"type": "session.updated", "session": {"id": "qwen-1"}},
            {
                "type": "conversation.item.input_audio_transcription.partial",
                "item_id": "item-1",
                "transcript": "请",
            },
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "item-1",
                "transcript": "请继续",
                "is_final": True,
            },
        ]
    )
    await client.connect("稳定层")

    event = await client.wait_for_final()

    assert event.type == "input_transcript_final"
    assert event.item_id == "item-1"
    assert event.transcript == "请继续"
    assert event.is_final is True
    await client.close()


async def test_create_first_response_requires_explicit_call():
    client, _ = make_client([{"type": "session.updated", "session": {"id": "qwen-1"}}])
    await client.connect("稳定层")
    assert all(
        message["type"] != "response.create" for message in client.websocket.sent
    )

    await client.create_first_response("resp-1", "每轮上下文")

    assert client.websocket.sent[-1] == {
        "type": "response.create",
        "response": {"id": "resp-1", "instructions": "每轮上下文"},
    }
    await client.close()


async def test_unknown_event_is_logged_and_connection_continues(caplog):
    client, _ = make_client(
        [
            {"type": "vendor.new.event"},
            {"type": "session.updated", "session": {"id": "qwen-1"}},
        ]
    )

    with caplog.at_level(logging.WARNING, logger="services.qwen_realtime_client"):
        session_id = await client.connect("稳定层")

    assert session_id == "qwen-1"
    assert any("vendor.new.event" in record.message for record in caplog.records)
    await client.close()


async def test_close_cancels_local_task_and_closes_websocket():
    client, _ = make_client([{"type": "session.updated", "session": {"id": "qwen-1"}}])
    await client.connect("稳定层")
    task = client._receive_task
    websocket = client.websocket

    await client.close()

    assert task.cancelled()
    assert websocket.closed


async def test_connection_failure_preserves_structured_error():
    failure = OSError("cloud unavailable")

    async def failing_factory(endpoint, *, extra_headers):
        raise failure

    client, _ = make_client([], connect_factory=failing_factory)

    with pytest.raises(QwenRealtimeConnectionError) as error:
        await client.connect("稳定层")

    assert error.value.endpoint == "wss://qwen.example/realtime"
    assert error.value.__cause__ is failure
