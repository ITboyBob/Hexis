"""Real AgentLoop-to-SQL verification for BOB-107's durable tool history."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


def _decode_json(value):
    return json.loads(value) if isinstance(value, str) else value


class _Registry:
    def __init__(self, pool, *, tool_result):
        self.pool = pool
        self._tool_result = tool_result
        self.execute_count = 0

    async def get_specs(self, _context):
        return [{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read one project file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }]

    async def get_config(self):
        from core.tools.config import ToolsConfig

        return ToolsConfig()

    def get_spec(self, _name):
        return None

    async def execute(self, name, arguments, _context):
        assert name == "read_file"
        assert arguments == {"path": "README.md"}
        self.execute_count += 1
        return self._tool_result


async def _new_projected_chat(db_pool):
    from services.chat import _persist_chat_user_source

    session_id = uuid4()
    client_message_id = uuid4()
    user_text = "Read the README and report the project name."
    identity = await _persist_chat_user_source(
        db_pool,
        user_message=user_text,
        session_id=str(session_id),
        client_message_id=str(client_message_id),
        surface="api",
    )
    return session_id, identity, user_text


def _agent_config(db_pool, registry, session_id, source_identity):
    from core.agent_loop import AgentLoopConfig
    from core.tools.config import ToolContext

    return AgentLoopConfig(
        tool_context=ToolContext.CHAT,
        system_prompt="You are an offline BOB-107 tool history test agent.",
        llm_config={"provider": "openai", "model": "offline-fixture"},
        registry=registry,
        pool=db_pool,
        timeout_seconds=10,
        session_id=str(session_id),
        chat_message_projection=True,
        chat_source_identity=source_identity,
        surface="chat",
    )


async def test_real_agent_loop_persists_tool_result_before_provider_failure(
    db_pool, monkeypatch
):
    from core.agent_loop import AgentLoop
    from core.tools.base import ToolResult

    session_id, identity, user_text = await _new_projected_chat(db_pool)
    call_id = f"call_{uuid4().hex}"
    tool_payload = {
        "text": "Hexis",
        "source": "README.md",
        "sentinel": "TOOL_PAYLOAD_SECRET_SENTINEL",
    }
    registry = _Registry(
        db_pool,
        tool_result=ToolResult.success_result(tool_payload),
    )
    registry._tool_result.energy_spent = 3
    provider_calls = 0
    provider_history = []

    async def offline_provider(**kwargs):
        nonlocal provider_calls
        provider_calls += 1
        messages = kwargs["messages"]
        provider_history.append(messages)
        assert all("metadata" not in message for message in messages)
        assert all("content_blocks" not in message for message in messages)
        if provider_calls == 1:
            return {
                "content": "Checking the README.",
                "tool_calls": [{
                    "id": call_id,
                    "name": "read_file",
                    "arguments": {"path": "README.md"},
                }],
                "raw": None,
            }
        assert any(
            message.get("role") == "tool"
            and "TOOL_PAYLOAD_SECRET_SENTINEL" in str(message.get("content"))
            for message in messages
        )
        async with db_pool.acquire() as conn:
            durable = await conn.fetchval(
                "SELECT count(*) FROM chat_messages WHERE session_id=$1::uuid "
                "AND source_message_id=$2",
                session_id,
                f"tool:{agent._turn_id}:{call_id}:result",
            )
        assert durable == 1
        raise RuntimeError("injected follow-up provider failure")

    monkeypatch.setattr("core.agent_loop.chat_completion", offline_provider)
    agent = AgentLoop(
        _agent_config(db_pool, registry, session_id, identity["source_identity"])
    )
    result = await agent.run(user_text)

    assert result.stopped_reason == "error"
    assert provider_calls == 2
    assert registry.execute_count == 1
    async with db_pool.acquire() as conn:
        messages = await conn.fetch(
            "SELECT role,content,content_blocks,metadata,source_message_id "
            "FROM chat_messages WHERE session_id=$1::uuid ORDER BY ordinal",
            session_id,
        )
        turn = await conn.fetchrow(
            "SELECT status,messages,runtime_state FROM agent_turns WHERE id=$1::uuid",
            result.turn_id,
        )
        result_events = await conn.fetch(
            "SELECT payload FROM agent_turn_events WHERE turn_id=$1::uuid "
            "AND event_type='tool_result'",
            result.turn_id,
        )
    assert [row["role"] for row in messages] == ["user", "assistant", "toolResult"]
    assert messages[0]["content"] == user_text
    assert messages[1]["source_message_id"] == f"tool:{result.turn_id}:{call_id}:call"
    assert messages[2]["source_message_id"] == f"tool:{result.turn_id}:{call_id}:result"
    result_metadata = json.loads(messages[2]["metadata"]) if isinstance(messages[2]["metadata"], str) else messages[2]["metadata"]
    assert result_metadata["tool_result"]["details"] == tool_payload
    assert "action_receipts" not in result_metadata
    turn_messages = json.loads(turn["messages"]) if isinstance(turn["messages"], str) else turn["messages"]
    runtime_state = _decode_json(turn["runtime_state"])
    assert turn["status"] == "failed"
    assert turn_messages[-1]["role"] == "tool"
    assert "TOOL_PAYLOAD_SECRET_SENTINEL" in turn_messages[-1]["content"]
    assert runtime_state["energy_spent"] == 3
    assert len(runtime_state["tool_calls_made"]) == 1
    assert runtime_state["tool_calls_made"][0]["success"] is True
    event_payloads = [
        json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        for row in result_events
    ]
    assert event_payloads
    assert all(event["call_id"] == call_id for event in event_payloads)
    assert all(event["output"] == tool_payload for event in event_payloads)
    assert any(event.get("total_energy_spent") == 3 for event in event_payloads)

    from core.conversation_messages import internal_to_provider

    hydrated = await db_pool.fetch(
        "SELECT role,content,content_blocks,metadata,source_message_id,session_id "
        "FROM chat_messages WHERE session_id=$1::uuid ORDER BY ordinal",
        session_id,
    )
    internal_history = [dict(row) for row in hydrated]
    for message in internal_history:
        message["content_blocks"] = _decode_json(message["content_blocks"])
        message["metadata"] = _decode_json(message["metadata"])
    provider_messages, diagnostics = internal_to_provider(
        internal_history, session_id=str(session_id)
    )
    assert diagnostics == []
    assert [message["role"] for message in provider_messages] == [
        "user", "assistant", "tool"
    ]
    assert all("metadata" not in message and "content_blocks" not in message for message in provider_messages)

    from services.chat import ChatRequestIdentityError, chat_turn

    with pytest.raises(ChatRequestIdentityError, match="completion status is unknown"):
        await chat_turn(
            user_message=user_text,
            history=[],
            llm_config={"provider": "openai", "model": "offline-fixture"},
            session_id=str(session_id),
            client_message_id=str(identity["client_message_id"]),
            pool=db_pool,
            surface="api",
        )
    assert registry.execute_count == 1


async def test_real_agent_loop_database_result_failure_blocks_followup_and_is_not_retried(
    db_pool, monkeypatch
):
    from core.agent_loop import AgentLoop
    from core.tools.base import ToolResult

    session_id, identity, user_text = await _new_projected_chat(db_pool)
    call_id = f"call_{uuid4().hex}"
    registry = _Registry(
        db_pool,
        tool_result=ToolResult.success_result({"text": "written"}),
    )
    provider_calls = 0
    suffix = uuid4().hex
    function_name = f"bob107_fail_tool_result_{suffix}"
    trigger_name = f"bob107_fail_tool_result_{suffix}"
    async with db_pool.acquire() as conn:
        await conn.execute(
            f"""CREATE FUNCTION public.{function_name}() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN
                    IF NEW.role='toolResult' THEN
                        RAISE EXCEPTION 'injected durable result write failure';
                    END IF;
                    RETURN NEW;
                END $$"""
        )
        await conn.execute(
            f"CREATE TRIGGER {trigger_name} BEFORE INSERT ON public.chat_messages "
            f"FOR EACH ROW EXECUTE FUNCTION public.{function_name}()"
        )

    async def offline_provider(**_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        return {
            "content": "Reading.",
            "tool_calls": [{
                "id": call_id,
                "name": "read_file",
                "arguments": {"path": "README.md"},
            }],
            "raw": None,
        }

    monkeypatch.setattr("core.agent_loop.chat_completion", offline_provider)
    agent = AgentLoop(
        _agent_config(db_pool, registry, session_id, identity["source_identity"])
    )
    try:
        with pytest.raises(Exception, match="injected durable result write failure"):
            await agent.run(user_text)
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                f"DROP TRIGGER IF EXISTS {trigger_name} ON public.chat_messages"
            )
            await conn.execute(f"DROP FUNCTION IF EXISTS public.{function_name}()")

    assert provider_calls == 1
    assert registry.execute_count == 1
    async with db_pool.acquire() as conn:
        call_count = await conn.fetchval(
            "SELECT count(*) FROM chat_messages WHERE session_id=$1::uuid "
            "AND role='assistant' AND source_message_id LIKE 'tool:%:call'",
            session_id,
        )
        result_count = await conn.fetchval(
            "SELECT count(*) FROM chat_messages WHERE session_id=$1::uuid AND role='toolResult'",
            session_id,
        )
    assert call_count == 1
    assert result_count == 0
