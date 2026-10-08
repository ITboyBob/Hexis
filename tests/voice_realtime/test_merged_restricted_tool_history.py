"""实库验证 S5 受限策略与 BOB-107 工具历史在合并后的边界。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from apps.hexis_api import _execute_restricted_voice_tool
from services.m5_tool_bridge import BoundToolCall
from services.voice_conversation_history import VoiceConversationHistory

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


async def _voice_call(pool, name, arguments):
    history = VoiceConversationHistory(pool)
    session_id, input_id = uuid4(), uuid4()
    turn_id = await history.start_turn(
        session_id=session_id, input_turn_id=input_id, input_order=1,
        stimulus_revision=0, user_text="合并验收的授权语音输入", context_version="merge-test",
    )
    session = SimpleNamespace(
        validate_voice_tool_call=Mock(),
        _voice_turns={str(input_id): {"turn_id": turn_id}},
    )
    call = BoundToolCall(
        actual_call_id=f"merge-call-{uuid4()}", command_id=uuid4(), name=name,
        raw_arguments=json.dumps(arguments), origin_session_id=session_id,
        input_turn_id=str(input_id), input_order=1, stimulus_revision=0,
        response_id="merge-response", supplier_response_id="supplier-response",
        request_id="merge-request", tool_surface="m5",
    )
    return history, session, call, session_id, turn_id


@pytest.mark.parametrize("name,arguments", [
    ("submit", {"task_description": "只读检查 README"}),
    ("voice_recall", {"query": "Hexis"}),
])
async def test_valid_restricted_denial_is_durable_and_replayed_without_business_effects(
    db_pool, monkeypatch, name, arguments,
):
    forbidden = AsyncMock(side_effect=AssertionError("业务执行不得发生"))
    monkeypatch.setattr("services.m5_task_service.M5TaskService.submit_m5_task", forbidden)
    monkeypatch.setattr("services.voice_recall.VoiceRecallService.execute", forbidden)
    history, session, call, session_id, turn_id = await _voice_call(db_pool, name, arguments)
    first = await _execute_restricted_voice_tool(session, call, history)
    second = await _execute_restricted_voice_tool(session, call, history)
    assert first == second
    receipt = json.loads(first)
    assert receipt["ok"] is False
    assert receipt["error"]["code"] == "PERMISSION_DENIED"
    forbidden.assert_not_called()
    rows = await db_pool.fetch(
        "SELECT role,content_blocks,metadata,source_message_id FROM chat_messages "
        "WHERE session_id=$1 ORDER BY ordinal", session_id,
    )
    assert [row["role"] for row in rows] == ["user", "assistant", "toolResult"]
    call_block = _decode(rows[1]["content_blocks"])[0]
    assert call_block == {"type": "toolCall", "id": call.actual_call_id, "name": name, "arguments": arguments}
    metadata = _decode(rows[2]["metadata"])
    assert metadata["agent_turn_id"] == str(turn_id)
    assert metadata["tool_result"]["details"] == receipt
    assert metadata["tool_result"]["isError"] is True
    assert rows[2]["source_message_id"] == f"tool:{turn_id}:{call.actual_call_id}:result"
    assert await db_pool.fetchval(
        "SELECT count(*) FROM agent_turn_events WHERE turn_id=$1 AND event_type='tool_result'",
        turn_id,
    ) == 1


@pytest.mark.parametrize("raw,code", [("not-json", "INVALID_JSON"), ("[]", "INVALID_JSON"), ('{"unexpected":true}', "INVALID_ARGUMENTS")])
async def test_malformed_restricted_call_preserves_schema_error_without_fabricating_history(raw, code):
    history = SimpleNamespace(register_tool_call=AsyncMock(side_effect=AssertionError("不能伪造调用")))
    session = SimpleNamespace(validate_voice_tool_call=Mock(), _voice_turns={})
    call = SimpleNamespace(name="voice_recall", raw_arguments=raw, actual_call_id="invalid-call")
    receipt = json.loads(await _execute_restricted_voice_tool(session, call, history))
    assert receipt["error"]["code"] == code
    history.register_tool_call.assert_not_called()


async def test_valid_denial_requires_real_turn(db_pool):
    history, session, call, _, _ = await _voice_call(db_pool, "voice_recall", {"query": "Hexis"})
    session._voice_turns = {}
    with pytest.raises(RuntimeError, match="real persisted AgentLoop turn"):
        await _execute_restricted_voice_tool(session, call, history)


async def test_denial_persistence_failure_is_not_returned_as_success(db_pool, monkeypatch):
    history, session, call, _, _ = await _voice_call(db_pool, "voice_recall", {"query": "Hexis"})
    monkeypatch.setattr(history, "record_tool_result", AsyncMock(side_effect=RuntimeError("injected write failure")))
    with pytest.raises(RuntimeError, match="injected write failure"):
        await _execute_restricted_voice_tool(session, call, history)
    with pytest.raises(RuntimeError, match="uncertain"):
        await _execute_restricted_voice_tool(session, call, history)
