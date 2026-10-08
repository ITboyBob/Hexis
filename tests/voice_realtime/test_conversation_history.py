from __future__ import annotations

import json
from uuid import uuid4

import pytest

from services.voice_conversation_history import VoiceConversationHistory

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


class _Transaction:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        self.pool.transaction_count += 1

    async def __aexit__(self, *_exc):
        return False


class _Connection:
    def __init__(self, pool):
        self.pool = pool

    def transaction(self):
        return _Transaction(self.pool)

    async def fetchval(self, sql, *args):
        self.pool.calls.append((sql, args))
        if "start_agent_turn" in sql:
            return json.dumps({"turn_id": str(self.pool.turn_id)})
        if "append_chat_message_blocks" in sql:
            return json.dumps({"message_id": str(uuid4()), "status": "stored"})
        return json.dumps({"status": "completed", "deduplicated": False})


class _Acquire:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        self.pool.acquire_count += 1
        return _Connection(self.pool)

    async def __aexit__(self, *_exc):
        return False


class _Pool:
    def __init__(self):
        self.turn_id = uuid4()
        self.acquire_count = 0
        self.transaction_count = 0
        self.calls = []

    def acquire(self):
        return _Acquire(self)


async def test_start_turn_uses_existing_agent_turn_and_voice_context():
    pool = _Pool()
    history = VoiceConversationHistory(pool)
    session_id, input_turn_id = uuid4(), uuid4()

    turn_id = await history.start_turn(
        session_id=session_id,
        input_turn_id=input_turn_id,
        input_order=4,
        stimulus_revision=2,
        user_text="  authorized words  ",
        context_version="context-v1",
    )

    input_sql, input_args = pool.calls[0]
    turn_sql, turn_args = pool.calls[1]
    input_metadata = json.loads(input_args[3])
    runtime_state = json.loads(turn_args[3])["runtime_state"]
    assert "append_chat_message_blocks" in input_sql
    assert input_args[0] == session_id
    assert input_args[1] == "  authorized words  "
    assert input_args[4] == f"voice:{session_id}:{input_turn_id}:2:user"
    assert input_metadata == {
        "channel": "voice",
        "input_turn_id": str(input_turn_id),
        "input_order": 4,
        "stimulus_revision": 2,
        "context_version": "context-v1",
    }
    assert "start_agent_turn" in turn_sql
    assert (turn_args[0], turn_args[1], turn_args[2]) == (
        "voice",
        "  authorized words  ",
        session_id,
    )
    assert runtime_state == {
        "channel": "voice",
        "input_turn_id": str(input_turn_id),
        "input_order": 4,
        "stimulus_revision": 2,
        "context_version": "context-v1",
        "chat_message_projection": True,
        "chat_source_identity": f"voice:{session_id}:{input_turn_id}:2",
    }
    assert turn_id == pool.turn_id
    assert pool.acquire_count == 1


async def test_partial_playback_never_persists_generated_assistant_text():
    pool = _Pool()
    history = VoiceConversationHistory(pool)
    session_id, input_turn_id, turn_id = uuid4(), uuid4(), uuid4()

    result = await history.complete_turn(
        session_id=session_id,
        input_turn_id=input_turn_id,
        input_order=4,
        stimulus_revision=0,
        turn_id=turn_id,
        user_text="authorized input",
        assistant_text="unheard tail",
        context_version="context-v1",
        playback_scope="partial",
        metadata={"final_response_id": "resp-1"},
        media_session_id="media-1",
        response_ids=["resp-1"],
        playback_ids=[{"playback_id": "play-1", "scope": "partial"}],
    )

    sql, args = pool.calls[0]
    assert "complete_voice_turn" in sql
    assert args[6] is None
    assert result["status"] == "completed"
    assert pool.acquire_count == 1
    assert pool.transaction_count == 1


