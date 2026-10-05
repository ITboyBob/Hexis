from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from services.voice_playback_watchdog import PlaybackWatchIdentity, VoicePlaybackWatchdog


@pytest.mark.asyncio
async def test_watchdog_expires_after_configured_timeout_without_usable_progress():
    watchdog = VoicePlaybackWatchdog(timeout_ms=30)
    identity = PlaybackWatchIdentity(uuid4(), "media-1", "resp-1", "play-1", 1)
    expired = asyncio.Event()
    facts = []

    async def on_timeout(value, seq):
        facts.append((value, seq))
        expired.set()

    watchdog.open(identity, producer_seq=1, on_timeout=on_timeout)
    await asyncio.wait_for(expired.wait(), timeout=0.2)

    assert facts == [(identity, 2)]
    assert watchdog.terminal(identity) is False


@pytest.mark.asyncio
async def test_valid_progress_renews_but_duplicate_or_out_of_bounds_does_not():
    watchdog = VoicePlaybackWatchdog(timeout_ms=40)
    identity = PlaybackWatchIdentity(uuid4(), "media-1", "resp-2", "play-2", 2)
    expired = asyncio.Event()

    async def on_timeout(_identity, _seq):
        expired.set()

    watchdog.open(identity, producer_seq=1, on_timeout=on_timeout)
    await asyncio.sleep(0.025)
    assert watchdog.progress(identity, producer_seq=2, played_ms=10, scheduled_ms=20)
    assert not watchdog.progress(identity, producer_seq=2, played_ms=10, scheduled_ms=20)
    assert not watchdog.progress(identity, producer_seq=3, played_ms=21, scheduled_ms=20)
    await asyncio.sleep(0.025)
    assert not expired.is_set()
    await asyncio.wait_for(expired.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_terminal_closes_watch_and_long_playback_can_keep_renewing():
    watchdog = VoicePlaybackWatchdog(timeout_ms=40)
    identity = PlaybackWatchIdentity(uuid4(), "media-1", "resp-3", "play-3", 3)
    expired = asyncio.Event()

    async def on_timeout(_identity, _seq):
        expired.set()

    watchdog.open(identity, producer_seq=1, on_timeout=on_timeout)
    for seq in range(2, 6):
        await asyncio.sleep(0.02)
        assert watchdog.progress(identity, producer_seq=seq, played_ms=seq * 10, scheduled_ms=1000)
    assert watchdog.terminal(identity)
    await asyncio.sleep(0.06)
    assert not expired.is_set()
