"""Long-lived realtime voice session ownership and event routing."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

from services.qwen_realtime_protocol import ProtocolEvent


class VoiceRealtimeSessionError(RuntimeError):
    """The realtime session violated a readiness or routing invariant."""


class ActiveVoiceSessionExists(RuntimeError):
    """A chat already has one active realtime media session."""


class VoiceRealtimeSession:
    """One server-owned cloud session with a single receive dispatcher."""

    def __init__(
        self,
        *,
        session_id: UUID,
        media_session_id: str,
        client: Any,
        queue_limit: int = 1000,
    ) -> None:
        if not isinstance(session_id, UUID):
            raise ValueError("session_id must be a UUID")
        if not isinstance(media_session_id, str) or not media_session_id.strip():
            raise ValueError("media_session_id must be a non-empty string")
        if queue_limit <= 0:
            raise ValueError("queue_limit must be positive")
        self.session_id = session_id
        self.media_session_id = media_session_id
        self.client = client
        self.queue_limit = queue_limit
        self.cloud_session_id: str | None = None
        self._ready = False
        self._closed = False
        self._committed_event: ProtocolEvent | None = None
        self._committed_future: asyncio.Future[ProtocolEvent] | None = None
        self._final_events: dict[str, ProtocolEvent] = {}
        self._final_futures: dict[str, asyncio.Future[ProtocolEvent]] = {}
        self._response_queues: dict[str, asyncio.Queue[ProtocolEvent]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()

    @property
    def ready(self) -> bool:
        return self._ready and not self._closed

    async def start(self, instructions: str) -> str:
        if self._closed:
            raise VoiceRealtimeSessionError("realtime session is closed")
        if self._ready:
            raise VoiceRealtimeSessionError("realtime session already started")
        cloud_session_id = await self.client.connect(instructions)
        if not isinstance(cloud_session_id, str) or not cloud_session_id.strip():
            raise VoiceRealtimeSessionError(
                "session.updated did not provide a usable cloud session id"
            )
        self.cloud_session_id = cloud_session_id
        self._ready = True
        return cloud_session_id

    def subscribe_response(self, response_id: str) -> asyncio.Queue[ProtocolEvent]:
        if not isinstance(response_id, str) or not response_id.strip():
            raise ValueError("response_id must be a non-empty string")
        if response_id in self._response_queues:
            raise VoiceRealtimeSessionError(
                f"response {response_id} already has a subscriber"
            )
        queue: asyncio.Queue[ProtocolEvent] = asyncio.Queue(maxsize=self.queue_limit)
        self._response_queues[response_id] = queue
        return queue

    async def wait_for_input_committed(self) -> ProtocolEvent:
        if self._committed_event is not None:
            return self._committed_event
        if self._committed_future is None:
            self._committed_future = asyncio.get_running_loop().create_future()
        return await self._committed_future

    async def wait_for_final(self, item_id: str) -> ProtocolEvent:
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError("item_id must be a non-empty string")
        if item_id in self._final_events:
            return self._final_events[item_id]
        if item_id not in self._final_futures:
            self._final_futures[item_id] = asyncio.get_running_loop().create_future()
        return await self._final_futures[item_id]

    async def run_receiver(self) -> None:
        if not self._ready:
            raise VoiceRealtimeSessionError("realtime session is not ready")
        async for event in self.client.events():
            self._route(event)

    def register_background_task(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def close(self, reason: str = "manual") -> None:
        del reason
        self._closed = True
        self._ready = False
        await self.client.close()

    def _route(self, event: ProtocolEvent) -> None:
        if event.type == "input_audio_buffer_committed":
            self._committed_event = event
            if self._committed_future is not None and not self._committed_future.done():
                self._committed_future.set_result(event)

        if event.type == "input_transcript_final" and event.item_id:
            self._final_events[event.item_id] = event
            future = self._final_futures.get(event.item_id)
            if future is not None and not future.done():
                future.set_result(event)

        if event.response_id:
            queue = self._response_queues.get(event.response_id)
            if queue is not None:
                if queue.full():
                    raise VoiceRealtimeSessionError(
                        f"response {event.response_id} queue is overloaded"
                    )
                queue.put_nowait(event)


class VoiceRealtimeSessionRegistry:
    """Enforce one active media session per chat in this server process."""

    def __init__(self) -> None:
        self._sessions: dict[UUID, VoiceRealtimeSession] = {}
        self._by_media_session_id: dict[str, VoiceRealtimeSession] = {}

    def register(
        self, session_id: UUID, session: VoiceRealtimeSession
    ) -> VoiceRealtimeSession:
        existing = self._sessions.get(session_id)
        if existing is not None:
            raise ActiveVoiceSessionExists(
                f"chat {session_id} already has active realtime session "
                f"{existing.media_session_id}"
            )
        self._sessions[session_id] = session
        self._by_media_session_id[session.media_session_id] = session
        return session

    def remove(
        self, session_id: UUID, session: VoiceRealtimeSession
    ) -> VoiceRealtimeSession:
        existing = self._sessions.get(session_id)
        if existing is not session:
            raise VoiceRealtimeSessionError(
                f"chat {session_id} does not own realtime session {session.media_session_id}"
            )
        del self._sessions[session_id]
        del self._by_media_session_id[session.media_session_id]
        return session

    def get(self, session_id: UUID) -> VoiceRealtimeSession | None:
        return self._sessions.get(session_id)

    def get_by_media_session_id(self, media_session_id: str) -> VoiceRealtimeSession | None:
        return self._by_media_session_id.get(media_session_id)


def new_media_session_id() -> str:
    return f"media-{uuid4()}"


__all__ = [
    "ActiveVoiceSessionExists",
    "VoiceRealtimeSession",
    "VoiceRealtimeSessionError",
    "VoiceRealtimeSessionRegistry",
    "new_media_session_id",
]
