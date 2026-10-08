from contextlib import asynccontextmanager
import asyncio
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from services import chat as chat_mod
from core.agent_loop import AgentEvent, AgentEventData
from core.cognitive_memory_api import HydratedContext, Memory, MemoryType, RecallResult

pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.core]


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_exc):
        return False


class _ConfigPool:
    def __init__(self, values):
        self.values = values

    def acquire(self):
        return _Acquire(self)

    async def fetchval(self, query):
        for key, value in self.values.items():
            if key in query:
                return value
        return None


class _RememberMem:
    def __init__(self, config=None):
        self._pool = _ConfigPool(config or {})
        self.raw_calls = []
        self.remember_calls = []
        self.link_calls = []
        self.record_chat_turn_memory_calls = []
        self.record_chat_session_turn_calls = []

    async def remember_turn_raw(self, *args, **kwargs):
        self.raw_calls.append((args, kwargs))
        return {"unit_id": str(uuid4()), "status": "stored"}

    async def remember(self, *args, **kwargs):
        self.remember_calls.append((args, kwargs))
        return uuid4()

    async def link_to_source_unit(self, *args, **kwargs):
        self.link_calls.append((args, kwargs))
        return True

    async def record_chat_turn_memory(self, *args, **kwargs):
        self.record_chat_turn_memory_calls.append((args, kwargs))
        return {"direct_promoted": True, "raw": {"status": "stored"}}

    async def record_chat_session_turn(self, *args, **kwargs):
        self.record_chat_session_turn_calls.append((args, kwargs))
        return {
            "session": {"session_id": kwargs.get("session_id")},
            "history": {"messages": []},
        }


async def test_remember_conversation_does_not_write_new_action_receipts():
    mem = _RememberMem()
    session_id = str(uuid4())
    turn_id = str(uuid4())

    await chat_mod._remember_conversation(  # noqa: SLF001
        mem,
        user_message="remember this important preference",
        assistant_message="noted",
        session_id=session_id,
        source_identity="chat:test",
        agent_turn_id=turn_id,
        surface="cli",
    )

    assert len(mem.record_chat_session_turn_calls) == 1
    assert (
        mem.record_chat_session_turn_calls[0][0][0]
        == "remember this important preference"
    )
    assert mem.record_chat_session_turn_calls[0][1]["session_id"] == session_id
    assert mem.record_chat_session_turn_calls[0][1]["surface"] == "cli"
    context = mem.record_chat_session_turn_calls[0][1]["context"]
    assert context["assistant_metadata"] == {"agent_turn_id": turn_id}


async def test_message_history_rehydrates_authoritative_action_receipts():
    memory_id = str(uuid4())
    receipt = {
        "name": "remember",
        "success": True,
        "arguments": {"content": "preserve the lighthouse agreement"},
        "result": {"memory_id": memory_id, "reused": False},
    }

    history = chat_mod._message_history_only(
        [
            {  # noqa: SLF001
                "role": "assistant",
                "content": "Stored it.",
                "metadata": {"action_receipts": [receipt]},
            }
        ],
        trusted_database=True,
    )

    assert history[0]["metadata"]["action_receipts"] == [receipt]
    assert history[1]["role"] == "system"
    assert "Authoritative prior-action evidence" in history[1]["content"]
    assert memory_id in history[1]["content"]


