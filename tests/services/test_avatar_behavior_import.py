from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from services.avatar_behavior_import import import_avatar_preview_evidence
from services.behavior_events import query_behavior_events

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


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
                "event_id": "not-a-uuid",
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
    assert summary["rejections"][0]["reason"] == "event_id must be a UUID"
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
