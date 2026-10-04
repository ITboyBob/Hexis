"""Coordinate one logical emotion evaluation with deterministic hydration."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from services.voice_emotion_window import EmotionWindowHost, LateDiagnostic, TimeoutKind


Identity = Mapping[str, Any]


@dataclass(frozen=True)
class VoiceEmotionGateResult:
    identity: dict[str, Any]
    released: bool
    context_ready: bool
    evaluation: dict[str, Any]
    hydration: Any | None
    visual_window: dict[str, Any] | None
    metrics: dict[str, Any]
    late_diagnostics: tuple[LateDiagnostic, ...]

    def to_b_gate_facts(self) -> dict[str, bool]:
        return {"released": self.released, "context_ready": self.context_ready}


class VoiceEmotionCoordinator:
    """Run evaluation and hydration in parallel without coupling B to visuals."""

    def __init__(
        self,
        *,
        context_reader: Callable[[Identity], Awaitable[Any]],
        evaluator: Callable[[str, Any], Awaitable[Any]],
        hydrator: Callable[[Identity, str], Awaitable[Any]],
        emotion_timeout_ms: float,
        window_host: EmotionWindowHost | None = None,
    ) -> None:
        if not math.isfinite(emotion_timeout_ms) or emotion_timeout_ms <= 0:
            raise ValueError("emotion_timeout_ms must be a positive finite number")
        self._context_reader = context_reader
        self._evaluator = evaluator
        self._hydrator = hydrator
        self._emotion_timeout_ms = emotion_timeout_ms
        self.window_host = window_host or EmotionWindowHost()

    async def run(
        self, identity: Identity, message: str
    ) -> VoiceEmotionGateResult:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must not be empty")

        loop = asyncio.get_running_loop()
        started = loop.time()
        self.window_host.open(identity, self._emotion_timeout_ms)

        evaluation_started = loop.time()
        evaluation_task = asyncio.create_task(self._evaluate(identity, message))
        hydration_started = loop.time()
        hydration_task = asyncio.create_task(self._hydrate(identity, message))

        done, _pending = await asyncio.wait(
            {evaluation_task}, timeout=self._emotion_timeout_ms / 1000
        )
        evaluation_finished = loop.time()
        if evaluation_task in done:
            try:
                value = evaluation_task.result()
                accepted = self.window_host.accept_result(identity)
                evaluation = {
                    "status": "valid",
                    "value": value,
                    "accepted": accepted["accepted"],
                }
                release_reason = "evaluation_complete"
            except Exception as exc:
                evaluation = {
                    "status": "error",
                    "error_type": type(exc).__name__,
                }
                release_reason = "evaluation_error"
        else:
            self.window_host.close_for_timeout(
                identity, TimeoutKind.MODEL_TIMEOUT
            )
            evaluation = {
                "status": "timeout",
                "reason": TimeoutKind.MODEL_TIMEOUT.value,
            }
            release_reason = TimeoutKind.MODEL_TIMEOUT.value
            evaluation_task.add_done_callback(
                lambda task: self._record_late_evaluation(identity, task)
            )

        context_ready = (
            hydration_task.done()
            and not hydration_task.cancelled()
            and hydration_task.exception() is None
        )
        try:
            hydration = await hydration_task
        except Exception:
            hydration = None
            context_ready = False

        return VoiceEmotionGateResult(
            identity=dict(identity),
            released=True,
            context_ready=context_ready,
            evaluation=evaluation,
            hydration=hydration,
            visual_window=self.window_host.current(identity),
            metrics={
                "eval_ms": (evaluation_finished - evaluation_started) * 1000,
                "hydrate_ms": (loop.time() - hydration_started) * 1000,
                "gate_wait_ms": (evaluation_finished - started) * 1000,
                "release_reason": release_reason,
            },
            late_diagnostics=self.window_host.late_diagnostics,
        )

    async def _evaluate(self, identity: Identity, message: str) -> Any:
        context = await self._context_reader(identity)
        return await self._evaluator(message, context)

    async def _hydrate(self, identity: Identity, message: str) -> Any:
        return await self._hydrator(identity, message)

    def _record_late_evaluation(
        self, identity: Identity, task: asyncio.Task[Any]
    ) -> None:
        if task.cancelled():
            return
        try:
            value = task.result()
        except Exception as exc:
            self.window_host.record_late_error(identity, exc)
            return
        self.window_host.record_late_result(identity, value, status="valid")
