"""The checked-in M5 tool artifact must match its approved source exactly."""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

import pytest

_SOURCE = Path(
    "/Users/itboybob/Project/geek_interview/docs/M5-工具描述与JSON-Schema契约.md"
)


def _source_contract():
    text = _SOURCE.read_text(encoding="utf-8")
    definitions = []
    for index in range(1, 6):
        section = re.search(
            rf"### 3\.{index} [^\n]+\n(.*?)(?=^### 3\.|^## 4．)",
            text,
            re.MULTILINE | re.DOTALL,
        )
        assert section is not None
        fenced = re.search(r"```json\s*(.*?)\s*```", section.group(1), re.DOTALL)
        assert fenced is not None
        definitions.append(json.loads(fenced.group(1)))
    receipt_section = re.search(
        r"^## 5．统一回执与供应商结果封装\s*$(.*?)(?=^## 6．)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert receipt_section is not None
    receipt_fence = re.search(
        r"```json\s*(.*?)\s*```", receipt_section.group(1), re.DOTALL
    )
    assert receipt_fence is not None
    return {
        "contract_version": "m5-tool-v1",
        "tools": definitions,
        "receipt_schema": json.loads(receipt_fence.group(1)),
    }


def test_artifact_matches_all_five_original_definitions_and_receipt_schema():
    from services import m5_tool_contracts

    contract = m5_tool_contracts.load_contract()

    assert contract == _source_contract()
    assert [item["function"]["name"] for item in contract["tools"]] == [
        "submit",
        "pend_message",
        "report_task_progress",
        "remember",
        "add_evidence",
    ]
    assert m5_tool_contracts.FRONTEND_TOOL_NAMES == {
        "submit",
        "pend_message",
        "remember",
        "add_evidence",
    }
    assert m5_tool_contracts.REPORT_TOOL_NAMES == {"report_task_progress"}


@pytest.mark.parametrize(
    "raw",
    [
        '{"task_description":"first","task_description":"second"}',
        '{"content":"fact","importance":NaN}',
        '{"content":"fact","importance":Infinity}',
        '{"task_description":"unfinished"',
        '  ',
    ],
)
def test_tool_arguments_reject_nonstandard_or_incomplete_json(raw):
    from services.m5_tool_contracts import parse_tool_arguments

    with pytest.raises(ValueError):
        parse_tool_arguments("submit" if "task_description" in raw else "remember", raw)


def test_tool_arguments_enforce_schema_identity_and_source_qualification():
    from services.m5_tool_contracts import parse_tool_arguments

    with pytest.raises(ValueError):
        parse_tool_arguments(
            "submit",
            json.dumps({"task_description": "work", "command_id": str(uuid4())}),
        )
    with pytest.raises(ValueError):
        parse_tool_arguments("submit", '{"task_description":" \t "}')
    with pytest.raises(ValueError, match="ref or label"):
        parse_tool_arguments(
            "remember",
            json.dumps({"content": "fact", "sources": [{"kind": "email"}]}),
        )
    with pytest.raises(ValueError, match="ref or label"):
        parse_tool_arguments(
            "add_evidence",
            json.dumps(
                {
                    "memory_id": str(uuid4()),
                    "stance": "supports",
                    "source": {"kind": "email"},
                }
            ),
        )

    parsed = parse_tool_arguments(
        "submit",
        json.dumps({"task_description": "  preserve this complete request  "}),
    )
    assert parsed["task_description"] == "  preserve this complete request  "


def test_receipt_requires_a_real_call_and_valid_business_state():
    from services.m5_tool_contracts import build_receipt

    call_id = f"call-{uuid4()}"
    receipt = build_receipt(
        tool_name="submit",
        call_id=call_id,
        operation_id=uuid4(),
        ok=True,
        state="accepted",
        data={
            "task_id": str(uuid4()),
            "task_title": "Prepare a plan",
            "task_state": "working",
        },
        error=None,
    )
    assert receipt["call_id"] == call_id
    assert receipt["state"] == "accepted"
    assert set(receipt) == {
        "contract_version",
        "tool_name",
        "call_id",
        "operation_id",
        "ok",
        "state",
        "data",
        "error",
    }

    with pytest.raises(ValueError, match="call_id"):
        build_receipt(
            tool_name="submit",
            call_id=" ",
            operation_id=None,
            ok=False,
            state="failed",
            data={},
            error={
                "code": "MISSING_TOOL_CALL",
                "message": "The call id is unavailable.",
                "retryable": False,
            },
        )
    with pytest.raises(ValueError):
        build_receipt(
            tool_name="submit",
            call_id=call_id,
            operation_id=None,
            ok=True,
            state="accepted",
            data={},
            error=None,
        )
