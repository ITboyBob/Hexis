from __future__ import annotations

import json
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


def _coerce_json(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


async def test_agent_turn_state_machine_records_llm_and_tool_results(db_pool):
    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            started = _coerce_json(
                await conn.fetchval(
                    "SELECT start_agent_turn($1::text, $2::text, NULL, $3::jsonb)",
                    "chat",
                    "hello",
                    json.dumps({
                        "messages": [{"role": "user", "content": "hello"}],
                        "energy_budget": 5,
                        "max_iterations": 2,
                    }),
                )
            )
            turn_id = started["turn_id"]
            step = _coerce_json(await conn.fetchval("SELECT next_agent_step($1::uuid)", turn_id))
            assert step["action"] == "llm"
            assert step["iteration"] == 1

            applied_llm = _coerce_json(
                await conn.fetchval(
                    "SELECT apply_agent_llm_result($1::uuid, $2::jsonb)",
                    turn_id,
                    json.dumps({"content": "using tool", "tool_calls": [{"name": "echo"}]}),
                )
            )
            assert applied_llm["iterations"] == 1

            applied_tool = _coerce_json(
                await conn.fetchval(
                    "SELECT apply_agent_tool_result($1::uuid, $2::text, $3::jsonb)",
                    turn_id,
                    "call-1",
                    json.dumps({"tool_name": "echo", "success": True, "energy_spent": 2, "model_output": "ok"}),
                )
            )
            assert applied_tool["energy_spent"] == 2

            finished = _coerce_json(
                await conn.fetchval(
                    "SELECT finish_agent_turn($1::uuid, $2::jsonb)",
                    turn_id,
                    json.dumps({"stopped_reason": "completed", "text": "done"}),
                )
            )
            assert finished["status"] == "completed"
            assert finished["stopped_reason"] == "completed"
            event_count = await conn.fetchval("SELECT COUNT(*) FROM agent_turn_events WHERE turn_id = $1::uuid", turn_id)
            assert event_count >= 4
        finally:
            await tr.rollback()


async def test_agent_next_step_stops_on_db_owned_energy_budget(db_pool):
    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            started = _coerce_json(
                await conn.fetchval(
                    "SELECT start_agent_turn('heartbeat', 'go', NULL, $1::jsonb)",
                    json.dumps({"energy_budget": 1}),
                )
            )
            turn_id = started["turn_id"]
            await conn.fetchval(
                "SELECT apply_agent_tool_result($1::uuid, 'call-1', $2::jsonb)",
                turn_id,
                json.dumps({"tool_name": "costly", "success": True, "energy_spent": 1}),
            )
            step = _coerce_json(await conn.fetchval("SELECT next_agent_step($1::uuid)", turn_id))
            assert step["action"] == "stop"
            assert step["reason"] == "energy"
        finally:
            await tr.rollback()


async def test_next_step_returns_db_owned_message_log(db_pool):
    """next_agent_step hands back the authoritative agent_turns.messages log,
    and apply_* / append_agent_message accumulate it in replayable format."""
    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            started = _coerce_json(
                await conn.fetchval(
                    "SELECT start_agent_turn($1::text, $2::text, NULL, $3::jsonb)",
                    "chat",
                    "hi",
                    json.dumps({
                        "messages": [
                            {"role": "system", "content": "S"},
                            {"role": "user", "content": "hi"},
                        ],
                        "energy_budget": 10,
                        "max_iterations": 5,
                    }),
                )
            )
            turn_id = started["turn_id"]

            # The step hands back the current message log for the LLM call.
            step = _coerce_json(await conn.fetchval("SELECT next_agent_step($1::uuid)", turn_id))
            assert step["action"] == "llm"
            assert isinstance(step["messages"], list)
            assert step["messages"][-1] == {"role": "user", "content": "hi"}

            # Assistant message with an OpenAI-format tool call, then its result.
            await conn.fetchval(
                "SELECT apply_agent_llm_result($1::uuid, $2::jsonb)",
                turn_id,
                json.dumps({
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "type": "function",
                         "function": {"name": "recall", "arguments": "{}"}}
                    ],
                }),
            )
            await conn.fetchval(
                "SELECT apply_agent_tool_result($1::uuid, 'c1', $2::jsonb)",
                turn_id,
                json.dumps({"tool_name": "recall", "success": True,
                            "energy_spent": 1, "model_output": "result-text"}),
            )

            step2 = _coerce_json(await conn.fetchval("SELECT next_agent_step($1::uuid)", turn_id))
            roles = [m["role"] for m in step2["messages"]]
            assert roles == ["system", "user", "assistant", "tool"]
            assert step2["messages"][2]["tool_calls"][0]["id"] == "c1"
            # Budgeted turns carry the energy footer on every tool result (#44).
            assert step2["messages"][3]["content"] == "result-text\n\n[energy: 1/10 spent]"

            # append_agent_message adds a user turn (continuation / plan / verify).
            appended = _coerce_json(
                await conn.fetchval(
                    "SELECT append_agent_message($1::uuid, 'user', 'continue')", turn_id
                )
            )
            assert appended["messages"][-1] == {"role": "user", "content": "continue"}
        finally:
            await tr.rollback()


