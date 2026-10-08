from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core.conversation_messages import (
    ConversationMessageError,
    internal_to_provider,
    normalize_history,
    project_memory_texts,
    provider_to_internal,
    validate_new_message,
)


FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "conversation_tool_messages.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
FIXTURE_SESSION_ID = "10000000-0000-4000-8000-000000000001"
FIXTURE_TURN_ID = "20000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["id"])
def test_fixture_message_contract_and_memory_projection(case):
    result = normalize_history(
        case["messages"],
        session_id=FIXTURE_SESSION_ID,
    )
    expected = case["expected"]

    assert result["valid"] is expected["valid"]
    if not expected["valid"]:
        assert result["error_category"] == expected["error_category"]
        assert result["replay_allowed"] is False
        return

    if expected.get("historical_incomplete"):
        assert result["historical_incomplete"] is True
        assert result["replay_allowed"] is False

    memory_texts = project_memory_texts(result["messages"])
    if "memory_texts" in expected:
        assert memory_texts == expected["memory_texts"]
    for forbidden in expected.get("memory_excludes", []):
        assert all(forbidden not in text for text in memory_texts)

    if "provider_roles" in expected:
        provider_messages, diagnostics = internal_to_provider(
            result["messages"], session_id=FIXTURE_SESSION_ID
        )
        assert [message["role"] for message in provider_messages] == expected["provider_roles"]
        assert not diagnostics
        assert all("metadata" not in message for message in provider_messages)
        assert "BOB107_工具结构哨兵" not in json.dumps(provider_messages, ensure_ascii=False)

    if "empty_text_bubble" in expected:
        provider_messages, _ = internal_to_provider(
            result["messages"], session_id=FIXTURE_SESSION_ID
        )
        assert expected["empty_text_bubble"] is False
        assert not any(message.get("content") == "" for message in provider_messages)


def test_internal_provider_roundtrip_uses_explicit_result_authority():
    case = next(item for item in FIXTURE["cases"] if item["id"] == "details-object")
    original = copy.deepcopy(case["messages"])
    provider_messages, diagnostics = internal_to_provider(
        original,
        session_id=FIXTURE_SESSION_ID,
    )

    assert not diagnostics
    tool_call = provider_messages[1]["tool_calls"][0]
    rebuilt, rebuild_diagnostics = provider_to_internal(
        provider_messages,
        session_id=FIXTURE_SESSION_ID,
        turn_id=FIXTURE_TURN_ID,
        tool_results={
            tool_call["id"]: {
                "toolName": "read_file",
                "isError": False,
                "details": {"project": "Hexis", "sentinel": "BOB107_工具结构哨兵"},
            }
        },
    )

    assert not rebuild_diagnostics
    assert original == case["messages"]
    assert rebuilt[1]["content_blocks"][0]["id"] == tool_call["id"]
    assert rebuilt[2]["metadata"]["tool_result"]["details"]["sentinel"] == "BOB107_工具结构哨兵"
    assert rebuilt[2]["metadata"]["tool_result"]["isError"] is False
    assert rebuilt[1]["source_message_id"] == f"tool:{FIXTURE_TURN_ID}:{tool_call['id']}:call"
    assert rebuilt[2]["source_message_id"] == f"tool:{FIXTURE_TURN_ID}:{tool_call['id']}:result"


def test_provider_arguments_must_decode_to_json_object():
    provider_messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "provider-call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "[]"},
                }
            ],
        }
    ]

    with pytest.raises(ConversationMessageError) as error:
        provider_to_internal(
            provider_messages,
            session_id=FIXTURE_SESSION_ID,
            turn_id=FIXTURE_TURN_ID,
        )
    assert error.value.category == "invalid_tool_arguments"