async def test_message_history_preserves_paired_internal_tool_messages():
    session_id = str(uuid4())
    turn_id = str(uuid4())
    call_id = "call-history-pair"
    history = chat_mod._message_history_only(  # noqa: SLF001
        [
            {
                "role": "user",
                "content": "Read README",
                "content_blocks": [{"type": "text", "text": "Read README"}],
                "source_message_id": "chat:prior:user",
                "metadata": {},
            },
            {
                "role": "assistant",
                "content": "",
                "content_blocks": [{
                    "type": "toolCall",
                    "id": call_id,
                    "name": "read_file",
                    "arguments": {"path": "README.md"},
                }],
                "source_message_id": f"tool:{turn_id}:{call_id}:call",
                "metadata": {"agent_turn_id": turn_id},
            },
            {
                "role": "toolResult",
                "content": "Hexis project",
                "content_blocks": [{"type": "text", "text": "Hexis project"}],
                "source_message_id": f"tool:{turn_id}:{call_id}:result",
                "metadata": {
                    "agent_turn_id": turn_id,
                    "tool_result": {
                        "toolCallId": call_id,
                        "toolName": "read_file",
                        "isError": False,
                        "details": {"title": "Hexis"},
                    },
                },
            },
        ],
        session_id=session_id,
        trusted_database=True,
    )

    assert [item["role"] for item in history] == ["user", "assistant", "toolResult"]
    assert history[1]["content_blocks"][0]["id"] == call_id
    assert history[2]["metadata"]["tool_result"]["details"] == {"title": "Hexis"}


async def test_new_input_filter_keeps_legacy_client_history_for_empty_db_session(db_pool):
    session_id = str(uuid4())
    request_identity = await chat_mod._persist_chat_user_source(  # noqa: SLF001
        db_pool,
        user_message="Current question",
        session_id=session_id,
        client_message_id=None,
        surface="api",
    )
    history = await chat_mod._hydrate_chat_history(  # noqa: SLF001
        db_pool,
        session_id,
        [
            {"role": "user", "content": "Legacy previous turn"},
            {
                "role": "assistant",
                "content": "I stored it.",
                "metadata": {"action_receipts": [{"name": "remember", "success": True}]},
            },
        ],
        exclude_source_message_id=request_identity["user_source_message_id"],
    )

    assert [item["role"] for item in history] == ["user", "assistant"]
    assert history[0]["content"] == "Legacy previous turn"
    assert "metadata" not in history[1]


async def test_turn_id_survives_without_action_receipts():
    mem = _RememberMem()
    turn_id = str(uuid4())

    await chat_mod._remember_conversation(  # noqa: SLF001
        mem,
        user_message="hello",
        assistant_message="Hello.",
        session_id=str(uuid4()),
        agent_turn_id=turn_id,
    )

    context = mem.record_chat_session_turn_calls[0][1]["context"]
    assert context["assistant_metadata"] == {"agent_turn_id": turn_id}

    history = chat_mod._message_history_only(  # noqa: SLF001
        [
            {
                "role": "assistant",
                "content": "Hello.",
                "metadata": {"agent_turn_id": turn_id},
            }
        ],
        trusted_database=True,
    )
    assert history == [
        {
            "role": "assistant",
            "content": "Hello.",
            "metadata": {"agent_turn_id": turn_id},
        }
    ]


async def test_remember_conversation_falls_back_for_non_uuid_session():
    mem = _RememberMem()

    await chat_mod._remember_conversation(  # noqa: SLF001
        mem,
        user_message="remember this important preference",
        assistant_message="noted",
        session_id="channel:legacy:session",
        source_identity="chat:test",
    )

    assert len(mem.record_chat_turn_memory_calls) == 1
    assert (
        mem.record_chat_turn_memory_calls[0][0][0]
        == "remember this important preference"
    )


