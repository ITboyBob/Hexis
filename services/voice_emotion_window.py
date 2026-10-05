"""Service-side visual eligibility host for realtime emotion evaluation."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


def _default_schedule(callback: Callable[[], None], delay_ms: float) -> Any:
    """Run the deadline on the event loop when available, else on a timer thread.

    ``dispatch`` is a seam so tests can invoke the callback synchronously.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        timer = threading.Timer(
            delay_ms / 1000,
            lambda: dispatch(callback, loop=None),
        )
        timer.daemon = True
        timer.start()
        return {"kind": "thread", "timer": timer}
    handle = loop.call_later(delay_ms / 1000, lambda: dispatch(callback, loop=loop))
    return {"kind": "loop", "handle": handle}


def dispatch(callback: Callable[[], None], *, loop: Any = None) -> None:
    """Invoke a scheduled deadline callback safely on its owning loop."""
    if loop is None:
        callback()
        return
    loop.call_soon_threadsafe(callback)


def _default_cancel(handle: Any) -> None:
    if handle is None:
        return
    if isinstance(handle, dict):
        if handle.get("kind") == "loop":
            handle["handle"].cancel()
            return
        timer = handle.get("timer")
        if timer is not None:
            timer.cancel()
        return
    handle.cancel()


class TimeoutKind(str, Enum):
    MODEL_TIMEOUT = "model_timeout"
    TOTAL_DEADLINE = "total_deadline"


@dataclass(frozen=True)
class LateDiagnostic:
    identity: dict[str, Any]
    status: str
    value: Any | None = None
    error_type: str | None = None
    accepted: bool = False
    rejection_reason: str = "visual_window_closed"
    logical_call_count: int = 1