@pytest.mark.parametrize("case_id", ["m5-accepted", "m5-pending", "m5-selection_cancelled"])
def test_native_m5_receipt_roundtrip_preserves_all_eight_fields(case_id):
    case = next(item for item in FIXTURE["cases"] if item["id"] == case_id)
    normalized = normalize_history(case["messages"], session_id=FIXTURE_SESSION_ID)
    provider_messages, diagnostics = internal_to_provider(
        normalized["messages"], session_id=FIXTURE_SESSION_ID
    )
    rebuilt, rebuild_diagnostics = provider_to_internal(
        provider_messages,
        session_id=FIXTURE_SESSION_ID,
        turn_id=FIXTURE_TURN_ID,
    )

    assert not diagnostics
    assert not rebuild_diagnostics
    original_receipt = json.loads(next(message["content"] for message in case["messages"] if message["role"] == "toolResult"))
    restored = next(message for message in rebuilt if message["role"] == "toolResult")
    assert restored["metadata"]["tool_result"]["details"] == original_receipt
    assert restored["metadata"]["tool_result"]["isError"] is (not original_receipt["ok"])
    assert set(restored["metadata"]["tool_result"]["details"]) == {
        "contract_version", "tool_name", "call_id", "operation_id", "ok", "state", "data", "error"
    }


def test_provider_text_result_without_execution_authority_is_not_persisted():
    provider_messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "provider-call-2",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{\"path\":\"README.md\"}"},
            }],
        },
        {"role": "tool", "tool_call_id": "provider-call-2", "content": "读取失败：文件不存在"},
    ]

    internal, diagnostics = provider_to_internal(
        provider_messages,
        session_id=FIXTURE_SESSION_ID,
        turn_id=FIXTURE_TURN_ID,
    )

    assert [message["role"] for message in internal] == ["assistant"]
    assert diagnostics[0]["code"] == "provider_tool_result_missing_authority"


def test_provider_tool_call_requires_a_real_agent_turn_id():
    provider_messages = [{
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "provider-call-3",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }],
    }]

    with pytest.raises(ConversationMessageError) as error:
        provider_to_internal(provider_messages, session_id=FIXTURE_SESSION_ID)
    assert error.value.category == "invalid_turn_identity"