async def test_agent_chat_tool_calls_project_only_for_explicit_chat_opt_in(db_pool):
    session_id = uuid4()
    client_message_id = uuid4()
    source_identity = f"chat:{session_id}:{client_message_id}"
    call_id = f"call_{uuid4().hex}"
    tool_call = {
        "id": call_id,
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
    }
    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.fetchval(
                "SELECT get_or_create_chat_session($1::uuid,'chat',NULL,'{}'::jsonb)",
                session_id,
            )
            user_content = "Read the project title."
            user_blocks = [{"type": "text", "text": user_content}]
            await conn.fetchval(
                "SELECT append_chat_message_blocks($1,'user',$2,$3::jsonb,'{}'::jsonb,$4,TRUE)",
                session_id,
                user_content,
                json.dumps(user_blocks),
                f"{source_identity}:user",
            )
            optin_turn = _coerce_json(await conn.fetchval(
                "SELECT start_agent_turn('chat',$1,$2::uuid,$3::jsonb)",
                user_content,
                session_id,
                json.dumps({
                    "messages": [{"role": "user", "content": user_content}],
                    "runtime_state": {
                        "chat_message_projection": True,
                        "chat_source_identity": source_identity,
                        "chat_work_execution_id": None,
                    },
                }),
            ))
            await conn.fetchval(
                "SELECT apply_agent_llm_result($1::uuid,$2::jsonb)",
                optin_turn["turn_id"],
                json.dumps({"content": "intermediate text must not become chat body", "tool_calls": [tool_call]}),
            )
            projected = await conn.fetchrow(
                "SELECT role,content,content_blocks,metadata,source_message_id "
                "FROM chat_messages WHERE session_id=$1 AND source_message_id=$2",
                session_id,
                f"tool:{optin_turn['turn_id']}:{call_id}:call",
            )
            assert projected is not None
            assert projected["role"] == "assistant"
            assert projected["content"] == ""
            assert _coerce_json(projected["metadata"])["agent_turn_id"] == optin_turn["turn_id"]
            projected_blocks = _coerce_json(projected["content_blocks"])
            assert projected_blocks[0]["type"] == "toolCall"
            assert projected_blocks[0]["id"] == call_id

            tool_result = {
                "tool_name": "read_file",
                "arguments": {"path": "README.md"},
                "success": True,
                "output": {"title": "Hexis"},
                "display_output": "Read README",
                "model_output": '{"title":"Hexis"}',
                "energy_spent": 2,
                "metadata": {
                    "model_visual_attachments": [{
                        "data_url": "data:image/png;base64,AAAA",
                        "mime_type": "image/png",
                        "name": "plot.png",
                    }]
                },
            }
            first_result = _coerce_json(await conn.fetchval(
                "SELECT apply_agent_tool_result($1::uuid,$2,$3::jsonb)",
                optin_turn["turn_id"],
                call_id,
                json.dumps(tool_result),
            ))
            assert first_result["replayed"] is False
            replayed_result = _coerce_json(await conn.fetchval(
                "SELECT apply_agent_tool_result($1::uuid,$2,$3::jsonb)",
                optin_turn["turn_id"],
                call_id,
                json.dumps(tool_result),
            ))
            assert replayed_result["replayed"] is True
            result_row = await conn.fetchrow(
                "SELECT role,content,content_blocks,metadata,source_message_id "
                "FROM chat_messages WHERE session_id=$1 AND source_message_id=$2",
                session_id,
                f"tool:{optin_turn['turn_id']}:{call_id}:result",
            )
            assert result_row["role"] == "toolResult"
            result_metadata = _coerce_json(result_row["metadata"])
            assert result_metadata["tool_result"] == {
                "toolCallId": call_id,
                "toolName": "read_file",
                "isError": False,
                "details": {"title": "Hexis"},
            }
            assert result_metadata["model_visual_attachments"][0]["name"] == "plot.png"
            assert _coerce_json(result_row["content_blocks"])[0]["text"] == '{"title":"Hexis"}'
            state_row = await conn.fetchrow(
                "SELECT messages,runtime_state FROM agent_turns WHERE id=$1::uuid",
                optin_turn["turn_id"],
            )
            state = {
                "messages": _coerce_json(state_row["messages"]),
                "runtime_state": _coerce_json(state_row["runtime_state"]),
            }
            assert state["runtime_state"]["energy_spent"] == 2
            assert len(state["runtime_state"]["tool_calls_made"]) == 1
            assert sum(message.get("role") == "tool" for message in state["messages"]) == 1
            assert await conn.fetchval(
                "SELECT count(*) FROM agent_turn_events WHERE turn_id=$1::uuid AND event_type='tool_result'",
                optin_turn["turn_id"],
            ) == 1
            with pytest.raises(Exception, match="tool result identity conflict"):
                async with conn.transaction():
                    await conn.fetchval(
                        "SELECT apply_agent_tool_result($1::uuid,$2,$3::jsonb)",
                        optin_turn["turn_id"],
                        call_id,
                        json.dumps({**tool_result, "output": {"title": "Different"}}),
                    )

            scalar_call_id = f"call_{uuid4().hex}"
            scalar_call = {
                "id": scalar_call_id,
                "type": "function",
                "function": {"name": "send_message", "arguments": "{}"},
            }
            await conn.fetchval(
                "SELECT apply_agent_llm_result($1::uuid,$2::jsonb)",
                optin_turn["turn_id"],
                json.dumps({"content": "", "tool_calls": [scalar_call]}),
            )
            await conn.fetchval(
                "SELECT apply_agent_tool_result($1::uuid,$2,$3::jsonb)",
                optin_turn["turn_id"],
                scalar_call_id,
                json.dumps({
                    "tool_name": "send_message",
                    "arguments": {},
                    "success": False,
                    "output": "delivery refused",
                    "error": "denied",
                    "model_output": "Error: denied",
                    "energy_spent": 0,
                }),
            )
            scalar_result = await conn.fetchrow(
                "SELECT content,content_blocks,metadata FROM chat_messages "
                "WHERE session_id=$1 AND source_message_id=$2",
                session_id,
                f"tool:{optin_turn['turn_id']}:{scalar_call_id}:result",
            )
            scalar_metadata = _coerce_json(scalar_result["metadata"])
            assert scalar_result["content"] == "delivery refused"
            assert scalar_metadata["tool_result"]["isError"] is True
            assert scalar_metadata["tool_result"]["details"] is None
            assert _coerce_json(scalar_result["content_blocks"])[0]["text"] == "delivery refused"

            non_optin_turn = _coerce_json(await conn.fetchval(
                "SELECT start_agent_turn('chat',$1,$2::uuid,$3::jsonb)",
                "legacy chat call",
                session_id,
                json.dumps({"messages": [{"role": "user", "content": "legacy chat call"}]}),
            ))
            non_optin_call_id = f"call_{uuid4().hex}"
            await conn.fetchval(
                "SELECT apply_agent_llm_result($1::uuid,$2::jsonb)",
                non_optin_turn["turn_id"],
                json.dumps({"tool_calls": [{**tool_call, "id": non_optin_call_id}]}),
            )
            non_optin_count = await conn.fetchval(
                "SELECT count(*) FROM chat_messages WHERE session_id=$1 "
                "AND source_message_id=$2",
                session_id,
                f"tool:{non_optin_turn['turn_id']}:{non_optin_call_id}:call",
            )
            assert non_optin_count == 0

            with pytest.raises(Exception, match="chat_message_projection"):
                await conn.fetchval(
                    "SELECT start_agent_turn('heartbeat',$1,$2::uuid,$3::jsonb)",
                    "must not opt in",
                    session_id,
                    json.dumps({"runtime_state": {
                        "chat_message_projection": True,
                        "chat_source_identity": f"chat:{session_id}:{uuid4()}",
                    }}),
                )
        finally:
            await tr.rollback()


async def test_external_call_kind_resolution_is_db_owned(db_pool):
    async with db_pool.acquire() as conn:
        tool_call = _coerce_json(
            await conn.fetchval(
                "SELECT resolve_external_call_kind($1::jsonb)",
                json.dumps({"call_type": "tool_use", "input": {"tool_name": "recall"}}),
            )
        )
        think_call = _coerce_json(
            await conn.fetchval(
                "SELECT resolve_external_call_kind($1::jsonb)",
                json.dumps({"call_type": "think", "input": {"kind": "reflect"}}),
            )
        )
        assert tool_call["call_type"] == "tool_use"
        assert tool_call["kind"] == "tool_use"
        assert think_call["call_type"] == "think"
        assert think_call["kind"] == "reflect"
        assert think_call["supported"] is True