class EmotionWindowHost:
    """Owns visual eligibility separately from evaluation status."""

    def __init__(
        self,
        *,
        now: Callable[[], float] | None = None,
        schedule: Callable[[Callable[[], None], float], Any] | None = None,
        cancel: Callable[[Any], None] | None = None,
    ) -> None:
        if now is None:
            self._now_ms = lambda: time.monotonic() * 1000.0
        else:
            self._now_ms = lambda: float(now()) * 1000.0
        self._schedule = schedule or _default_schedule
        self._cancel = cancel or _default_cancel
        self._entries: dict[tuple[str, str, int, int], dict[str, Any]] = {}
        self._controls: list[dict[str, Any]] = []
        self._late_diagnostics: list[LateDiagnostic] = []
        self._disposed = False

    def open(self, identity: Mapping[str, Any], timeout_ms: float) -> dict[str, Any]:
        if self._disposed:
            raise RuntimeError("emotion window host is disposed")
        key, normalized = self._identity(identity)
        existing = self._entries.get(key)
        if existing is not None:
            return dict(existing["fact"])
        if not math.isfinite(timeout_ms) or timeout_ms <= 0:
            raise ValueError("timeout_ms must be a positive finite number")

        entry: dict[str, Any] = {
            "identity": normalized,
            "fact": {
                "status": "open",
                "owner": "service",
                "decision_seq": 0,
            },
            "deadline": self._now_ms() + timeout_ms,
            "timer": None,
            "logical_call_count": 1,
            "transport_attempts": 0,
        }
        entry["timer"] = self._schedule(
            lambda: self._expire_entry(key, entry), timeout_ms
        )
        self._entries[key] = entry
        return dict(entry["fact"])

    def current(self, identity: Mapping[str, Any]) -> dict[str, Any] | None:
        key, _ = self._identity(identity)
        entry = self._entries.get(key)
        return dict(entry["fact"]) if entry is not None else None

    def accept_result(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        if self._disposed:
            return {"accepted": False, "fact": None}
        key, _ = self._identity(identity)
        entry = self._entries.get(key)
        if entry is None:
            return {"accepted": False, "fact": None}
        if entry["fact"]["status"] != "open":
            return {"accepted": False, "fact": dict(entry["fact"])}
        if self._now_ms() >= entry["deadline"]:
            return {"accepted": False, "fact": dict(entry["fact"])}

        if entry["timer"] is not None:
            self._cancel(entry["timer"])
            entry["timer"] = None
        entry["fact"] = {
            "status": "accepted",
            "owner": "service",
            "decision_seq": entry["fact"]["decision_seq"] + 1,
        }
        return {"accepted": True, "fact": dict(entry["fact"])}

    def expire(self, identity: Mapping[str, Any]) -> dict[str, Any] | None:
        key, _ = self._identity(identity)
        entry = self._entries.get(key)
        if entry is None:
            return None
        return self._expire_entry(key, entry)

    def close_for_timeout(
        self, identity: Mapping[str, Any], kind: TimeoutKind
    ) -> dict[str, Any] | None:
        if not isinstance(kind, TimeoutKind):
            raise TypeError("kind must be a TimeoutKind")
        key, _ = self._identity(identity)
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._close_entry(entry, kind.value)
        return dict(entry["fact"])

    def record_late_result(
        self,
        identity: Mapping[str, Any],
        value: Any,
        *,
        status: str = "valid",
    ) -> LateDiagnostic | None:
        if status not in {"valid", "pending", "query"}:
            raise ValueError("invalid late result status")
        key, _ = self._identity(identity)
        entry = self._entries.get(key)
        if entry is None or entry["fact"]["status"] != "closed":
            return None
        diagnostic = LateDiagnostic(
            identity=dict(entry["identity"]),
            status=status,
            value=value,
            logical_call_count=entry["logical_call_count"],
        )
        self._late_diagnostics.append(diagnostic)
        return diagnostic

    def record_late_error(
        self,
        identity: Mapping[str, Any],
        error: BaseException,
        *,
        rejection_reason: str = "visual_window_closed",
    ) -> LateDiagnostic | None:
        key, _ = self._identity(identity)
        entry = self._entries.get(key)
        if entry is None or entry["fact"]["status"] != "closed":
            return None
        diagnostic = LateDiagnostic(
            identity=dict(entry["identity"]),
            status="error",
            error_type=type(error).__name__,
            logical_call_count=entry["logical_call_count"],
            rejection_reason=rejection_reason,
        )
        self._late_diagnostics.append(diagnostic)
        return diagnostic

    def record_cancellation(
        self,
        identity: Mapping[str, Any],
        *,
        rejection_reason: str = "coordinator_cancelled",
    ) -> LateDiagnostic:
        """Always record a cancellation diagnostic, even without a closed entry."""
        key, normalized = self._identity(identity)
        entry = self._entries.get(key)
        diagnostic = LateDiagnostic(
            identity=normalized,
            status="error",
            error_type="CancelledError",
            logical_call_count=entry["logical_call_count"] if entry else 1,
            rejection_reason=rejection_reason,
        )
        self._late_diagnostics.append(diagnostic)
        return diagnostic

    def record_transport_attempts(
        self, identity: Mapping[str, Any], attempts: int
    ) -> None:
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise ValueError("attempts must be a positive integer")
        key, _ = self._identity(identity)
        entry = self._entries.get(key)
        if entry is None:
            raise ValueError("identity has no open evaluation window")
        entry["transport_attempts"] = attempts

    def call_metrics(self, identity: Mapping[str, Any]) -> dict[str, int] | None:
        key, _ = self._identity(identity)
        entry = self._entries.get(key)
        if entry is None:
            return None
        return {
            "logical_call_count": entry["logical_call_count"],
            "transport_attempts": entry["transport_attempts"],
        }

    @property
    def late_diagnostics(self) -> tuple[LateDiagnostic, ...]:
        return tuple(self._late_diagnostics)

    def reset_visuals(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        return self._record_control(identity, "reset")

    def reattach(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        return self._record_control(identity, "reattach")

    def reduced_motion(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        return self._record_control(identity, "reduced_motion")

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        for entry in self._entries.values():
            if entry["timer"] is not None:
                self._cancel(entry["timer"])
                entry["timer"] = None
        self._entries.clear()

    def _expire_entry(
        self, key: tuple[str, str, int, int], entry: dict[str, Any]
    ) -> dict[str, Any] | None:
        if self._disposed or self._entries.get(key) is not entry:
            return None
        if entry["fact"]["status"] != "open":
            return dict(entry["fact"])
        remaining = entry["deadline"] - self._now_ms()
        if remaining > 0:
            entry["timer"] = self._schedule(
                lambda: self._expire_entry(key, entry), remaining
            )
            return dict(entry["fact"])
        self.close_for_timeout(entry["identity"], TimeoutKind.MODEL_TIMEOUT)
        return dict(entry["fact"])

    def _close_entry(self, entry: dict[str, Any], reason: str) -> None:
        if entry["fact"]["status"] != "open":
            return
        if entry["timer"] is not None:
            self._cancel(entry["timer"])
            entry["timer"] = None
        entry["fact"] = {
            "status": "closed",
            "owner": "service",
            "decision_seq": entry["fact"]["decision_seq"] + 1,
            "reason": reason,
        }

    def _record_control(
        self, identity: Mapping[str, Any], action: str
    ) -> dict[str, Any]:
        _, normalized = self._identity(identity)
        record = {"identity": normalized, "action": action}
        self._controls.append(record)
        return dict(record)

    def _identity(
        self, identity: Mapping[str, Any]
    ) -> tuple[tuple[str, str, int, int], dict[str, Any]]:
        if not isinstance(identity, Mapping):
            raise ValueError("identity must be a mapping")
        missing = {
            "session_id",
            "input_turn_id",
            "input_order",
            "stimulus_revision",
        } - set(identity)
        if missing:
            raise ValueError(f"identity is missing fields: {sorted(missing)}")
        input_order = identity["input_order"]
        stimulus_revision = identity["stimulus_revision"]
        if isinstance(input_order, bool) or not isinstance(input_order, int):
            raise ValueError("input_order must be an integer")
        if isinstance(stimulus_revision, bool) or not isinstance(
            stimulus_revision, int
        ):
            raise ValueError("stimulus_revision must be an integer")
        if input_order < 0:
            raise ValueError("input_order must be greater than or equal to 0")
        if stimulus_revision < 0:
            raise ValueError("stimulus_revision must be greater than or equal to 0")
        normalized = {
            "session_id": str(identity["session_id"]),
            "input_turn_id": str(identity["input_turn_id"]),
            "input_order": input_order,
            "stimulus_revision": stimulus_revision,
        }
        return (
            (
                normalized["session_id"],
                normalized["input_turn_id"],
                input_order,
                stimulus_revision,
            ),
            normalized,
        )
