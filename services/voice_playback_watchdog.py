"""Host-clock watchdog for genuinely scheduled native voice playback."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import UUID


@dataclass(frozen=True)
class PlaybackWatchIdentity:
    session_id: UUID
    media_session_id: str
    response_id: str
    playback_id: str
    output_epoch: int


@dataclass
class _Watch:
    identity: PlaybackWatchIdentity
    deadline_ms: float
    last_seq: int
    played_ms: int
    scheduled_ms: int
    on_timeout: Callable[[PlaybackWatchIdentity, int], Awaitable[None]]
    task: asyncio.Task[Any] | None = None


def _key(identity: PlaybackWatchIdentity) -> tuple[Any, ...]:
    return (
        identity.session_id,
        identity.media_session_id,
        identity.response_id,
        identity.playback_id,
        identity.output_epoch,
    )


class VoicePlaybackWatchdog:
    """Refresh only on ordered playback progress bounded by emitted PCM."""

    def __init__(
        self,
        *,
        timeout_ms: int = 5000,
        now_ms: Callable[[], float] | None = None,
    ) -> None:
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
            raise ValueError("timeout_ms must be a positive integer")
        self.timeout_ms = timeout_ms
        self._now_ms = now_ms or (lambda: time.monotonic() * 1000)
        self._watches: dict[tuple[Any, ...], _Watch] = {}

    def open(
        self,
        identity: PlaybackWatchIdentity,
        *,
        producer_seq: int,
        on_timeout: Callable[[PlaybackWatchIdentity, int], Awaitable[None]],
    ) -> None:
        if producer_seq <= 0:
            raise ValueError("producer_seq must be positive")
        key = _key(identity)
        existing = self._watches.get(key)
        if existing is not None:
            if existing.last_seq == producer_seq:
                return
            raise ValueError("playback watchdog is already open")
        watch = _Watch(
            identity=identity,
            deadline_ms=self._now_ms() + self.timeout_ms,
            last_seq=producer_seq,
            played_ms=0,
            scheduled_ms=0,
            on_timeout=on_timeout,
        )
        self._watches[key] = watch
        watch.task = asyncio.create_task(self._run(key, watch))

    def progress(
        self,
        identity: PlaybackWatchIdentity,
        *,
        producer_seq: int,
        played_ms: int,
        scheduled_ms: int,
    ) -> bool:
        watch = self._watches.get(_key(identity))
        if watch is None:
            return False
        if producer_seq <= watch.last_seq:
            return False
        if (
            isinstance(played_ms, bool)
            or not isinstance(played_ms, int)
            or played_ms < watch.played_ms
            or isinstance(scheduled_ms, bool)
            or not isinstance(scheduled_ms, int)
            or scheduled_ms < watch.scheduled_ms
            or played_ms > scheduled_ms
        ):
            return False
        watch.last_seq = producer_seq
        watch.played_ms = played_ms
        watch.scheduled_ms = scheduled_ms
        watch.deadline_ms = self._now_ms() + self.timeout_ms
        return True

    def terminal(self, identity: PlaybackWatchIdentity) -> bool:
        watch = self._watches.pop(_key(identity), None)
        if watch is None:
            return False
        if watch.task is not None and watch.task is not asyncio.current_task():
            watch.task.cancel()
        return True

    async def _run(self, key: tuple[Any, ...], watch: _Watch) -> None:
        while self._watches.get(key) is watch:
            remaining_ms = watch.deadline_ms - self._now_ms()
            if remaining_ms > 0:
                await asyncio.sleep(remaining_ms / 1000)
                continue
            self._watches.pop(key, None)
            await watch.on_timeout(watch.identity, watch.last_seq + 1)
            return


__all__ = ["PlaybackWatchIdentity", "VoicePlaybackWatchdog"]
