"""One-shot gate for the first host-controlled realtime response."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from services.behavior_events import BehaviorEvent
from services.voice_input_authorization import (
    CaptureRange,
    FinalTranscript,
    InputAuthorizationService,
)


class GateNotReady(RuntimeError):
    pass


class _TurnStageError(Exception):
    def __init__(self, error: TurnError):
        super().__init__(error.message)
        self.error = error


@dataclass(frozen=True)
class TurnError:
    stage: str
    error_type: str
    message: str


@dataclass(frozen=True)
class FirstResponseTurnResult:
    ok: bool
    gate: FirstResponseGate
    response_sent: bool = False
    release_started_monotonic_ms: int | None = None
    release_ended_monotonic_ms: int | None = None
    hydration_started_monotonic_ms: int | None = None
    hydration_ended_monotonic_ms: int | None = None
    release_wait_ms: int | None = None
    hydration_wait_ms: int | None = None
    gate_wait_ms: int | None = None
    error: TurnError | None = None


@dataclass
class FirstResponseGate:
    committed: bool = False
    emotion_released: bool = False
    context_ready: bool = False
    closed: bool = False

    def __post_init__(self) -> None:
        self._event = asyncio.Event()

    def _refresh(self) -> None:
        if self.committed and self.emotion_released and self.context_ready:
            self._event.set()

    def mark_committed(self) -> None:
        self._require_open()
        self.committed = True
        self._refresh()

    def mark_emotion_released(self) -> None:
        self._require_open()
        self.emotion_released = True
        self._refresh()

    def mark_context_ready(self) -> None:
        self._require_open()
        self.context_ready = True
        self._refresh()

    def assert_ready(self) -> None:
        self._require_open()
        if not self.committed:
            raise GateNotReady("正式输入尚未原子提交")
        if not self.emotion_released:
            raise GateNotReady("情绪评估尚未完成或由宿主超时放行")
        if not self.context_ready:
            raise GateNotReady("上下文尚未可用")

    async def wait_until_ready(self) -> None:
        self._require_open()
        await self._event.wait()
        self.assert_ready()

    def close(self) -> None:
        self.closed = True
        self._event.clear()

    def _require_open(self) -> None:
        if self.closed:
            raise GateNotReady("本次首答门控已关闭")


@dataclass(frozen=True)
class _MeasuredAwaitable:
    value: Any = None
    exception: Exception | None = None
    started_monotonic_ms: int = 0
    ended_monotonic_ms: int = 0


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


async def _measure(awaitable: Awaitable[Any]) -> _MeasuredAwaitable:
    started = _monotonic_ms()
    try:
        value = await awaitable
        return _MeasuredAwaitable(
            value=value,
            started_monotonic_ms=started,
            ended_monotonic_ms=_monotonic_ms(),
        )
    except Exception as exc:
        return _MeasuredAwaitable(
            exception=exc,
            started_monotonic_ms=started,
            ended_monotonic_ms=_monotonic_ms(),
        )


def _release_fact(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        released = value.get("released")
    else:
        released = getattr(value, "released", None)
    return released is None or bool(released)


def _hydration_instructions(value: Any) -> str:
    if isinstance(value, dict):
        instructions = value.get("per_response_instructions")
    else:
        instructions = getattr(value, "per_response_instructions", None)
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("水合结果缺少可用的 per_response_instructions")
    return instructions


def _event(
    *,
    event_type: str,
    session_id: Any,
    payload: dict[str, Any],
    producer_seq: int,
    media_session_id: str | None = None,
    input_turn_id: str | None = None,
    stimulus_revision: int | None = None,
    response_id: str | None = None,
) -> BehaviorEvent:
    return BehaviorEvent(
        event_id=uuid4(),
        schema_version=1,
        event_type=event_type,
        stream_id="voice-first-response",
        session_id=session_id,
        source="server",
        producer_id="hexis.voice-first-response",
        producer_seq=producer_seq,
        channel="voice",
        payload=payload,
        ts=datetime.now(timezone.utc),
        stimulus_revision=stimulus_revision,
        input_turn_id=input_turn_id,
        response_id=response_id,
        media_session_id=media_session_id,
    )


def _error(exc: Exception, stage: str) -> TurnError:
    return TurnError(
        stage=stage,
        error_type=type(exc).__name__,
        message=str(exc) or type(exc).__name__,
    )


def _discard(awaitable: Awaitable[Any]) -> None:
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()


async def run_first_response_turn(
    *,
    authorization_service: InputAuthorizationService,
    final_transcript: FinalTranscript,
    capture_range: CaptureRange,
    release: Awaitable[Any],
    hydration: Awaitable[Any],
    client: Any,
    event_sink: Callable[[BehaviorEvent], Awaitable[None]],
    response_id: str,
) -> FirstResponseTurnResult:
    """Run one committed voice input through the one-shot first-response gate."""
    gate = FirstResponseGate()
    response_sent = False
    release_started: int | None = None
    release_ended: int | None = None
    hydration_started: int | None = None
    hydration_ended: int | None = None
    release_wait_ms: int | None = None
    hydration_wait_ms: int | None = None
    gate_wait_ms: int | None = None
    error: TurnError | None = None

    try:
        commit_started = _monotonic_ms()
        await client.commit_audio()
        await event_sink(
            _event(
                event_type="voice_commit_sent",
                session_id=capture_range.session_id,
                media_session_id=capture_range.media_session_id,
                payload={
                    "capture_instance_id": str(capture_range.capture_instance_id),
                    "commit_sent_ms": _monotonic_ms() - commit_started,
                },
                producer_seq=0,
            )
        )

        final_event = await client.wait_for_final()
        if (
            final_event.type != "input_transcript_final"
            or final_event.is_final is not True
            or final_event.item_id != final_transcript.item_id
            or final_event.transcript != final_transcript.transcript
        ):
            raise _TurnStageError(
                _error(ValueError("云 final 转录与待授权转录不一致"), "final_transcript")
            )

        authorization_started = _monotonic_ms()
        receipt = await authorization_service.commit(final_transcript, capture_range)
        await event_sink(
            _event(
                event_type="voice_input_committed",
                session_id=capture_range.session_id,
                input_turn_id=str(receipt.identity.input_turn_id),
                stimulus_revision=receipt.identity.stimulus_revision,
                payload={
                    "capture_instance_id": str(capture_range.capture_instance_id),
                    "commit_ms": _monotonic_ms() - authorization_started,
                    "deduplicated": receipt.deduplicated,
                },
                producer_seq=1,
            )
        )
        gate.mark_committed()

        release_task = asyncio.create_task(_measure(release))
        hydration_task = asyncio.create_task(_measure(hydration))
        release_result, hydration_result = await asyncio.gather(
            release_task, hydration_task
        )

        release_started = release_result.started_monotonic_ms
        release_ended = release_result.ended_monotonic_ms
        hydration_started = hydration_result.started_monotonic_ms
        hydration_ended = hydration_result.ended_monotonic_ms
        release_wait_ms = release_ended - release_started
        hydration_wait_ms = hydration_ended - hydration_started
        gate_wait_ms = max(release_wait_ms, hydration_wait_ms)

        if release_result.exception is not None:
            raise _TurnStageError(
                _error(release_result.exception, "release")
            ) from release_result.exception
        if not _release_fact(release_result.value):
            raise _TurnStageError(
                _error(RuntimeError("C 节点未放行"), "release")
            )
        gate.mark_emotion_released()

        if hydration_result.exception is not None:
            raise _TurnStageError(
                _error(hydration_result.exception, "hydration")
            ) from hydration_result.exception
        try:
            instructions = _hydration_instructions(hydration_result.value)
        except ValueError as exc:
            raise _TurnStageError(_error(exc, "hydration")) from exc
        gate.mark_context_ready()

        gate.assert_ready()
        created_started = _monotonic_ms()
        await client.create_first_response(response_id, instructions)
        response_sent = True
        await event_sink(
            _event(
                event_type="voice_response_created",
                session_id=capture_range.session_id,
                response_id=response_id,
                payload={
                    "response_id": response_id,
                    "created_ms": _monotonic_ms() - created_started,
                },
                producer_seq=2,
            )
        )
    except _TurnStageError as exc:
        error = exc.error
    except GateNotReady as exc:
        error = _error(exc, "gate")
    except Exception as exc:
        error = _error(exc, "turn")
    finally:
        if error is not None:
            _discard(release)
            _discard(hydration)
        gate.close()

    return FirstResponseTurnResult(
        ok=error is None,
        gate=gate,
        response_sent=response_sent,
        release_started_monotonic_ms=release_started,
        release_ended_monotonic_ms=release_ended,
        hydration_started_monotonic_ms=hydration_started,
        hydration_ended_monotonic_ms=hydration_ended,
        release_wait_ms=release_wait_ms,
        hydration_wait_ms=hydration_wait_ms,
        gate_wait_ms=gate_wait_ms,
        error=error,
    )