async def test_voice_tool_call_and_result_are_durable_before_realtime_output(db_pool):
    history = VoiceConversationHistory(db_pool)
    session_id, input_turn_id = uuid4(), uuid4()
    turn_id = await history.start_turn(
        session_id=session_id,
        input_turn_id=input_turn_id,
        input_order=1,
        stimulus_revision=3,
        user_text="Please start the task.",
        context_version="context-v1",
    )
    arguments = {"task_description": "Prepare the requested plan"}

    registered = await history.register_tool_call(
        turn_id=turn_id,
        call_id="voice-submit-call",
        tool_name="submit",
        arguments=arguments,
    )
    uncertain_replay = await history.register_tool_call(
        turn_id=turn_id,
        call_id="voice-submit-call",
        tool_name="submit",
        arguments=arguments,
    )
    with pytest.raises(Exception, match="identity conflicts"):
        await history.register_tool_call(
            turn_id=turn_id,
            call_id="voice-submit-call",
            tool_name="submit",
            arguments={"task_description": "a different task"},
        )
    receipt = {
        "contract_version": "m5-tool-v1",
        "tool_name": "submit",
        "call_id": "voice-submit-call",
        "operation_id": str(uuid4()),
        "ok": True,
        "state": "accepted",
        "data": {"task_id": str(uuid4())},
        "error": None,
    }
    output = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
    saved = await history.record_tool_result(
        turn_id=turn_id,
        call_id="voice-submit-call",
        tool_name="submit",
        arguments=arguments,
        output=receipt,
        output_text=output,
        success=True,
    )
    replay = await history.register_tool_call(
        turn_id=turn_id,
        call_id="voice-submit-call",
        tool_name="submit",
        arguments=arguments,
    )
    recall_arguments = {"query": "the saved plan"}
    await history.register_tool_call(
        turn_id=turn_id,
        call_id="voice-recall-call",
        tool_name="voice_recall",
        arguments=recall_arguments,
    )
    recall_error = {
        "status": "error",
        "error": {"code": "VOICE_RECALL_FAILED", "message": "Recall failed."},
    }
    recall_output = json.dumps(recall_error, separators=(",", ":"))
    await history.record_tool_result(
        turn_id=turn_id,
        call_id="voice-recall-call",
        tool_name="voice_recall",
        arguments=recall_arguments,
        output=recall_error,
        output_text=recall_output,
        success=False,
    )

    broken_event = [{
        "event_id": "not-a-uuid",
        "schema_version": 1,
        "event_type": "voice_turn_completed",
        "stream_id": "voice-realtime-session",
        "request_id": str(input_turn_id),
        "response_id": "response-final",
        "media_session_id": "media-voice-tool-history",
        "producer_seq": 1,
        "source": "server",
        "producer_id": "hexis.voice-realtime",
        "channel": "voice",
        "payload": {},
    }]
    async with db_pool.acquire() as conn:
        with pytest.raises(Exception):
            await conn.fetchval(
                """SELECT public.complete_voice_turn(
                    $1::uuid,$2::uuid,1,3,$3::uuid,$4,$5,'context-v1','complete',
                    '{}'::jsonb,$6::jsonb
                )""",
                session_id,
                input_turn_id,
                turn_id,
                "Please start the task.",
                "The requested task is accepted.",
                json.dumps(broken_event),
            )

    assert registered["status"] == "registered"
    assert uncertain_replay["status"] == "unknown"
    assert saved["replayed"] is False
    assert replay == {"status": "completed", "output": output}
    async with db_pool.acquire() as conn:
        messages = await conn.fetch(
            "SELECT role,content,content_blocks,metadata,source_message_id "
            "FROM chat_messages WHERE session_id=$1::uuid ORDER BY ordinal",
            session_id,
        )
        turn_messages = await conn.fetchval(
            "SELECT messages FROM agent_turns WHERE id=$1::uuid", turn_id
        )
    assert [message["role"] for message in messages] == [
        "user", "assistant", "toolResult", "assistant", "toolResult"
    ]
    assert messages[0]["source_message_id"] == (
        f"voice:{session_id}:{input_turn_id}:3:user"
    )
    assert messages[1]["source_message_id"] == f"tool:{turn_id}:voice-submit-call:call"
    assert _json(messages[1]["content_blocks"])[0]["arguments"] == arguments
    assert messages[2]["source_message_id"] == f"tool:{turn_id}:voice-submit-call:result"
    assert messages[4]["source_message_id"] == f"tool:{turn_id}:voice-recall-call:result"
    result_metadata = _json(messages[2]["metadata"])
    recall_metadata = _json(messages[4]["metadata"])
    call_metadata = _json(messages[1]["metadata"])
    assert result_metadata["tool_result"]["details"] == receipt
    assert result_metadata["tool_result"]["isError"] is False
    assert recall_metadata["tool_result"]["details"] == recall_error
    assert recall_metadata["tool_result"]["isError"] is True
    assert "action_receipts" not in call_metadata
    assert "action_receipts" not in result_metadata
    assert "action_receipts" not in recall_metadata
    turn_messages = _json(turn_messages)
    assert turn_messages[-1]["role"] == "tool"
    assert turn_messages[-3]["role"] == "tool"
    assert turn_messages[-3]["content"] == output
    assert turn_messages[-1]["content"] == recall_output
    async with db_pool.acquire() as conn:
        completion_rows = await conn.fetchval(
            "SELECT count(*) FROM voice_turn_completions WHERE session_id=$1::uuid",
            session_id,
        )
        assistant_source_count = await conn.fetchval(
            "SELECT count(*) FROM chat_messages WHERE session_id=$1::uuid "
            "AND source_message_id=$2",
            session_id,
            f"voice:{session_id}:{input_turn_id}:3:assistant",
        )
    assert completion_rows == 0
    assert assistant_source_count == 0
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE chat_messages SET metadata=jsonb_set(metadata,'{tool_result,toolName}','\"other\"'::jsonb) "
            "WHERE session_id=$1::uuid AND source_message_id=$2",
            session_id,
            f"tool:{turn_id}:voice-submit-call:result",
        )
    with pytest.raises(Exception, match="result identity conflicts"):
        await history.register_tool_call(
            turn_id=turn_id,
            call_id="voice-submit-call",
            tool_name="submit",
            arguments=arguments,
        )
