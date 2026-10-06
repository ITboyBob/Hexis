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
        on_visual_window: Callable[[dict[str, Any], dict[str, Any], str | None], Awaitable[Any]] | None = None,
    ) -> None:
        if not math.isfinite(emotion_timeout_ms) or emotion_timeout_ms <= 0:
            raise ValueError("emotion_timeout_ms must be a positive finite number")
        self._context_reader = context_reader
        self._evaluator = evaluator
        self._hydrator = hydrator
        self._emotion_timeout_ms = emotion_timeout_ms
        self.window_host = window_host or EmotionWindowHost()
        self._on_visual_window = on_visual_window
        self.last_evaluation_task: asyncio.Task[Any] | None = None

    def set_visual_window_sink(
        self,
        sink: Callable[[dict[str, Any], dict[str, Any], str | None], Awaitable[Any]] | None,
    ) -> None:
        self._on_visual_window = sink

    async def run(self, identity: Identity, message: str) -> VoiceEmotionGateResult:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must not be empty")

        if self.window_host.current(identity) is not None:
            raise RuntimeError("identity already has an evaluation window")

        loop = asyncio.get_running_loop()
        started = loop.time()
        opened = self.window_host.open(identity, self._emotion_timeout_ms)
        reported_windows: set[tuple[Any, ...]] = set()
        await self._publish_visual_window(identity, opened, reported=reported_windows)
        close_watcher = (
            asyncio.create_task(self._watch_visual_window_close(identity, reported_windows))
            if self._on_visual_window is not None
            else None
        )

        evaluation_timing: dict[str, float | None] = {
            "start": loop.time(),
            "end": None,
        }
        evaluation_task = asyncio.create_task(
            self._evaluate(identity, message, evaluation_timing)
        )
        self.last_evaluation_task = evaluation_task
        hydration_timing: dict[str, float | None] = {
            "start": loop.time(),
            "end": None,
        }
        hydration_task = asyncio.create_task(
            self._hydrate(identity, message, hydration_timing)
        )

        try:
            done, _pending = await asyncio.wait(
                {evaluation_task}, timeout=self._emotion_timeout_ms / 1000
            )
            if evaluation_task in done:
                try:
                    value = evaluation_task.result()
                    accepted = self.window_host.accept_result(identity)
                    await self._publish_visual_window(
                        identity,
                        accepted["fact"],
                        self._label(value) if accepted["accepted"] else None,
                        reported=reported_windows,
                    )
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
                closed = self.window_host.close_for_timeout(identity, TimeoutKind.MODEL_TIMEOUT)
                await self._publish_visual_window(identity, closed, reported=reported_windows)
                evaluation = {
                    "status": "timeout",
                    "reason": TimeoutKind.MODEL_TIMEOUT.value,
                }
                release_reason = TimeoutKind.MODEL_TIMEOUT.value
                evaluation_task.add_done_callback(
                    lambda task: self._record_late_evaluation(identity, task)
                )
                evaluation_task.cancel()

            hydration = None
            hydration_error_type: str | None = None
            try:
                hydration = await hydration_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                hydration = None
                hydration_error_type = type(exc).__name__
            context_ready = hydration is not None
        except asyncio.CancelledError:
            self._recover_cancelled_children(identity, evaluation_task, hydration_task)
            closed = self.window_host.current(identity)
            if closed is not None and closed.get("status") == "closed":
                await self._publish_visual_window(identity, closed, reported=reported_windows)
            raise
        finally:
            fact = self.window_host.current(identity)
            if (fact is None or fact.get("status") != "open") and close_watcher is not None:
                if not close_watcher.done():
                    close_watcher.cancel()
                    await asyncio.gather(close_watcher, return_exceptions=True)

        def _elapsed(timing: dict[str, float | None]) -> float:
            end = timing["end"]
            return ((end if end is not None else loop.time()) - timing["start"]) * 1000

        metrics: dict[str, Any] = {
            "eval_ms": _elapsed(evaluation_timing),
            "hydrate_ms": _elapsed(hydration_timing),
            "gate_wait_ms": (loop.time() - started) * 1000,
            "release_reason": release_reason,
        }
        if hydration_error_type is not None:
            metrics["hydration_error_type"] = hydration_error_type

        return VoiceEmotionGateResult(
            identity=dict(identity),
            released=True,
            context_ready=context_ready,
            evaluation=evaluation,
            hydration=hydration,
            visual_window=self.window_host.current(identity),
            metrics=metrics,
            late_diagnostics=self.window_host.late_diagnostics,
        )

    def _recover_cancelled_children(
        self,
        identity: Identity,
        evaluation_task: asyncio.Task[Any],
        hydration_task: asyncio.Task[Any],
    ) -> None:
        """Cancel and await both children, recording one structured diagnostic."""
        for task in (evaluation_task, hydration_task):
            if not task.done():
                task.cancel()
        if evaluation_task.done():
            try:
                evaluation_task.exception()
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self.window_host.close_for_timeout(identity, TimeoutKind.MODEL_TIMEOUT)
        diagnostic = self.window_host.record_late_error(
            identity,
            asyncio.CancelledError(),
            rejection_reason="coordinator_cancelled",
        )
        if diagnostic is None:
            self.window_host.record_cancellation(
                identity, rejection_reason="coordinator_cancelled"
            )

    async def _evaluate(
        self, identity: Identity, message: str, timing: dict[str, float | None]
    ) -> Any:
        try:
            context = await self._context_reader(identity)
            return await self._evaluator(message, context)
        finally:
            timing["end"] = asyncio.get_running_loop().time()

    async def _hydrate(
        self, identity: Identity, message: str, timing: dict[str, float | None]
    ) -> Any:
        try:
            return await self._hydrator(identity, message)
        finally:
            timing["end"] = asyncio.get_running_loop().time()

    def _record_late_evaluation(
        self, identity: Identity, task: asyncio.Task[Any]
    ) -> None:
        if task.cancelled():
            self.window_host.record_late_error(identity, asyncio.CancelledError())
            return
        try:
            value = task.result()
        except Exception as exc:
            self.window_host.record_late_error(identity, exc)
            return
        self.window_host.record_late_result(identity, value, status="valid")

    async def _watch_visual_window_close(
        self, identity: Identity, reported: set[tuple[Any, ...]]
    ) -> None:
        """Publish the host's authoritative timeout close, including after run returns."""
        while True:
            await asyncio.sleep(0.01)
            fact = self.window_host.current(identity)
            if fact is None or fact.get("status") != "open":
                if fact is not None and fact.get("status") == "closed":
                    await self._publish_visual_window(identity, fact, reported=reported)
                return

    async def _publish_visual_window(
        self,
        identity: Identity,
        fact: dict[str, Any] | None,
        label: str | None = None,
        *,
        reported: set[tuple[Any, ...]] | None = None,
    ) -> None:
        if fact is None or self._on_visual_window is None:
            return
        event_key = (
            tuple(identity.get(key) for key in ("session_id", "input_turn_id", "input_order", "stimulus_revision")),
            fact.get("status"),
            fact.get("decision_seq"),
            fact.get("reason"),
        )
        if reported is not None:
            if event_key in reported:
                return
            reported.add(event_key)
        result = self._on_visual_window(dict(identity), dict(fact), label)
        if asyncio.iscoroutine(result):
            await result

    @staticmethod
    def _label(value: Any) -> str | None:
        from services.avatar_emotion import EMOTIONS

        if not isinstance(value, dict):
            return None
        label = value.get("label")
        return label if isinstance(label, str) and label in EMOTIONS else None
