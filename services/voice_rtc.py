"""WebRTC 会话：媒体直达云端，宿主只处理控制、输入身份和工具事实。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse
from uuid import UUID, uuid4, uuid5

from services.m5_tool_bridge import BoundToolCall


class RtcProtocolError(ValueError):
    """无法可靠关联的云端或控制事件，必须终止当前媒体会话。"""


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise RtcProtocolError(f"{name} must be a non-empty bounded string")
    return value


@dataclass(frozen=True)
class RtcConfig:
    endpoint: str
    webrtc_endpoint: str
    model: str
    voice: str
    api_key_env: str
    timeout: float

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> RtcConfig:
        selected = urlparse(_text(value.get("endpoint"), "endpoint"))
        rtc = urlparse(_text(value.get("webrtc_endpoint"), "webrtc_endpoint"))
        if (
            selected.scheme not in {"wss", "https"}
            or rtc.scheme != "https"
            or not selected.hostname
            or rtc.hostname != selected.hostname
            or rtc.username
            or rtc.password
            or rtc.query
            or rtc.fragment
            or rtc.path != "/api/v1/webrtc/realtime"
        ):
            raise ValueError(
                "WebRTC endpoint must use HTTPS and the selected provider workspace"
            )
        timeout = value.get("timeout")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError(
                "selected realtime timeout must be a positive finite number"
            )
        return cls(
            endpoint=value["endpoint"],
            webrtc_endpoint=value["webrtc_endpoint"],
            model=_text(value.get("model"), "model"),
            voice=_text(value.get("voice"), "voice"),
            api_key_env=_text(value.get("api_key_env"), "api_key_env"),
            timeout=float(timeout),
        )


@dataclass(frozen=True)
class RtcContext:
    instructions: str
    context_version: str


class RtcVoiceSession:
    def __init__(
        self,
        *,
        session_id: UUID,
        media_session_id: UUID,
        config: RtcConfig,
        context: RtcContext,
        history: Any,
        tools: list[dict[str, Any]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
        tool_executor: (
            Callable[[RtcVoiceSession, BoundToolCall], Awaitable[str]] | None
        ) = None,
        on_cloud_session: Callable[[str], Awaitable[None]] | None = None,
        on_usage: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ):
        self.session_id = session_id
        self.media_session_id = media_session_id
        self.config = config
        self.context = context
        self.history = history
        self.send = send
        self.tool_executor = tool_executor
        self.on_cloud_session = on_cloud_session
        self.on_usage = on_usage
        self.tool_surface = "m5"
        self.ready = False
        self.closed = asyncio.Event()
        self.cloud_session_id: str | None = None
        self.session_config = {
            "modalities": ["text", "audio"],
            "instructions": context.instructions,
            "audio": {"output": {"voice": config.voice}},
            "input_audio_transcription": None,
            "turn_detection": {"type": "semantic_vad"},
            "tools": tools,
        }
        self.inputs: dict[str, dict[str, Any]] = {}
        self.responses: dict[str, dict[str, Any]] = {}
        self.calls: dict[str, BoundToolCall] = {}
        self._seen: OrderedDict[str, str] = OrderedDict()
        self._pending_inputs: deque[str] = deque()
        self._followup_input: str | None = None
        self._followup_received: asyncio.Future | None = None
        self._tool_tasks: set[asyncio.Task] = set()
        self._command_waiters: dict[str, asyncio.Future] = {}
        self._speech_pending = False
        self._activity = asyncio.Event()
        self._latest_input: str | None = None

    def envelope(self, **values: Any) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "session_id": str(self.session_id),
            "media_session_id": str(self.media_session_id),
            **values,
        }

    async def emit(self, **values: Any) -> None:
        await self.send(self.envelope(**values))

    def _deduplicate(self, event: dict[str, Any]) -> bool:
        event_id = event.get("event_id")
        if event_id is None:
            return False
        event_id = _text(event_id, "event_id")
        digest = hashlib.sha256(
            json.dumps(event, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        previous = self._seen.get(event_id)
        if previous is not None:
            if previous != digest:
                raise RtcProtocolError(
                    "provider event id was reused for different facts"
                )
            return True
        self._seen[event_id] = digest
        if len(self._seen) > 4096:
            self._seen.popitem(last=False)
        return False

    async def provider_event(self, event: dict[str, Any]) -> None:
        if self.closed.is_set() or not isinstance(event, dict):
            raise RtcProtocolError("realtime session is closed or event is malformed")
        kind = _text(event.get("type"), "event type")
        if kind in {
            "input_audio_buffer.append",
            "input_audio_buffer.commit",
            "response.audio.delta",
        } or kind.startswith("conversation.item.input_audio_transcription."):
            raise RtcProtocolError(
                "unexpected PCM or input transcription event in WebRTC mode"
            )
        if self._deduplicate(event):
            return
        if kind == "session.created":
            cloud = event.get("session", {})
            cloud_id = _text(cloud.get("id"), "cloud session id")
            if self.cloud_session_id not in {None, cloud_id}:
                raise RtcProtocolError("cloud session identity changed")
            if cloud.get("model") not in {None, self.config.model}:
                raise RtcProtocolError("cloud model differs from selected model")
            self.cloud_session_id = cloud_id
            if self.on_cloud_session:
                await self.on_cloud_session(cloud_id)
            return
        if kind == "session.updated":
            value = event.get("session", {})
            # 官方新字段优先，兼容既有 session.voice 回显。
            voice = (
                value.get("audio", {})
                .get("output", {})
                .get("voice", value.get("voice"))
            )
            if (
                not self.cloud_session_id
                or value.get("id", self.cloud_session_id) != self.cloud_session_id
                or value.get("model", self.config.model) != self.config.model
                or voice != self.config.voice
                or value.get("input_audio_transcription") is not None
                or value.get("turn_detection", {}).get("type") != "semantic_vad"
                or value.get("turn_detection", {}).get("create_response") is False
                or value.get("instructions") != self.context.instructions
                or set(value.get("modalities", [])) != {"text", "audio"}
            ):
                raise RtcProtocolError(
                    "cloud session configuration does not match the prepared context"
                )

            def names(tools):
                if not isinstance(tools, list):
                    raise RtcProtocolError("cloud tool configuration is missing")
                result = set()
                for tool in tools:
                    function = (
                        tool.get("function", tool) if isinstance(tool, dict) else None
                    )
                    if not isinstance(function, dict) or not isinstance(
                        function.get("name"), str
                    ):
                        raise RtcProtocolError(
                            "cloud function tool configuration is malformed"
                        )
                    result.add(function["name"])
                return result

            if names(value.get("tools")) != names(self.session_config["tools"]):
                raise RtcProtocolError(
                    "cloud tools differ from the selected frontend surface"
                )
            self.ready = True
            await self.emit(type="ready", cloud_session_id=self.cloud_session_id)
            return
        if kind == "error":
            error = event.get("error", {})
            raise RtcProtocolError(
                f"cloud realtime error: {error.get('code', 'unknown')}: {error.get('message', 'retry Talk')}"
            )
        if not self.ready:
            raise RtcProtocolError(
                "cloud media events arrived before configuration confirmation"
            )
        if kind == "input_audio_buffer.speech_started":
            self._speech_pending = True
            self._activity.set()
            return
        if kind == "input_audio_buffer.speech_stopped":
            # 停止不等于有效提交；等待 committed 才创建输入和调度关联。
            return
        if kind == "input_audio_buffer.committed":
            item = _text(event.get("item_id"), "input item id")
            self._speech_pending = False
            if item not in self.inputs:
                if len(self.inputs) >= 512:
                    raise RtcProtocolError(
                        "voice input association budget exhausted; reopen Talk"
                    )
                input_id = str(uuid5(self.media_session_id, item))
                order = len(self.inputs)
                turn_id = await self.history.start_audio_input(
                    session_id=self.session_id,
                    media_session_id=self.media_session_id,
                    input_turn_id=UUID(input_id),
                    input_order=order,
                    provider_item_id=item,
                    context_version=self.context.context_version,
                )
                self.inputs[item] = {
                    "input_turn_id": input_id,
                    "input_order": order,
                    "turn_id": turn_id,
                    "provider_item_id": item,
                }
                self._pending_inputs.append(item)
                self._latest_input = item
            self._activity.set()
            return
        if kind == "response.created":
            response_id = _text(event.get("response", {}).get("id"), "response id")
            if response_id in self.responses:
                return
            if self._followup_input is not None:
                if self._pending_inputs:
                    raise RtcProtocolError(
                        "cloud automatic and tool response identities are ambiguous"
                    )
                item = self._followup_input
                self._followup_input = None
                if (
                    self._followup_received is not None
                    and not self._followup_received.done()
                ):
                    self._followup_received.set_result(response_id)
            elif self._pending_inputs:
                item = self._pending_inputs.popleft()
            else:
                raise RtcProtocolError("cloud response has no committed input identity")
            self.responses[response_id] = {
                "item": item,
                "status": "in_progress",
                "text": {},
                "calls": {},
                "done": asyncio.Event(),
            }
            self._activity.set()
            return
        response_id = event.get("response_id") or event.get("response", {}).get("id")
        if kind in {
            "response.audio_transcript.delta",
            "response.text.delta",
            "response.audio_transcript.done",
            "response.text.done",
        }:
            response = self._response(response_id)
            item = str(event.get("item_id") or "output")
            if kind.endswith(".delta"):
                response["text"][item] = response["text"].get(item, "") + str(
                    event.get("delta") or ""
                )
                identity = self.inputs[response["item"]]
                await self.emit(
                    type="assistant_text",
                    response_id=response_id,
                    turn_id=str(identity["turn_id"]),
                    text=str(event.get("delta") or ""),
                )
            else:
                response["text"][item] = str(
                    event.get("transcript") or event.get("text") or ""
                )
            return
        if kind == "response.function_call_arguments.done":
            response = self._response(response_id)
            call_id = _text(event.get("call_id"), "tool call id")
            call = self.authorize_voice_tool_call(
                response_id=response_id,
                call_id=call_id,
                name=_text(event.get("name"), "tool name"),
                arguments=event.get("arguments"),
                request_id=event.get("event_id"),
            )
            response["calls"][call_id] = call
            return
        if kind == "response.done":
            response = self._response(response_id)
            final = event["response"]
            status = _text(final.get("status"), "response status")
            terminal_digest = hashlib.sha256(
                json.dumps(final, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            if response["status"] != "in_progress":
                if (
                    response["status"] != status
                    or response.get("terminal_digest") != terminal_digest
                ):
                    raise RtcProtocolError("cloud response terminal facts changed")
                return
            response["status"] = status
            response["terminal_digest"] = terminal_digest
            response["done"].set()
            snapshots = []
            for item in final.get("output", []):
                if item.get("type") == "function_call":
                    call_id = _text(item.get("call_id"), "tool call id")
                    call = self.authorize_voice_tool_call(
                        response_id=response_id,
                        call_id=call_id,
                        name=_text(item.get("name"), "tool name"),
                        arguments=item.get("arguments"),
                    )
                    response["calls"][call_id] = call
                elif item.get("type") == "message":
                    contents = item.get("content", [])
                    text = "".join(
                        str(c.get("transcript") or "")
                        for c in contents
                        if c.get("type") == "audio"
                    )
                    if not text:
                        text = "".join(
                            str(c.get("text") or "")
                            for c in contents
                            if c.get("type") == "text"
                        )
                    if text:
                        snapshots.append(text)
            text = (
                "\n".join(snapshots)
                if snapshots
                else "".join(response["text"].values())
            )
            identity = self.inputs[response["item"]]
            await self.history.save_generated_output(
                session_id=self.session_id,
                media_session_id=self.media_session_id,
                input_turn_id=UUID(identity["input_turn_id"]),
                turn_id=identity["turn_id"],
                response_id=response_id,
                assistant_text=text,
                status=status,
                context_version=self.context.context_version,
                playback_scope="unknown",
                final_for_turn=status != "completed" or not response["calls"],
            )
            if self.on_usage:
                await self.on_usage(final.get("usage") or {})
            await self.emit(
                type="response_saved",
                response_id=response_id,
                turn_id=str(identity["turn_id"]),
                text=text,
                status=status,
                playback_scope="unknown",
            )
            if response["calls"] and status == "completed":
                task = asyncio.create_task(self._execute_tools(response_id))
                self._tool_tasks.add(task)
                task.add_done_callback(self._tool_tasks.discard)
            self._activity.set()

    def _response(self, response_id: Any) -> dict[str, Any]:
        if not isinstance(response_id, str) or response_id not in self.responses:
            raise RtcProtocolError("provider response identity is unknown")
        return self.responses[response_id]

    def authorize_voice_tool_call(
        self,
        *,
        response_id: str,
        call_id: str,
        name: str,
        arguments: str,
        request_id: str | None = None,
    ) -> BoundToolCall:
        if not isinstance(arguments, str):
            raise RtcProtocolError("tool arguments must be a JSON string")
        previous = self.calls.get(call_id)
        if previous:
            if (previous.response_id, previous.name, previous.raw_arguments) != (
                response_id,
                name,
                arguments,
            ):
                raise RtcProtocolError(
                    "tool call id conflicts with its original binding"
                )
            return previous
        response = self._response(response_id)
        identity = self.inputs[response["item"]]
        call = BoundToolCall(
            call_id,
            uuid5(self.media_session_id, "tool:" + call_id),
            name,
            arguments,
            self.session_id,
            identity["input_turn_id"],
            identity["input_order"],
            0,
            response_id,
            response_id,
            request_id,
            self.tool_surface,
        )
        self.calls[call_id] = call
        return call

    def validate_voice_tool_call(self, call: BoundToolCall) -> None:
        response = self._response(call.response_id)
        if (
            self.closed.is_set()
            or not self.ready
            or call.origin_session_id != self.session_id
            or self.calls.get(call.actual_call_id) != call
            or response["status"] not in {"in_progress", "completed"}
        ):
            raise RtcProtocolError("voice tool is not bound to a usable cloud response")

    async def _await_or_closed(self, value: Awaitable[Any]) -> Any:
        task = asyncio.ensure_future(value)
        stop = asyncio.create_task(self.closed.wait())
        try:
            done, _ = await asyncio.wait(
                {task, stop}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop in done:
                raise RtcProtocolError("voice session closed while awaiting a tool")
            return await task
        finally:
            for pending in (task, stop):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, stop, return_exceptions=True)

    async def command(self, event: dict[str, Any]) -> None:
        command_id = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self._command_waiters[command_id] = future
        try:
            await self.emit(type="provider_command", command_id=command_id, event=event)
            await asyncio.wait_for(asyncio.shield(future), self.config.timeout)
        finally:
            self._command_waiters.pop(command_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    def command_ack(self, command_id: str, status: str) -> None:
        future = self._command_waiters.get(command_id)
        if future is None or future.done():
            raise RtcProtocolError("unknown or repeated control command receipt")
        if status == "sent":
            future.set_result(None)
        else:
            future.set_exception(
                RtcProtocolError(
                    "provider command delivery is unknown; it was not replayed"
                )
            )

    async def _execute_tools(self, response_id: str) -> None:
        accepted = None
        stage = "backend tool execution"
        try:
            response = self._response(response_id)
            if self.tool_executor is None:
                raise RtcProtocolError("backend tool execution is unavailable")
            outputs = []
            for call in response["calls"].values():
                self.validate_voice_tool_call(call)
                outputs.append(
                    (call.actual_call_id, await self.tool_executor(self, call))
                )
            for call_id, output in outputs:
                stage = "tool output delivery"
                await self.command(
                    {
                        "type": "conversation.item.create",
                        "event_id": str(uuid4()),
                        "item": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": output,
                        },
                    }
                )
            # 结果不重放；只有没有新讲话和活动响应时，才请求工具续答。
            while not self.closed.is_set():
                self._activity.clear()
                busy = (
                    self._speech_pending
                    or self._pending_inputs
                    or self._followup_input is not None
                    or any(
                        r["status"] == "in_progress" for r in self.responses.values()
                    )
                )
                if not busy:
                    break
                await self._await_or_closed(self._activity.wait())
            self._followup_input = response["item"]
            accepted = asyncio.get_running_loop().create_future()
            self._followup_received = accepted
            stage = "tool follow-up acceptance"
            await self.command({"type": "response.create", "event_id": str(uuid4())})
            # 浏览器 sent 回执不代表供应商接受；必须看到真实 response.created。
            await asyncio.wait_for(asyncio.shield(accepted), self.config.timeout)
        except Exception as exc:
            if not self.closed.is_set():
                await self.emit(
                    type="error",
                    code="tool_delivery_unknown",
                    message=f"RTC {stage} failed ({type(exc).__name__}): {exc}. The command was not replayed; reopen Talk to continue.",
                )
                await self.close()
        finally:
            if accepted is not None:
                if not accepted.done():
                    accepted.cancel()
                elif not accepted.cancelled():
                    accepted.exception()
                if self._followup_received is accepted:
                    self._followup_received = None

    async def close(self) -> None:
        if self.closed.is_set():
            return
        self.ready = False
        self.closed.set()
        self._activity.set()
        if self._followup_received is not None and not self._followup_received.done():
            self._followup_received.cancel()
        for future in self._command_waiters.values():
            if not future.done():
                future.set_exception(
                    RtcProtocolError(
                        "voice control closed before command delivery was confirmed"
                    )
                )
        tasks = [t for t in self._tool_tasks if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
