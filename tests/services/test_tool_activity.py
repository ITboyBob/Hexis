"""Tool Activity formatter preserves the existing text-chat presentation contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.tool_activity import format_tool_result, format_tool_start


FIXTURE = Path(__file__).parents[1] / "fixtures" / "tool_activity.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _case(case_id: str) -> dict:
    return next(case for case in _fixture()["agent_event_cases"] if case["id"] == case_id)


def test_tool_start_keeps_existing_shape_and_limits_arguments_to_500_characters() -> None:
    data = _case("long-request")["agent_events"][0]["data"]
    result = format_tool_start(data, event_id="event-start")

    assert result == {
        "id": "event-start",
        "kind": "tool_call",
        "title": "recall",
        "detail": json.dumps(data["arguments"])[:500],
    }
    assert len(result["detail"]) == 500


def test_regular_result_keeps_display_output_and_1000_character_summary() -> None:
    data = _case("scalar-output")["agent_events"][1]["data"]
    data["display_output"] = "R" * 1200
    result = format_tool_result(data, event_id="event-result")

    assert result["id"] == "event-result"
    assert result["title"] == "read_file"
    assert result["detail"] == "R" * 1000
    assert result["display_output"] == "R" * 1200


def test_recall_keeps_full_text_and_existing_empty_or_malformed_messages() -> None:
    long_case = _case("recall-long")["agent_events"][1]["data"]
    long_result = format_tool_result(long_case, event_id="long-recall")
    assert long_result["detail"] == "1. " + long_case["output"]["memories"][0]["content"]
    assert len(long_result["detail"]) > 1000

    empty = format_tool_result(_case("recall-empty")["agent_events"][1]["data"], event_id="empty")
    malformed = format_tool_result(_case("recall-malformed")["agent_events"][1]["data"], event_id="malformed")
    assert empty["detail"] == "未召回记忆"
    assert malformed["detail"] == "Recall 返回结构异常：无法读取记忆正文"


def test_tool_failure_uses_the_actual_error() -> None:
    data = _case("file-failure")["agent_events"][1]["data"]
    result = format_tool_result(data, event_id="failed")
    assert result["detail"] == f"FAILED: {data['error']}"


def test_lightweight_receipt_state_is_authoritative_for_activity_visibility() -> None:
    accepted = format_tool_result(
        {"tool_name": "remember", "success": True, "output": {}},
        event_id="accepted",
        state="accepted",
    )
    assert accepted is None

    unknown = format_tool_result(
        {"tool_name": "remember", "success": False, "error": None},
        event_id="unknown",
        state="unknown",
    )
    assert unknown == {
        "id": "unknown",
        "kind": "tool_result",
        "title": "remember",
        "detail": "工具调用结果无法确认。",
        "status_label": "结果未确认",
        "state": "unknown",
    }


def test_voice_recall_preserves_per_memory_limit_without_text_result_truncation() -> None:
    data = {
        "tool_name": "voice_recall",
        "success": True,
        "output": {"status": "found", "memories": [{"content": "V" * 2100}]},
    }
    result = format_tool_result(data, event_id="voice-result")
    assert result["title"] == "voice_recall"
    assert result["detail"] == "1. " + ("V" * 2000)


@pytest.mark.parametrize("success", [False, True])
@pytest.mark.parametrize("status", ["error", "failed"])
def test_voice_recall_failure_shows_the_real_error_from_its_output(success: bool, status: str) -> None:
    result = format_tool_result(
        {
            "tool_name": "voice_recall",
            "success": success,
            "output": {"status": status, "error": {"code": "VOICE_RECALL_FAILED", "message": "synthetic read-only failure"}},
        },
        event_id="voice-error",
    )
    assert result["detail"] == "FAILED: synthetic read-only failure"


def test_unknown_receipt_appends_only_a_real_error_reason() -> None:
    result = format_tool_result(
        {"tool_name": "add_evidence", "success": False, "error": {"message": "reason"}},
        event_id="unknown-error",
        state="unknown",
    )
    assert result is not None
    assert result["detail"] == "工具调用结果无法确认。\n原因：reason"
