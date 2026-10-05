from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from services.avatar_behavior_import import import_avatar_preview_evidence
from services.behavior_events import query_behavior_events

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


def _page_event(event_id: str, session_id, payload, stream="preview-local", seq=0):
    """One event shaped exactly like the preview page dispatches it."""
    return {
        "event_id": event_id,
        "session_id": str(session_id),
        "stream_id": stream,
        "producer_seq": seq,
        "payload": payload,
    }


def _evidence(session_id=None):
    session_id = session_id or uuid4()
    identity = {
        "session_id": str(session_id),
        "input_turn_id": str(uuid4()),
        "input_order": 0,
        "stimulus_revision": 0,
    }
    return {
        "schema_version": "avatar-preview-evidence-v1",
        "session_id": str(session_id),
        "events": [
            {
                "event_id": str(uuid4()),
                "session_id": str(session_id),
                "stream_id": "input",
                "producer_seq": 0,
                "payload": {"type": "input_committed", "identity": identity},
            },
            {
                "event_id": str(uuid4()),
                "session_id": str(session_id),
                "stream_id": "task",
                "producer_seq": 0,
                "payload": {
                    "type": "task",
                    "task": {
                        "activity_id": str(uuid4()),
                        "task_id": str(uuid4()),
                        "kind": "tool",
                        "foreground": False,
                        "phase": "running",
                        "source": "real",
                    },
                },
            },
            {
                "event_id": "   ",
                "session_id": str(session_id),
                "stream_id": "bad",
                "producer_seq": 0,
                "payload": {"type": "control", "action": "reset"},
            },
        ],
    }


async def test_import_avatar_preview_evidence_maps_and_reports_rejections(db_pool):
    evidence = _evidence()
    summary = await import_avatar_preview_evidence(db_pool, evidence)
    rows = await query_behavior_events(
        db_pool, session_id=UUID(evidence["session_id"]), limit=10
    )

    assert summary["accepted"] == 2
    assert summary["deduplicated"] == 0
    assert summary["rejected"] == 1
    assert summary["rejections"][0]["reason"] == (
        "event_id must be a UUID or a non-empty trimmed string"
    )
    assert {row["event_type"] for row in rows} == {
        "avatar.input_committed",
        "avatar.task",
    }
    assert all(row["turn_id"] is None for row in rows)
    assert all(row["source"] == "avatar-preview" for row in rows)


async def test_retrying_avatar_import_deduplicates_without_new_rows(db_pool):
    evidence = _evidence()
    evidence["events"] = evidence["events"][:2]
    first = await import_avatar_preview_evidence(db_pool, evidence)
    retry = await import_avatar_preview_evidence(db_pool, evidence)
    rows = await query_behavior_events(
        db_pool, session_id=UUID(evidence["session_id"]), limit=10
    )

    assert first["accepted"] == 2
    assert retry["accepted"] == 0
    assert retry["deduplicated"] == 2
    assert retry["rejected"] == 0
    assert len(rows) == 2


async def test_real_page_event_ids_import_and_retry_deduplicates(db_pool):
    """The page's preview-event-<seq>-<uuid> ids import and retry-dedup."""
    session_id = uuid4()
    evidence = {
        "schema_version": "avatar-preview-evidence-v1",
        "session_id": str(session_id),
        "events": [
            _page_event(
                f"preview-event-{seq}-{uuid4()}",
                session_id,
                {"type": "control", "action": "reset"},
                seq=seq,
            )
            for seq in range(1, 4)
        ],
    }

    first = await import_avatar_preview_evidence(db_pool, evidence)
    retry = await import_avatar_preview_evidence(db_pool, evidence)
    rows = await query_behavior_events(db_pool, session_id=session_id, limit=10)

    assert first["accepted"] == 3
    assert first["rejected"] == 0
    assert retry["accepted"] == 0
    assert retry["deduplicated"] == 3
    assert retry["rejected"] == 0
    assert len(rows) == 3
    assert {row["event_type"] for row in rows} == {"avatar.control"}
    # Derived ids stay stable UUIDs across retries of the same export.
    assert all(UUID(row["event_id"]) for row in rows)


async def test_playback_events_map_envelope_identity_columns(db_pool):
    """playback_open epoch and playback feedback ids land on envelope columns."""
    session_id = uuid4()
    evidence = {
        "schema_version": "avatar-preview-evidence-v1",
        "session_id": str(session_id),
        "events": [
            _page_event(
                "preview-event-1-{}".format(uuid4()),
                session_id,
                {
                    "type": "playback_open",
                    "playback_id": "play-1",
                    "output_epoch": 7,
                    "source": "real",
                },
                seq=1,
            ),
            _page_event(
                "preview-event-2-{}".format(uuid4()),
                session_id,
                {
                    "type": "playback",
                    "feedback": {
                        "playback_id": "play-1",
                        "output_epoch": 7,
                        "response_id": "resp-1",
                        "media_session_id": "media-1",
                        "phase": "playing",
                        "progress": 0.5,
                        "played_ms": 500,
                        "interval_ms": 0,
                        "amplitude": 0.4,
                        "source": "real",
                    },
                },
                seq=2,
            ),
            _page_event(
                "preview-event-3-{}".format(uuid4()),
                session_id,
                {
                    "type": "playback",
                    "feedback": {
                        "playback_id": "play-1",
                        "output_epoch": 7,
                        "phase": "drained",
                        "progress": 1.0,
                        "played_ms": 1000,
                        "interval_ms": 0,
                        "amplitude": 0,
                        "source": "real",
                    },
                },
                seq=3,
            ),
        ],
    }

    summary = await import_avatar_preview_evidence(db_pool, evidence)
    rows = sorted(
        await query_behavior_events(db_pool, session_id=session_id, limit=10),
        key=lambda row: row["producer_seq"],
    )

    assert summary["accepted"] == 3
    assert summary["rejected"] == 0
    # playback_open keeps its epoch at the payload top level, mapped to the
    # envelope column.
    assert rows[0]["event_type"] == "avatar.playback_open"
    assert rows[0]["output_epoch"] == 7
    # playback feedback maps its identifiers onto the envelope; absent ones
    # stay NULL instead of being fabricated.
    assert rows[1]["event_type"] == "avatar.playback"
    assert rows[1]["output_epoch"] == 7
    assert rows[1]["response_id"] == "resp-1"
    assert rows[1]["media_session_id"] == "media-1"
    assert rows[2]["event_type"] == "avatar.playback"
    assert rows[2]["output_epoch"] == 7
    assert rows[2]["response_id"] is None
    assert rows[2]["media_session_id"] is None
