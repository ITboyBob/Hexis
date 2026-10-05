from __future__ import annotations

import json
from uuid import uuid4

import pytest

from services.voice_conversation_history import VoiceConversationHistory

pytestmark = [pytest.mark.asyncio(loop_scope="function")]


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
        user_text="  authorized words  ",
        context_version="context-v1",
    )

    sql, args = pool.calls[0]
    runtime_state = json.loads(args[3])["runtime_state"]
    assert "start_agent_turn" in sql
    assert (args[0], args[1], args[2]) == (
        "voice",
        "  authorized words  ",
        session_id,
    )
    assert runtime_state == {
        "channel": "voice",
        "input_turn_id": str(input_turn_id),
        "input_order": 4,
        "context_version": "context-v1",
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