async def test_chat_turn_basic_flow(monkeypatch, db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO config (key, value, description) VALUES ('chat.use_rlm', 'false', 'test override') "
            "ON CONFLICT (key) DO UPDATE SET value = 'false'"
        )

    class DummyMem:
        def __init__(self):
            self.remembered = []
            self.touched = []

        async def hydrate(self, *_args, **_kwargs):
            return HydratedContext(
                memories=[],
                partial_activations=[],
                identity=[],
                worldview=[],
                emotional_state=None,
                goals=None,
                urgent_drives=[],
            )

        async def touch_memories(self, ids):
            self.touched.extend(ids)

        async def remember(self, content, **_kwargs):
            self.remembered.append(content)
            return uuid4()

        async def record_chat_turn_memory(self, *_args, **_kwargs):
            self.remembered.append("turn_memory")
            return {"direct_promoted": False, "raw": {"status": "stored"}}

    mem = DummyMem()

    @asynccontextmanager
    async def fake_connect(_dsn, **_kwargs):
        yield mem

    async def fake_chat_completion(**_kwargs):
        return {"content": "hello there", "tool_calls": []}

    monkeypatch.setattr(chat_mod.CognitiveMemory, "connect", fake_connect)
    monkeypatch.setattr("core.agent_loop.chat_completion", fake_chat_completion)

    async def fake_agent_profile(_dsn=None, **_kwargs):
        return {}

    monkeypatch.setattr(chat_mod, "get_agent_profile_context", fake_agent_profile)

    result = await chat_mod.chat_turn(
        user_message="hi",
        history=[],
        llm_config={"provider": "openai", "model": "gpt-4o"},
        dsn="postgresql://unused",
        pool=db_pool,
    )

    assert result["assistant"] == "hello there"
    assert result["history"][-2] == {"role": "user", "content": "hi"}
    assistant_history = result["history"][-1]
    assert assistant_history["role"] == "assistant"
    assert assistant_history["content"] == "hello there"
    UUID(assistant_history["metadata"]["agent_turn_id"])


async def test_chat_turn_tool_loop(monkeypatch, db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO config (key, value, description) VALUES ('chat.use_rlm', 'false', 'test override') "
            "ON CONFLICT (key) DO UPDATE SET value = 'false'"
        )

    class DummyMem:
        async def hydrate(self, *_args, **_kwargs):
            return HydratedContext(
                memories=[],
                partial_activations=[],
                identity=[],
                worldview=[],
                emotional_state=None,
                goals=None,
                urgent_drives=[],
            )

        async def touch_memories(self, _ids):
            return None

        async def remember(self, *_args, **_kwargs):
            return uuid4()

        async def record_chat_turn_memory(self, *_args, **_kwargs):
            return {"direct_promoted": False, "raw": {"status": "stored"}}

    mem = DummyMem()

    @asynccontextmanager
    async def fake_connect(_dsn, **_kwargs):
        yield mem

    responses = [
        {
            "content": "",
            "tool_calls": [
                {"id": "tool-1", "name": "recall", "arguments": {"query": "x"}}
            ],
        },
        {"content": "final response", "tool_calls": []},
    ]

    async def fake_chat_completion(**_kwargs):
        return responses.pop(0)

    monkeypatch.setattr(chat_mod.CognitiveMemory, "connect", fake_connect)
    monkeypatch.setattr("core.agent_loop.chat_completion", fake_chat_completion)

    async def fake_agent_profile(_dsn=None, **_kwargs):
        return {}

    monkeypatch.setattr(chat_mod, "get_agent_profile_context", fake_agent_profile)

    result = await chat_mod.chat_turn(
        user_message="hi",
        history=[],
        llm_config={"provider": "openai", "model": "gpt-4o"},
        dsn="postgresql://unused",
        pool=db_pool,
    )

    assert result["assistant"] == "final response"


