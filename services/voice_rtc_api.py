"""RTC 信令与宿主控制入口；音频不经过本地 API。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Literal
from uuid import UUID, uuid4

import asyncpg
import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from services.voice_rtc import RtcConfig, RtcProtocolError, RtcVoiceSession
from services.voice_rtc_bootstrap import prepare_rtc_context
from services.voice_rtc_history import RtcVoiceHistory

logger = logging.getLogger(__name__)


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID
    tool_surface: Literal["m4", "m5"] = "m4"


async def exchange_provider_sdp(config: RtcConfig, key: str, offer: str) -> str:
    async with httpx.AsyncClient(
        timeout=config.timeout, follow_redirects=False
    ) as client:
        result = await client.post(
            config.webrtc_endpoint,
            params={"model": config.model},
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/sdp",
            },
            content=offer,
        )
    if result.status_code != 200:
        # 不将包含凭据或上下文的供应商响应体直接暴露给浏览器。
        raise HTTPException(
            502,
            f"WebRTC provider rejected SDP ({result.status_code}); check the selected workspace/model and retry Talk.",
        )
    answer = result.text
    if not answer.lstrip().startswith("v=0") or len(answer) > 131072:
        raise HTTPException(502, "WebRTC provider returned an invalid SDP answer.")
    return answer.replace("\r\n", "\n").replace("\n", "\r\n").rstrip() + "\r\n"


@dataclass
class Entry:
    session: RtcVoiceSession
    outgoing: asyncio.Queue
    offer_started: bool = False
    control_attached: bool = False
    expiry: asyncio.Task | None = None
    heartbeat: asyncio.Task | None = None


class RtcRuntime:
    def __init__(
        self,
        *,
        pool_provider: Callable[[], Any],
        config_loader: Callable[[], RtcConfig],
        control_url: Callable[[str], str],
        exchange_sdp=exchange_provider_sdp,
        tool_executor=None,
        tools_provider=None,
        origin_provider=None,
    ):
        self.pool_provider = pool_provider
        self.config_loader = config_loader
        self.control_url = control_url
        self.exchange_sdp = exchange_sdp
        self.tool_executor = tool_executor
        self.tools_provider = tools_provider or (lambda surface: [])
        self.origin_provider = origin_provider or (
            lambda: os.getenv("HEXIS_UI_ORIGIN", "http://localhost:3477")
        )
        self.entries: dict[UUID, Entry] = {}
        self.owner_id = uuid4()
        self._creating: set[UUID] = set()
        self.router = APIRouter()
        self.router.add_api_route(
            "/api/voice/realtime/sessions", self.create, methods=["POST"]
        )
        self.router.add_api_route(
            "/api/voice/realtime/sessions/{media_session_id}/offer",
            self.offer,
            methods=["POST"],
        )
        self.router.add_api_route(
            "/api/voice/realtime/sessions/{media_session_id}",
            self.delete,
            methods=["DELETE"],
        )
        self.router.add_api_websocket_route(
            "/api/voice/realtime/sessions/{media_session_id}/control", self.control
        )

    def pool(self):
        value = self.pool_provider()
        if value is None:
            raise HTTPException(
                503, "Database is not ready; retry Talk after the local API is ready."
            )
        return value

    def entry(self, media_session_id: UUID) -> Entry:
        value = self.entries.get(media_session_id)
        if value is None or value.session.closed.is_set():
            raise HTTPException(404, "RTC media session is unavailable; reopen Talk.")
        return value

    async def reap_stale(self, session_id: UUID):
        async with self.pool().acquire() as conn:
            await conn.execute(
                """WITH stale AS (
                UPDATE voice_realtime_sessions SET status='failed',closed_reason='error',closed_at=now(),last_event_at=now()
                WHERE session_id=$1 AND status='active' AND rtc_owner_id IS NOT NULL
                  AND rtc_lease_expires_at < clock_timestamp() RETURNING id
            ) UPDATE agent_turns SET status='failed',stopped_reason='rtc_owner_lease_expired',
                completed_at=clock_timestamp(),updated_at=clock_timestamp(),
                runtime_state=runtime_state || jsonb_build_object('playback_scope','unknown')
            WHERE status IN ('running','waiting_external') AND id IN (
                SELECT i.turn_id FROM voice_rtc_inputs i JOIN stale s ON s.id=i.media_session_id
            )""",
                session_id,
            )

    async def create(self, req: SessionRequest):
        if req.session_id in self._creating or any(
            e.session.session_id == req.session_id for e in self.entries.values()
        ):
            raise HTTPException(409, "This chat already has an active Talk session.")
        self._creating.add(req.session_id)
        media_id = uuid4()
        inserted = False
        try:
            pool = self.pool()
            config = self.config_loader()
            if not os.environ.get(config.api_key_env):
                raise HTTPException(
                    503,
                    f"Selected credential environment variable {config.api_key_env} is unavailable.",
                )
            control_url = self.control_url(str(media_id))
            async with pool.acquire() as conn:
                status = await conn.fetchval(
                    "SELECT status FROM chat_sessions WHERE id=$1", req.session_id
                )
                if status != "active":
                    raise HTTPException(
                        409, "The selected chat session is unavailable."
                    )
                configured = await conn.fetchval(
                    "SELECT COALESCE(get_config_bool('agent.is_configured'),false)"
                )
                consent = await conn.fetchval("SELECT get_agent_consent_status()")
                if not configured or consent != "consent":
                    raise HTTPException(
                        403,
                        "Complete Hexis agent setup and consent before starting Talk.",
                    )
            await self.reap_stale(req.session_id)
            context = await asyncio.wait_for(
                prepare_rtc_context(pool, session_id=req.session_id), config.timeout
            )
            outgoing = asyncio.Queue(maxsize=256)

            async def send(message):
                try:
                    outgoing.put_nowait(message)
                except asyncio.QueueFull as exc:
                    raise RtcProtocolError(
                        "RTC control queue is full; close and retry Talk"
                    ) from exc

            async def cloud_session(cloud_id):
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE voice_realtime_sessions SET qwen_session_id=$2,last_event_at=now() WHERE id=$1",
                        media_id,
                        cloud_id,
                    )

            async def usage(value):
                from core.usage import record_usage

                if (
                    not value
                    or "input_tokens" not in value
                    or "output_tokens" not in value
                ):
                    return
                numbers = {
                    k: value.get(k, 0) for k in ("input_tokens", "output_tokens")
                }
                if any(
                    isinstance(n, bool) or not isinstance(n, int) or n < 0
                    for n in numbers.values()
                ):
                    raise RtcProtocolError("cloud usage counters are invalid")
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE voice_realtime_sessions SET input_tokens=input_tokens+$2,output_tokens=output_tokens+$3,last_event_at=now() WHERE id=$1",
                        media_id,
                        numbers["input_tokens"],
                        numbers["output_tokens"],
                    )
                await record_usage(
                    provider="qwen-realtime",
                    model=config.model,
                    operation="voice",
                    pool=pool,
                    session_key=str(req.session_id),
                    source="voice-webrtc",
                    **numbers,
                )

            session = RtcVoiceSession(
                session_id=req.session_id,
                media_session_id=media_id,
                config=config,
                context=context,
                history=RtcVoiceHistory(pool),
                tools=self.tools_provider(req.tool_surface),
                send=send,
                tool_executor=self.tool_executor,
                on_cloud_session=cloud_session,
                on_usage=usage,
            )
            session.tool_surface = req.tool_surface
            async with pool.acquire() as conn:
                await conn.fetchval(
                    """INSERT INTO voice_realtime_sessions(id,session_id,model,endpoint,instructions_hash,rtc_owner_id,rtc_lease_expires_at)
                    VALUES($1,$2,$3,$4,$5,$6,clock_timestamp()+($7::double precision*interval '1 second')) RETURNING id""",
                    media_id,
                    req.session_id,
                    config.model,
                    config.webrtc_endpoint,
                    context.context_version,
                    self.owner_id,
                    config.timeout * 3,
                )
            inserted = True
            entry = Entry(session, outgoing)
            self.entries[media_id] = entry

            async def expire_pending():
                await asyncio.sleep(config.timeout)
                if not session.ready:
                    await self.close(media_id, "error")

            entry.expiry = asyncio.create_task(expire_pending())
            return session.envelope(
                transport="webrtc",
                control_url=control_url,
                offer_url=f"/api/voice/realtime/sessions/{media_id}/offer",
                close_url=f"/api/voice/realtime/sessions/{media_id}",
                timeout_ms=int(config.timeout * 1000),
                context_version=context.context_version,
                model=config.model,
                session_config=session.session_config,
            )
        except HTTPException:
            raise
        except asyncpg.UniqueViolationError as exc:
            raise HTTPException(
                409,
                "This chat has another active voice session. Stop Talk in that window, use a new chat, or retry after its RTC lease expires.",
            ) from exc
        except Exception as exc:
            logger.exception("RTC session initialization failed")
            if inserted:
                await self.close(media_id, "error")
            raise HTTPException(
                503,
                f"RTC initialization failed ({type(exc).__name__}); check the selected configuration/context and retry.",
            ) from exc
        finally:
            self._creating.discard(req.session_id)

    async def offer(self, media_session_id: UUID, request: Request):
        entry = self.entry(media_session_id)
        if request.headers.get("content-type", "").split(";")[0] != "application/sdp":
            raise HTTPException(415, "Offer must use application/sdp.")
        if entry.offer_started:
            raise HTTPException(
                409, "SDP was already requested; reopen Talk instead of replaying it."
            )
        raw = await request.body()
        if len(raw) > 131072:
            raise HTTPException(413, "SDP offer is too large.")
        try:
            offer = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(400, "SDP offer must use UTF-8.") from exc
        if not offer.lstrip().startswith("v=0"):
            raise HTTPException(400, "SDP offer is malformed.")
        entry.offer_started = True
        try:
            key = os.environ.get(entry.session.config.api_key_env)
            if not key:
                raise HTTPException(503, "Selected provider credential is unavailable.")
            answer = await asyncio.wait_for(
                self.exchange_sdp(entry.session.config, key, offer),
                entry.session.config.timeout,
            )
            return Response(answer, media_type="application/sdp")
        except Exception as exc:
            await self.close(media_session_id, "error")
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(
                502,
                "WebRTC signaling failed or timed out; reopen Talk after checking the selected provider.",
            ) from exc

    async def delete(self, media_session_id: UUID):
        await self.close(media_session_id, "manual")
        return {"closed": True}

    async def close(self, media_session_id: UUID, reason: str):
        entry = self.entries.pop(media_session_id, None)
        if entry is not None:
            for task in (entry.expiry, entry.heartbeat):
                if task and task is not asyncio.current_task():
                    task.cancel()
            await entry.session.close()
        async with self.pool().acquire() as conn:
            if entry is None:
                raw = await conn.fetchval(
                    """SELECT jsonb_build_object('status',status,'owner',rtc_owner_id,
                    'expired',COALESCE(rtc_lease_expires_at < clock_timestamp(),false))
                    FROM voice_realtime_sessions WHERE id=$1""",
                    media_session_id,
                )
                row = json.loads(raw) if isinstance(raw, str) else raw
                if row is None or row.get("status") != "active":
                    return
                if row.get("owner") != str(self.owner_id) and not row.get("expired"):
                    raise HTTPException(
                        409,
                        "This voice session is owned by another active runtime; stop it there or retry after its lease expires.",
                    )
            await conn.execute(
                """WITH closed AS (
                UPDATE voice_realtime_sessions SET status=$2,closed_reason=$3,closed_at=now(),last_event_at=now()
                WHERE id=$1 AND status='active' AND (
                    rtc_owner_id=$4 OR rtc_owner_id IS NULL OR rtc_lease_expires_at < clock_timestamp()
                ) RETURNING id
            ) UPDATE agent_turns SET
                status=CASE WHEN $3='manual' THEN 'cancelled' ELSE 'failed' END,
                stopped_reason='rtc_control_' || $3, completed_at=clock_timestamp(),updated_at=clock_timestamp(),
                runtime_state=runtime_state || jsonb_build_object('playback_scope','unknown','control_closed_reason',$3::text)
            WHERE status IN ('running','waiting_external') AND id IN (
                SELECT i.turn_id FROM voice_rtc_inputs i JOIN closed c ON c.id=i.media_session_id
            )""",
                media_session_id,
                "failed" if reason == "error" else "closed",
                reason,
                self.owner_id,
            )

    async def shutdown(self):
        for media_id in list(self.entries):
            await self.close(media_id, "disconnect")

    async def control(self, websocket: WebSocket, media_session_id: UUID):
        try:
            entry = self.entry(media_session_id)
        except HTTPException:
            await websocket.close(code=1008, reason="RTC session is unavailable")
            return
        if (
            websocket.headers.get("origin") != self.origin_provider()
            or entry.control_attached
        ):
            await websocket.close(
                code=1008, reason="RTC origin or control identity is invalid"
            )
            return
        entry.control_attached = True
        session = entry.session
        await websocket.accept()
        reason = "disconnect"
        send_sequence = 0
        send_lock = asyncio.Lock()

        async def transmit(value):
            nonlocal send_sequence
            async with send_lock:
                await websocket.send_json({**value, "producer_seq": send_sequence})
                send_sequence += 1

        async def heartbeat():
            try:
                while not session.closed.is_set():
                    async with self.pool().acquire() as conn:
                        renewed = await conn.execute(
                            """UPDATE voice_realtime_sessions
                            SET rtc_lease_expires_at=clock_timestamp()+($2::double precision*interval '1 second')
                            WHERE id=$1 AND rtc_owner_id=$3 AND status='active'""",
                            media_session_id,
                            session.config.timeout * 3,
                            self.owner_id,
                        )
                        if renewed != "UPDATE 1":
                            raise RtcProtocolError(
                                "RTC session ownership or active lease was lost"
                            )
                    await asyncio.sleep(session.config.timeout / 2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                try:
                    await transmit(
                        session.envelope(
                            type="error",
                            code="rtc_lease_failed",
                            message=f"RTC session lease could not be renewed ({type(exc).__name__}); reopen Talk.",
                        )
                    )
                finally:
                    await session.close()

        entry.heartbeat = asyncio.create_task(heartbeat())

        async def sender():
            while True:
                value = await entry.outgoing.get()
                await transmit(value)

        async def receiver():
            nonlocal reason
            sequence = 0
            while True:
                raw = await websocket.receive_text()
                if len(raw) > 1_048_576:
                    raise RtcProtocolError("RTC control event is too large")
                value = json.loads(raw)
                if (
                    not isinstance(value, dict)
                    or value.get("schema_version") != 2
                    or value.get("session_id") != str(session.session_id)
                    or value.get("media_session_id") != str(media_session_id)
                    or isinstance(value.get("producer_seq"), bool)
                    or value.get("producer_seq") != sequence
                ):
                    raise RtcProtocolError(
                        "RTC control identity or sequence does not match"
                    )
                sequence += 1
                kind = value.get("type")
                if kind == "provider_event":
                    await session.provider_event(value.get("event"))
                    if session.ready and entry.expiry:
                        entry.expiry.cancel()
                        entry.expiry = None
                elif kind == "command_ack":
                    session.command_ack(value.get("command_id"), value.get("status"))
                elif kind == "close":
                    reason = "error" if value.get("reason") == "error" else "manual"
                    return
                elif kind != "ping":
                    raise RtcProtocolError("unsupported RTC control message")

        tasks = [
            asyncio.create_task(sender()),
            asyncio.create_task(receiver()),
            asyncio.create_task(session.closed.wait()),
        ]
        try:
            await session.emit(type="control_ready")
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if tasks[2] in done:
                reason = "error"
            for task in done:
                if task is not tasks[2]:
                    await task
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            reason = "error"
            logger.warning("RTC control failed: %s", exc)
            try:
                await transmit(
                    session.envelope(
                        type="error", code="rtc_control_failed", message=str(exc)
                    )
                )
            except (RuntimeError, WebSocketDisconnect):
                pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.close(media_session_id, reason)
            try:
                await websocket.close()
            except (RuntimeError, WebSocketDisconnect):
                pass
