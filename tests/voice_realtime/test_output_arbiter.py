from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from services.voice_output_arbiter import (
    OutputClaimConflict,
    VoiceOutputArbiter,
)

pytestmark = [pytest.mark.asyncio(loop_scope="function")]


async def test_competing_claims_allow_exactly_one_owner():
    arbiter = VoiceOutputArbiter(session_id=uuid4())

    results = await asyncio.gather(
        arbiter.claim("resp-1", "media-1"),
        arbiter.claim("resp-1", "media-1"),
        return_exceptions=True,
    )

    successes = [value for value in results if not isinstance(value, BaseException)]
    conflicts = [value for value in results if isinstance(value, OutputClaimConflict)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert successes[0].output_epoch == 1


async def test_claim_rejects_wrong_media_and_release_enables_next_epoch():
    arbiter = VoiceOutputArbiter(session_id=uuid4())
    first = await arbiter.claim("resp-1", "media-1")

    with pytest.raises(OutputClaimConflict, match="media"):
        await arbiter.claim("resp-1", "media-2")
    with pytest.raises(OutputClaimConflict):
        await arbiter.claim("resp-1", "media-1")

    await arbiter.release(first.response_id, first.media_session_id)
    second = await arbiter.claim("resp-2", "media-1")

    assert second.output_epoch == 2