async def test_chat_turn_hydrates_db_session_history_before_agent(monkeypatch, db_pool):
    session_id = str(uuid4())

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO config (key, value, description) VALUES ('chat.use_rlm', 'false', 'test override') "
            "ON CONFLICT (key) DO UPDATE SET value = 'false'"
        )
        await conn.fetchval(
            "SELECT append_chat_message($1::uuid, 'user', 'db says alpha')", session_id
        )
        await conn.fetchval(
            "SELECT append_chat_message($1::uuid, 'assistant', 'db says beta')",
            session_id,
        )

    captured: dict[str, object] = {}

    class AgentResult:
        text = "fresh response"

    async def fake_run_agent(*_args, **kwargs):
        captured["history"] = kwargs["history"]
        return AgentResult()

    async def fake_agent_profile(_dsn=None, **_kwargs):
        return {}

    async def fake_remember_conversation(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(chat_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(chat_mod, "get_agent_profile_context", fake_agent_profile)
    monkeypatch.setattr(chat_mod, "_remember_conversation", fake_remember_conversation)

    result = await chat_mod.chat_turn(
        user_message="continue",
        history=[{"role": "user", "content": "stale caller history"}],
        llm_config={"provider": "openai", "model": "gpt-4o"},
        dsn="postgresql://unused",
        pool=db_pool,
        session_id=session_id,
    )

    assert result["assistant"] == "fresh response"
    assert [
        (item["role"], item["content"]) for item in captured["history"]
    ] == [("user", "db says alpha"), ("assistant", "db says beta")]
    assert all("content_blocks" in item for item in captured["history"])


async def test_stream_chat_turn_reports_empty_timeout_without_memory(monkeypatch):
    async def fake_agent_profile(_dsn=None, **_kwargs):
        return {}

    async def fake_stream_agent(*_args, **_kwargs):
        yield AgentEventData(
            event=AgentEvent.LOOP_END,
            data={"stopped_reason": "timeout", "timed_out": True},
        )

    @asynccontextmanager
    async def fail_connect(*_args, **_kwargs):
        raise AssertionError("timeout notices must not be written as memories")
        yield

    monkeypatch.setattr(chat_mod, "get_agent_profile_context", fake_agent_profile)
    monkeypatch.setattr(chat_mod, "stream_agent", fake_stream_agent)
    monkeypatch.setattr(chat_mod.CognitiveMemory, "connect", fail_connect)

    chunks = [
        chunk
        async for chunk in chat_mod.stream_chat_turn(
            user_message="hi",
            history=[],
            llm_config={"provider": "openai", "model": "gpt-4o"},
            dsn="postgresql://unused",
            pool=object(),
        )
    ]

    text = "".join(chunks)
    assert "Request timed out before a response arrived" in text


async def test_stream_chat_turn_skips_memory_for_partial_timeout(monkeypatch):
    async def fake_agent_profile(_dsn=None, **_kwargs):
        return {}

    async def fake_stream_agent(*_args, **_kwargs):
        yield AgentEventData(event=AgentEvent.TEXT_DELTA, data={"text": "partial"})
        yield AgentEventData(
            event=AgentEvent.LOOP_END,
            data={"stopped_reason": "timeout", "timed_out": True},
        )

    @asynccontextmanager
    async def fail_connect(*_args, **_kwargs):
        raise AssertionError("partial timeout replies must not be written as memories")
        yield

    monkeypatch.setattr(chat_mod, "get_agent_profile_context", fake_agent_profile)
    monkeypatch.setattr(chat_mod, "stream_agent", fake_stream_agent)
    monkeypatch.setattr(chat_mod.CognitiveMemory, "connect", fail_connect)

    chunks = [
        chunk
        async for chunk in chat_mod.stream_chat_turn(
            user_message="hi",
            history=[],
            llm_config={"provider": "openai", "model": "gpt-4o"},
            dsn="postgresql://unused",
            pool=object(),
        )
    ]

    assert "".join(chunks) == "partial\n\n[Response timed out before completion.]"


async def test_stream_chat_turn_marks_partial_error_without_memory(monkeypatch):
    async def fake_agent_profile(_dsn=None, **_kwargs):
        return {}

    async def fake_stream_agent(*_args, **_kwargs):
        yield AgentEventData(event=AgentEvent.TEXT_DELTA, data={"text": "partial"})
        yield AgentEventData(
            event=AgentEvent.ERROR,
            data={"error": "AssertionError", "error_type": "AssertionError"},
        )
        yield AgentEventData(
            event=AgentEvent.LOOP_END,
            data={"stopped_reason": "error", "timed_out": False},
        )

    async def fail_remember(*_args, **_kwargs):
        raise AssertionError("partial failed replies must not be written as memories")

    monkeypatch.setattr(chat_mod, "get_agent_profile_context", fake_agent_profile)
    monkeypatch.setattr(chat_mod, "stream_agent", fake_stream_agent)
    monkeypatch.setattr(chat_mod, "_remember_conversation", fail_remember)

    chunks = [
        chunk
        async for chunk in chat_mod.stream_chat_turn(
            user_message="hi",
            history=[],
            llm_config={"provider": "openai", "model": "gpt-4o"},
            dsn="postgresql://unused",
            pool=object(),
        )
    ]

    assert "".join(chunks) == (
        "partial\n\n[Response failed before completion: AssertionError]"
    )


async def test_stream_chat_events_trusts_error_stop_without_error_event(monkeypatch):
    async def fake_agent_profile(_dsn=None, **_kwargs):
        return {}

    async def fake_stream_agent(*_args, **_kwargs):
        yield AgentEventData(event=AgentEvent.TEXT_DELTA, data={"text": "partial"})
        yield AgentEventData(
            event=AgentEvent.LOOP_END,
            data={"stopped_reason": "error", "timed_out": False},
        )

    async def fail_remember(*_args, **_kwargs):
        raise AssertionError("error-stopped replies must not be written as memories")

    monkeypatch.setattr(chat_mod, "get_agent_profile_context", fake_agent_profile)
    monkeypatch.setattr(chat_mod, "stream_agent", fake_stream_agent)
    monkeypatch.setattr(chat_mod, "_remember_conversation", fail_remember)

    events = [
        event
        async for event in chat_mod.stream_chat_events(
            user_message="hi",
            history=[],
            llm_config={"provider": "openai", "model": "gpt-4o"},
            dsn="postgresql://unused",
            pool=object(),
        )
    ]

    assert [event.event for event in events] == [
        AgentEvent.TEXT_DELTA,
        AgentEvent.LOOP_END,
    ]


async def test_stream_chat_turn_marks_error_stop_without_error_event(monkeypatch):
    async def fake_agent_profile(_dsn=None, **_kwargs):
        return {}

    async def fake_stream_agent(*_args, **_kwargs):
        yield AgentEventData(event=AgentEvent.TEXT_DELTA, data={"text": "partial"})
        yield AgentEventData(
            event=AgentEvent.LOOP_END,
            data={"stopped_reason": "error", "timed_out": False},
        )

    async def fail_remember(*_args, **_kwargs):
        raise AssertionError("error-stopped replies must not be written as memories")

    monkeypatch.setattr(chat_mod, "get_agent_profile_context", fake_agent_profile)
    monkeypatch.setattr(chat_mod, "stream_agent", fake_stream_agent)
    monkeypatch.setattr(chat_mod, "_remember_conversation", fail_remember)

    chunks = [
        chunk
        async for chunk in chat_mod.stream_chat_turn(
            user_message="hi",
            history=[],
            llm_config={"provider": "openai", "model": "gpt-4o"},
            dsn="postgresql://unused",
            pool=object(),
        )
    ]

    assert "".join(chunks) == (
        "partial\n\n[Response failed before completion: "
        "Agent loop ended with an error.]"
    )


async def test_stream_chat_turn_converts_iterator_exception_to_error_outcome(
    monkeypatch,
):
    async def fake_stream_chat_events(*_args, **_kwargs):
        yield AgentEventData(event=AgentEvent.TEXT_DELTA, data={"text": "partial"})
        raise ConnectionError("provider stream disconnected")

    monkeypatch.setattr(chat_mod, "stream_chat_events", fake_stream_chat_events)
    outcomes: list[str] = []

    chunks = [
        chunk
        async for chunk in chat_mod.stream_chat_turn(
            user_message="hi",
            history=[],
            llm_config={"provider": "openai", "model": "gpt-4o"},
            pool=object(),
            on_terminal_outcome=outcomes.append,
        )
    ]

    assert outcomes == ["error"]
    assert "".join(chunks) == (
        "partial\n\n[Response failed before completion: "
        "provider stream disconnected]"
    )


async def test_stream_chat_turn_rejects_clean_close_without_loop_end(monkeypatch):
    async def fake_stream_chat_events(*_args, **_kwargs):
        yield AgentEventData(event=AgentEvent.TEXT_DELTA, data={"text": "partial"})

    monkeypatch.setattr(chat_mod, "stream_chat_events", fake_stream_chat_events)
    outcomes: list[str] = []

    chunks = [
        chunk
        async for chunk in chat_mod.stream_chat_turn(
            user_message="hi",
            history=[],
            llm_config={"provider": "openai", "model": "gpt-4o"},
            pool=object(),
            on_terminal_outcome=outcomes.append,
        )
    ]

    assert outcomes == ["error"]
    assert "".join(chunks) == (
        "partial\n\n[Response failed before completion: "
        "The response stream ended before Hexis finished.]"
    )


async def test_stream_chat_events_keeps_token_streaming_when_rlm_streaming_disabled(
    monkeypatch,
):
    seen: dict[str, bool] = {}

    async def fake_agent_profile(*_args, **_kwargs):
        return {}

    async def fake_stream_agent(*_args, **_kwargs):
        seen["stream_agent"] = True
        yield AgentEventData(event=AgentEvent.LOOP_START, data={})
        yield AgentEventData(
            event=AgentEvent.REASONING_DELTA,
            data={"text": "private thought"},
        )
        yield AgentEventData(event=AgentEvent.TEXT_DELTA, data={"text": "Hello"})
        yield AgentEventData(event=AgentEvent.TEXT_DELTA, data={"text": " there"})
        yield AgentEventData(
            event=AgentEvent.LOOP_END,
            data={"stopped_reason": "completed", "timed_out": False},
        )

    async def fake_remember(*_args, **kwargs):
        seen["remembered"] = True
        seen["assistant"] = kwargs.get("assistant_message")
        return {}

    async def fake_energy(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(chat_mod, "get_agent_profile_context", fake_agent_profile)
    monkeypatch.setattr(chat_mod, "stream_agent", fake_stream_agent)
    monkeypatch.setattr(chat_mod, "_remember_conversation", fake_remember)
    monkeypatch.setattr(chat_mod, "_apply_chat_energy_effects", fake_energy)

    events = [
        event
        async for event in chat_mod.stream_chat_events(
            user_message="hi",
            history=[],
            llm_config={"provider": "openai", "model": "gpt-4o"},
            dsn="postgresql://unused",
            pool=_ConfigPool(
                {
                    "chat.use_rlm": True,
                    "rlm.chat.streaming_enabled": False,
                }
            ),
        )
    ]

    assert seen["stream_agent"] is True
    assert seen["remembered"] is True
    assert seen["assistant"] == "Hello there"
    assert [
        event.data.get("text")
        for event in events
        if event.event == AgentEvent.TEXT_DELTA
    ] == ["Hello", " there"]
    assert not any(event.data.get("runtime") == "rlm" for event in events)


async def test_stream_chat_events_persists_successful_tool_receipt(monkeypatch, db_pool):
    turn_id = str(uuid4())
    memory_id = str(uuid4())
    session_id = str(uuid4())
    client_message_id = str(uuid4())
    source_identity = f"chat:{session_id}:{client_message_id}"
    captured: dict[str, object] = {}

    async def fake_agent_profile(*_args, **_kwargs):
        return {}

    async def fake_stream_agent(*_args, **_kwargs):
        assert _kwargs["chat_message_projection"] is True
        assert _kwargs["chat_source_identity"] == source_identity
        assert not any(
            item.get("role") == "user" and item.get("content") == "Please remember this"
            for item in _kwargs["history"]
        )
        async with db_pool.acquire() as connection:
            user = await connection.fetchrow(
                "SELECT role,content,source_message_id FROM chat_messages "
                "WHERE session_id=$1::uuid AND source_message_id=$2",
                session_id,
                f"{source_identity}:user",
            )
        assert user is not None
        assert user["role"] == "user"
        assert user["content"] == "Please remember this"
        assert user["source_message_id"] == f"{source_identity}:user"
        yield AgentEventData(
            event=AgentEvent.LOOP_START,
            data={"turn_id": turn_id},
        )
        yield AgentEventData(
            event=AgentEvent.TOOL_RESULT,
            data={
                "turn_id": turn_id,
                "call_id": "remember-1",
                "tool_name": "remember",
                "arguments": {
                    "content": "preserve the lighthouse agreement",
                    "type": "semantic",
                },
                "success": True,
                "output": {"memory_id": memory_id, "type": "semantic", "reused": False},
                "energy_spent": 1,
            },
        )
        yield AgentEventData(
            event=AgentEvent.TEXT_DELTA,
            data={"turn_id": turn_id, "text": "Stored it."},
        )
        yield AgentEventData(
            event=AgentEvent.LOOP_END,
            data={"turn_id": turn_id, "stopped_reason": "completed"},
        )

    async def fake_remember(*_args, **kwargs):
        captured.update(kwargs)
        return {}

    async def fake_energy(*_args, **_kwargs):
        return {}

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO config (key, value, description) VALUES ('chat.use_rlm', 'false', 'test override') "
            "ON CONFLICT (key) DO UPDATE SET value='false'"
        )

    monkeypatch.setattr(chat_mod, "get_agent_profile_context", fake_agent_profile)
    monkeypatch.setattr(chat_mod, "stream_agent", fake_stream_agent)
    monkeypatch.setattr(chat_mod, "_remember_conversation", fake_remember)
    monkeypatch.setattr(chat_mod, "_apply_chat_energy_effects", fake_energy)

    events = [
        event
        async for event in chat_mod.stream_chat_events(
            user_message="Please remember this",
            history=[],
            llm_config={"provider": "openai", "model": "gpt-4o"},
            dsn="postgresql://unused",
            pool=db_pool,
            session_id=session_id,
            client_message_id=client_message_id,
        )
    ]

    assert any(event.event == AgentEvent.TOOL_RESULT for event in events)
    assert captured["agent_turn_id"] == turn_id
    assert "action_receipts" not in captured


async def test_chat_turn_persists_user_before_agent_and_reuses_client_identity(monkeypatch, db_pool):
    session_id = str(uuid4())
    client_message_id = str(uuid4())
    source_identity = f"chat:{session_id}:{client_message_id}"
    call_order = []
    remembered = {}
    async with db_pool.acquire() as connection:
        await connection.execute(
            "INSERT INTO config (key, value, description) VALUES ('chat.use_rlm', 'false', 'test override') "
            "ON CONFLICT (key) DO UPDATE SET value='false'"
        )

    async def fake_capture(*_args, **_kwargs):
        return {}

    async def fake_agent_profile(*_args, **_kwargs):
        return {}

    async def fake_run_agent(*_args, **kwargs):
        call_order.append("agent")
        assert kwargs["chat_message_projection"] is True
        assert kwargs["chat_source_identity"] == source_identity
        assert kwargs["chat_work_execution_id"] is None
        async with db_pool.acquire() as connection:
            user = await connection.fetchrow(
                "SELECT role,content,content_blocks,source_message_id FROM chat_messages "
                "WHERE session_id=$1::uuid AND source_message_id=$2",
                session_id,
                f"{source_identity}:user",
            )
            turns = await connection.fetchval(
                "SELECT count(*) FROM agent_turns WHERE session_id=$1::uuid",
                session_id,
            )
        assert user is not None
        assert user["role"] == "user"
        assert user["content"] == "Question with one source identity."
        blocks = (
            json.loads(user["content_blocks"])
            if isinstance(user["content_blocks"], str)
            else user["content_blocks"]
        )
        assert blocks == [
            {"type": "text", "text": "Question with one source identity."}
        ]
        assert user["source_message_id"] == f"{source_identity}:user"
        assert turns == 0
        return SimpleNamespace(
            text="Answer.", turn_id=str(uuid4()), tool_results=[], energy_spent=0
        )

    async def fake_remember(_client, **kwargs):
        call_order.append("complete")
        remembered.update(kwargs)
        context = {
            "source_identity": kwargs["source_identity"],
            "user_source_message_id": kwargs["user_source_message_id"],
            "assistant_source_message_id": kwargs["assistant_source_message_id"],
            "user_metadata": {
                "submission_fingerprint": kwargs["submission_fingerprint"],
            },
            "assistant_metadata": {"agent_turn_id": kwargs["agent_turn_id"]},
            "surface": "api",
        }
        async with db_pool.acquire() as connection:
            await connection.fetchval(
                "SELECT record_chat_session_turn($1::uuid,$2,$3,'api',$4::jsonb)",
                session_id,
                kwargs["user_message"],
                kwargs["assistant_message"],
                json.dumps(context),
            )
        return {"memory": {"raw": {"status": "stored"}}}

    async def fake_energy(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(chat_mod, "_capture_operator_policy_for_turn", fake_capture)
    monkeypatch.setattr(chat_mod, "get_agent_profile_context", fake_agent_profile)
    async def fake_registry(*_args, **_kwargs):
        return object()

    monkeypatch.setattr(chat_mod, "create_full_registry", fake_registry)
    monkeypatch.setattr(chat_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(chat_mod, "_remember_conversation", fake_remember)
    monkeypatch.setattr(chat_mod, "_apply_chat_energy_effects", fake_energy)

    result = await chat_mod.chat_turn(
        user_message="Question with one source identity.",
        history=[],
        llm_config={"provider": "openai", "model": "gpt-4o"},
        session_id=session_id,
        client_message_id=client_message_id,
        pool=db_pool,
        surface="api",
    )

    assert result["assistant"] == "Answer."
    assert result["client_message_id"] == client_message_id
    assert call_order == ["agent", "complete"]
    assert remembered["source_identity"] == source_identity
    assert remembered["user_source_message_id"] == f"{source_identity}:user"
    assert remembered["assistant_source_message_id"] == f"{source_identity}:assistant"
    async with db_pool.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM chat_messages WHERE session_id=$1::uuid "
            "AND source_message_id=$2 AND role='user'",
            session_id,
            f"{source_identity}:user",
        ) == 1
        assistant_metadata = await connection.fetchval(
            "SELECT metadata FROM chat_messages WHERE session_id=$1::uuid "
            "AND source_message_id=$2 AND role='assistant'",
            session_id,
            f"{source_identity}:assistant",
        )
        assert assistant_metadata is not None
        assistant_metadata = (
            json.loads(assistant_metadata)
            if isinstance(assistant_metadata, str)
            else assistant_metadata
        )
        assert "action_receipts" not in assistant_metadata

    replay = await chat_mod.chat_turn(
        user_message="Question with one source identity.",
        history=[],
        llm_config={"provider": "openai", "model": "gpt-4o"},
        session_id=session_id,
        client_message_id=client_message_id,
        pool=db_pool,
        surface="api",
    )
    assert replay["assistant"] == "Answer."
    assert replay["client_message_id"] == client_message_id
    assert call_order == ["agent", "complete"]

    with pytest.raises(chat_mod.ChatRequestIdentityError, match="conflicts"):
        await chat_mod.chat_turn(
            user_message="Same identity with changed body.",
            history=[],
            llm_config={"provider": "openai", "model": "gpt-4o"},
            session_id=session_id,
            client_message_id=client_message_id,
            pool=db_pool,
            surface="api",
        )
    with pytest.raises(chat_mod.ChatRequestIdentityError, match="conflicts"):
        await chat_mod.chat_turn(
            user_message="Question with one source identity.",
            history=[],
            llm_config={"provider": "openai", "model": "gpt-4o"},
            session_id=session_id,
            client_message_id=client_message_id,
            attachments=[{"name": "different.pdf"}],
            pool=db_pool,
            surface="api",
        )
    assert call_order == ["agent", "complete"]
