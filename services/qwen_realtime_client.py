"""Controlled WebSocket client for the verified Qwen Realtime event subset."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from services.qwen_realtime_protocol import (
    ProtocolEvent,
    UnknownProtocolEvent,
    build_cancel_response,
    build_function_call_output,
    build_first_response,
    build_update_session,
    parse_event,
)

logger = logging.getLogger(__name__)
_MAX_AUDIO_BASE64_CHARS_PER_MESSAGE = 180_000


def _endpoint_with_model(endpoint: str, model: str) -> str:
    parsed = urlsplit(endpoint)
    query = [
        item
        for item in parse_qsl(parsed.query, keep_blank_values=True)
        if item[0] != "model"
    ]
    query.append(("model", model))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


class QwenRealtimeConnectionError(RuntimeError):
    """A structured failure while establishing or maintaining the WS connection."""

    def __init__(self, endpoint: str, message: str):
        super().__init__(message)
        self.endpoint = endpoint


class QwenRealtimeSessionError(RuntimeError):
    """The session receipt does not satisfy the readiness contract."""


class QwenRealtimeProtocolError(RuntimeError):
    """The peer sent a message outside the JSON protocol contract."""


class QwenRealtimeClient:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        voice: str,
        api_key: str,
        connect_factory: Callable[..., Awaitable[Any]],
    ):
        self.endpoint = endpoint
        self.model = model
        self.voice = voice
        self.api_key = api_key
        self.connect_factory = connect_factory
        self._websocket: Any | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._session_ready: asyncio.Future[ProtocolEvent] | None = None
        self._session_update_waiter: asyncio.Future[ProtocolEvent] | None = None
        self._session_update_expected_instructions: str | None = None
        self._session_update_lock = asyncio.Lock()
        self._audio_buffer_clear_waiter: asyncio.Future[ProtocolEvent] | None = None
        self._audio_buffer_clear_lock = asyncio.Lock()
        self._stable_instructions = ""
        self._session_config: dict[str, Any] | None = None
        self._events: asyncio.Queue[ProtocolEvent] = asyncio.Queue()
        self._terminal_error: Exception | None = None

    @property
    def websocket(self) -> Any:
        if self._websocket is None:
            raise RuntimeError("Qwen Realtime WebSocket 未连接")
        return self._websocket

    async def connect(
        self,
        instructions: str,
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> str:
        if self._websocket is not None:
            raise RuntimeError("Qwen Realtime WebSocket 已连接")

        effective_endpoint = _endpoint_with_model(self.endpoint, self.model)
        try:
            websocket = await self.connect_factory(
                effective_endpoint,
                extra_headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except Exception as exc:
            raise QwenRealtimeConnectionError(
                self.endpoint,
                "Qwen Realtime WebSocket 连接失败",
            ) from exc

        self._websocket = websocket
        loop = asyncio.get_running_loop()
        self._session_ready = loop.create_future()
        try:
            initial_update = build_update_session(
                instructions, voice=self.voice, tools=tools
            )
            self._stable_instructions = instructions
            self._session_config = copy.deepcopy(initial_update["session"])
            await self._send(initial_update)
            self._receive_task = asyncio.create_task(
                self._receive_loop(), name="qwen-realtime-receive"
            )
            session_event = await self._session_ready
            return session_event.session_id or ""
        except Exception:
            await self.close()
            raise

    async def append_audio(self, audio: str) -> None:
        if not isinstance(audio, str) or not audio:
            raise ValueError("audio must be a non-empty base64 string")
        chunk_size = _MAX_AUDIO_BASE64_CHARS_PER_MESSAGE
        chunk_size -= chunk_size % 4
        for offset in range(0, len(audio), chunk_size):
            await self._send(
                {
                    "type": "input_audio_buffer.append",
                    "audio": audio[offset : offset + chunk_size],
                }
            )

    async def commit_audio(self) -> None:
        await self._send({"type": "input_audio_buffer.commit"})

    async def clear_uncommitted_audio(self, *, timeout: float = 5.0) -> ProtocolEvent:
        """Clear the provider's uncommitted input buffer and await its receipt."""
        self._require_connected()
        async with self._audio_buffer_clear_lock:
            loop = asyncio.get_running_loop()
            waiter: asyncio.Future[ProtocolEvent] = loop.create_future()
            self._audio_buffer_clear_waiter = waiter
            try:
                await self._send(
                    {"type": "input_audio_buffer.clear", "event_id": f"event_{uuid4().hex}"}
                )
                return await asyncio.wait_for(waiter, timeout)
            finally:
                if self._audio_buffer_clear_waiter is waiter:
                    self._audio_buffer_clear_waiter = None

    async def update_session_instructions(
        self, per_turn_instructions: str, *, timeout: float = 10.0
    ) -> ProtocolEvent:
        """Apply stable plus per-turn instructions and require the provider echo."""
        if not isinstance(per_turn_instructions, str) or not per_turn_instructions.strip():
            raise ValueError("per_turn_instructions must be a non-empty string")
        self._require_connected()
        if self._session_config is None:
            raise QwenRealtimeSessionError("session configuration is unavailable")

        async with self._session_update_lock:
            loop = asyncio.get_running_loop()
            waiter: asyncio.Future[ProtocolEvent] = loop.create_future()
            event_id = f"event_{uuid4().hex}"
            self._session_update_waiter = waiter
            combined = f"{self._stable_instructions}\n\n{per_turn_instructions}"
            self._session_update_expected_instructions = combined
            session = {"instructions": combined}
            try:
                await self._send(
                    {
                        "type": "session.update",
                        "event_id": event_id,
                        "session": session,
                    }
                )
                receipt = await asyncio.wait_for(waiter, timeout=timeout)
                if self._session_ready is not None:
                    initial = self._session_ready.result()
                    if not receipt.session_id or receipt.session_id != initial.session_id:
                        raise QwenRealtimeSessionError(
                            "session.updated belongs to another cloud session"
                        )
                received_session = (receipt.raw or {}).get("session") or {}
                if receipt.instructions != combined:
                    raise QwenRealtimeSessionError(
                        "session.updated instructions do not match the requested instructions"
                    )
                for name, value in self._session_config.items():
                    if name != "instructions" and received_session.get(name) != value:
                        raise QwenRealtimeSessionError(
                            f"session.updated changed existing session field: {name}"
                        )
                self._session_config["instructions"] = combined
                return receipt
            except asyncio.TimeoutError as exc:
                raise QwenRealtimeSessionError(
                    "session.update timed out before matching session.updated"
                ) from exc
            finally:
                if self._session_update_waiter is waiter:
                    self._session_update_waiter = None
                    self._session_update_expected_instructions = None

    async def create_first_response(self, response_id: str) -> None:
        await self._send(
            build_first_response(response_id, event_id=f"event_{uuid4().hex}")
        )

    async def send_function_call_output(self, call_id: str, output: str) -> None:
        await self._send(build_function_call_output(call_id, output))

    async def create_followup_response(
        self, response_id: str
    ) -> None:
        await self.create_first_response(response_id)

    async def cancel_response(self) -> None:
        await self._send(build_cancel_response())

    async def events(self) -> AsyncIterator[ProtocolEvent]:
        self._require_connected()
        while True:
            if not self._events.empty():
                yield await self._events.get()
                continue

            receive_task = self._require_receive_task()
            if receive_task.done():
                if self._terminal_error is not None:
                    raise self._terminal_error
                receive_task.result()
                return
            await asyncio.sleep(0)

    async def wait_for_input_committed(self) -> ProtocolEvent:
        """Wait for the cloud commit receipt that binds the acoustic range."""
        async for event in self.events():
            if event.type == "input_audio_buffer_committed":
                if not event.item_id:
                    raise QwenRealtimeProtocolError(
                        "input_audio_buffer.committed 缺少 item_id"
                    )
                return event
        raise RuntimeError("Qwen Realtime 连接已结束，未收到 commit 回执")

    async def wait_for_final(self, item_id: str | None = None) -> ProtocolEvent:
        """Wait for a final transcript, optionally bound to a commit item_id.

        When ``item_id`` is provided, any unbound, stale, or mismatched final is
        rejected and never returned to the caller.
        """
        if item_id is not None and (not isinstance(item_id, str) or not item_id):
            raise ValueError("item_id 必须是非空字符串")
        async for event in self.events():
            if event.type != "input_transcript_final" or event.is_final is not True:
                continue
            if item_id is None:
                return event
            if event.item_id == item_id:
                return event
            logger.warning(
                "忽略未绑定 final 转录: expected_item=%s actual_item=%s",
                item_id,
                event.item_id,
            )
        raise RuntimeError("Qwen Realtime 连接已结束，未收到 final 转录")

    async def wait_for_response_terminal(
        self, response_id: str | None = None, *, timeout: float | None = None
    ) -> ProtocolEvent:
        """Wait for a response terminal event without treating deltas as done."""

        async def wait() -> ProtocolEvent:
            async for event in self.events():
                if event.type not in {"response.done", "response_failed"}:
                    continue
                if response_id is not None and event.response_id != response_id:
                    continue
                return event
            raise RuntimeError("Qwen Realtime 连接已结束，未收到 response 终态")

        if timeout is None:
            return await wait()
        return await asyncio.wait_for(wait(), timeout)

    async def close(self) -> None:
        clear_waiter = self._audio_buffer_clear_waiter
        if clear_waiter is not None and not clear_waiter.done():
            clear_waiter.set_exception(
                QwenRealtimeProtocolError("Qwen Realtime closed before audio clear was acknowledged")
            )
        receive_task = self._receive_task
        if receive_task is not None:
            receive_task.cancel()
            try:
                await receive_task
            except asyncio.CancelledError:
                pass

        websocket = self._websocket
        if websocket is not None:
            await websocket.close()
        self._websocket = None

    async def _receive_loop(self) -> None:
        websocket = self.websocket
        while True:
            try:
                message = await websocket.recv()
                value = json.loads(message)
                event = parse_event(value)
            except asyncio.CancelledError:
                raise
            except UnknownProtocolEvent as exc:
                logger.warning("未知 Qwen Realtime 事件: %s", exc)
                continue
            except Exception as exc:
                error = QwenRealtimeProtocolError(f"Qwen Realtime 连接中断: {exc}")
                error.__cause__ = exc
                self._fail_session(error)
                return

            await self._events.put(event)
            if event.type == "input_audio_buffer_cleared":
                waiter = self._audio_buffer_clear_waiter
                if waiter is not None and not waiter.done():
                    waiter.set_result(event)
            if event.type == "session.updated":
                waiter = self._session_update_waiter
                if (
                    waiter is not None
                    and not waiter.done()
                    and event.session_id
                    == (self._session_ready.result().session_id if self._session_ready else None)
                    and event.instructions
                    == self._session_update_expected_instructions
                ):
                    waiter.set_result(event)
                elif self._session_ready is not None and not self._session_ready.done():
                    if event.session_id:
                        self._resolve_session(event)
                    else:
                        self._fail_session(
                            QwenRealtimeSessionError("session.updated 缺少 session id")
                        )
            elif event.type == "error":
                waiter = self._session_update_waiter
                if waiter is not None and not waiter.done():
                    waiter.set_exception(
                        QwenRealtimeSessionError(
                            event.error_message or event.error_code or "session.update rejected"
                        )
                    )

    async def _send(self, value: dict[str, Any]) -> None:
        await self.websocket.send(json.dumps(value, ensure_ascii=False))

    def _require_connected(self) -> None:
        if self._websocket is None:
            raise RuntimeError("Qwen Realtime WebSocket 未连接")

    def _require_receive_task(self) -> asyncio.Task[None]:
        if self._receive_task is None:
            raise RuntimeError("Qwen Realtime 接收任务未启动")
        return self._receive_task

    def _resolve_session(self, event: ProtocolEvent) -> None:
        if self._session_ready is not None and not self._session_ready.done():
            self._session_ready.set_result(event)

    def _fail_session(self, error: Exception) -> None:
        self._terminal_error = error
        if self._session_ready is not None and not self._session_ready.done():
            self._session_ready.set_exception(error)