@pytest.mark.parametrize("complete", [True, False])
@pytest.mark.parametrize("include_visual", [True, False])
def test_parallel_provider_calls_split_for_durable_identity_and_regroup_for_provider(complete, include_visual):
    provider_messages = [
        {
            "role": "assistant",
            "content": "I will inspect both files.",
            "tool_calls": [
                {"id": "parallel-1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\":\"README.md\"}"}},
                {"id": "parallel-2", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\":\"LICENSE\"}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "parallel-1", "content": "Hexis"},
        {"role": "tool", "tool_call_id": "parallel-2", "content": "MIT"},
    ]
    authority = {
        "parallel-1": {"toolName": "read_file", "isError": False, "details": {"path": "README.md"}},
        "parallel-2": {"toolName": "read_file", "isError": False, "details": {"path": "LICENSE"}},
    }
    if include_visual:
        authority["parallel-1"]["model_visual_attachments"] = [{
            "data_url": "data:image/png;base64,aGVsbG8=", "mime_type": "image/png", "name": "first result image",
        }]
    if not complete:
        provider_messages.pop()

    internal, diagnostics = provider_to_internal(
        provider_messages,
        session_id=FIXTURE_SESSION_ID,
        turn_id=FIXTURE_TURN_ID,
        tool_results=authority,
    )
    regrouped, regroup_diagnostics = internal_to_provider(
        internal, session_id=FIXTURE_SESSION_ID
    )

    assert not diagnostics
    durable_calls = [
        message for message in internal
        if message["role"] == "assistant" and message["content_blocks"]
        and any(block["type"] == "toolCall" for block in message["content_blocks"])
    ]
    assert len(durable_calls) == 2
    assert [message["source_message_id"] for message in durable_calls] == [
        f"tool:{FIXTURE_TURN_ID}:parallel-1:call",
        f"tool:{FIXTURE_TURN_ID}:parallel-2:call",
    ]
    assert all(sum(block["type"] == "toolCall" for block in message["content_blocks"]) == 1 for message in durable_calls)
    assert project_memory_texts(internal) == ["I will inspect both files."]
    for message in internal:
        validate_new_message(message, session_id=FIXTURE_SESSION_ID)
    if not complete:
        assert any(item["code"] == "tool_pair_incomplete" for item in regroup_diagnostics)
        assert all("tool_calls" not in message and message["role"] != "tool" for message in regrouped)
        return
    assert not regroup_diagnostics
    assert [message["source_message_id"] for message in internal if message["role"] == "toolResult"] == [
        f"tool:{FIXTURE_TURN_ID}:parallel-1:result",
        f"tool:{FIXTURE_TURN_ID}:parallel-2:result",
    ]
    assert [message["role"] for message in regrouped] == ["assistant", "tool", "tool"] + (["user"] if include_visual else [])
    assert len(regrouped[0]["tool_calls"]) == 2
    assert regrouped[0]["content"] == "I will inspect both files."


@pytest.mark.parametrize("boundary", ["session", "turn", "user"])
def test_provider_regrouping_respects_session_turn_and_adjacency(boundary):
    calls, results = [], []
    for index in range(2):
        session = "10000000-0000-4000-8000-000000000002" if boundary == "session" and index else FIXTURE_SESSION_ID
        turn = "20000000-0000-4000-8000-000000000002" if boundary == "turn" and index else FIXTURE_TURN_ID
        call_id = f"boundary-{index}"
        calls.append({
            "role": "assistant", "session_id": session, "content": "",
            "content_blocks": [{"type": "toolCall", "id": call_id, "name": "read_file", "arguments": {}}],
            "metadata": {"agent_turn_id": turn}, "source_message_id": f"tool:{turn}:{call_id}:call",
        })
        results.append({
            "role": "toolResult", "session_id": session, "content": "done", "content_blocks": [],
            "metadata": {"agent_turn_id": turn, "tool_result": {
                "toolCallId": call_id, "toolName": "read_file", "isError": False, "details": None,
            }}, "source_message_id": f"tool:{turn}:{call_id}:result",
        })
    source = [calls[0], calls[1], *results]
    if boundary == "user":
        source.insert(1, {"role": "user", "content": "pause", "content_blocks": [], "metadata": {}})

    provider, diagnostics = internal_to_provider(source, session_id=FIXTURE_SESSION_ID)

    assert not diagnostics
    assert [len(message["tool_calls"]) for message in provider if "tool_calls" in message] == [1, 1]


def test_provider_split_rejects_duplicate_call_identity_within_one_message():
    call = {"id": "duplicate-call", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
    with pytest.raises(ConversationMessageError) as error:
        provider_to_internal(
            [{"role": "assistant", "content": None, "tool_calls": [call, copy.deepcopy(call)]}],
            session_id=FIXTURE_SESSION_ID, turn_id=FIXTURE_TURN_ID,
        )
    assert error.value.category == "duplicate_call_id"


def test_tool_visual_attachment_uses_existing_agent_visual_message_shape():
    case = next(item for item in FIXTURE["cases"] if item["id"] == "details-object")
    messages = copy.deepcopy(case["messages"])
    result = next(message for message in messages if message["role"] == "toolResult")
    data_url = "data:image/png;base64,aGVsbG8="
    result["metadata"]["model_visual_attachments"] = [
        {"data_url": data_url, "mime_type": "image/png", "name": "fixture image"}
    ]

    provider_messages, diagnostics = internal_to_provider(
        messages, session_id=FIXTURE_SESSION_ID
    )

    assert not diagnostics
    visual = provider_messages[3]
    assert visual == {
        "role": "user",
        "content": [
            {"type": "input_text", "text": "[Fresh tool-provided visual context: fixture image]"},
            {"type": "input_image", "image_url": data_url, "detail": "auto"},
        ],
    }


def test_new_messages_require_array_blocks_source_and_real_turn_for_tools():
    base_tool_call = {
        "role": "assistant",
        "content": "",
        "content_blocks": [{"type": "toolCall", "id": "call-1", "name": "read_file", "arguments": {"path": "README.md"}}],
        "metadata": {"agent_turn_id": FIXTURE_TURN_ID},
        "source_message_id": f"tool:{FIXTURE_TURN_ID}:call-1:call",
    }

    assert validate_new_message(base_tool_call, session_id=FIXTURE_SESSION_ID)["role"] == "assistant"
    for changed, category in (
        ({**base_tool_call, "content_blocks": None}, "invalid_content_blocks"),
        ({**base_tool_call, "source_message_id": None}, "invalid_source_identity"),
        ({**base_tool_call, "metadata": {}}, "invalid_turn_identity"),
    ):
        with pytest.raises(ConversationMessageError) as error:
            validate_new_message(changed, session_id=FIXTURE_SESSION_ID)
        assert error.value.category == category
