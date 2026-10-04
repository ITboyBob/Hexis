from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from services.voice_input_authorization import (
    AuthorizationConflict,
    CaptureRange,
    FinalTranscript,
    InputAuthorizationService,
    InputIdentity,
    validate_final,
)

pytestmark = [pytest.mark.asyncio(loop_scope="function")]


def identity():
    return InputIdentity(
        session_id=uuid4(),
        input_turn_id=uuid4(),
        input_order=0,
        stimulus_revision=0,
    )


def capture(value):
    return CaptureRange(
        session_id=value.session_id,
        media_session_id="media-1",
        capture_instance_id=uuid4(),
        start_frame=100,
        end_frame=300,
        start_monotonic_ms=1_000,
        end_monotonic_ms=2_000,
        audio_sha256="a" * 64,
    )


def final(value, source):
    return FinalTranscript(
        identity=value,
        capture_instance_id=source.capture_instance_id,
        media_session_id=source.media_session_id,
        item_id="item-1",
        transcript="请帮我安排明天的会议",
        transcript_version=1,
        is_final=True,
    )


async def test_rejects_partial_empty_and_mismatched_final():
    value = identity()
    source = capture(value)
    bound = final(value, source)
    other = capture(value)

    with pytest.raises(ValueError, match="final 转录"):
        validate_final(bound.model_copy(update={"is_final": False}), source)
    with pytest.raises(ValueError, match="转录不能为空"):
        validate_final(bound.model_copy(update={"transcript": "  "}), source)
    with pytest.raises(ValueError, match="采集实例"):
        validate_final(bound.model_copy(update={"capture_instance_id": other.capture_instance_id}), source)
    with pytest.raises(ValueError, match="媒体会话"):
        validate_final(bound.model_copy(update={"media_session_id": "media-2"}), source)
    with pytest.raises(ValueError, match="会话"):
        validate_final(bound.model_copy(update={"identity": other_identity()}), source)


def other_identity():
    return InputIdentity(
        session_id=uuid4(),
        input_turn_id=uuid4(),
        input_order=0,
        stimulus_revision=0,
    )


async def test_same_range_commits_once_and_exact_retry_deduplicates():
    service = InputAuthorizationService()
    value = identity()
    source = capture(value)
    bound = final(value, source)

    first = await service.commit(bound, source)
    retry = await service.commit(bound, source)

    assert first.deduplicated is False
    assert retry.deduplicated is True
    assert first.event_id == retry.event_id
    assert first.identity == value
    assert first.range_key == retry.range_key


async def test_same_range_with_changed_content_fails_loud():
    service = InputAuthorizationService()
    value = identity()
    source = capture(value)
    await service.commit(final(value, source), source)

    changed = final(value, source).model_copy(update={"transcript": "不同内容"})
    with pytest.raises(AuthorizationConflict, match="同一采集范围"):
        await service.commit(changed, source)


async def test_concurrent_same_range_has_one_winner():
    service = InputAuthorizationService()
    value = identity()
    source = capture(value)
    bound = final(value, source)
    changed = bound.model_copy(update={"transcript": "竞争内容"})

    results = await asyncio.gather(
        service.commit(bound, source),
        service.commit(changed, source),
        return_exceptions=True,
    )
    winners = [item for item in results if not isinstance(item, BaseException)]
    conflicts = [item for item in results if isinstance(item, AuthorizationConflict)]

    assert len(winners) == 1
    assert len(conflicts) == 1
    assert winners[0].deduplicated is False
