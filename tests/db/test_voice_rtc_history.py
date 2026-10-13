"""RTC 输入只存元数据，生成文字与实际播放事实分开保存。"""

from uuid import UUID, uuid4
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import json

import pytest

from services.voice_rtc_history import RtcVoiceHistory
from services.voice_conversation_history import VoiceConversationHistory
from services.m5_task_service import M5TaskService
from services.voice_rtc_api import RtcRuntime, Entry

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_audio_turn_has_no_user_text_and_generated_message_is_idempotent(db_pool):
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            session_id = await conn.fetchval(
                "INSERT INTO chat_sessions DEFAULT VALUES RETURNING id"
            )
            media_id = await conn.fetchval(
                "INSERT INTO voice_realtime_sessions(session_id) VALUES($1) RETURNING id",
                session_id,
            )

            # 使用固定连接，让所有调用处于同一可回滚事务内。
            class Pool:
                def acquire(self):
                    return Lease()

            class Lease:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *args):
                    return False

            history = RtcVoiceHistory(Pool())
            identity = dict(
                session_id=session_id,
                media_session_id=media_id,
                input_turn_id=uuid4(),
                input_order=0,
                provider_item_id="real-audio-item",
                context_version="a" * 64,
            )
            turn_id = await history.start_audio_input(**identity)
            assert await history.start_audio_input(**identity) == turn_id
            assert (
                await conn.fetchval(
                    "SELECT user_message FROM agent_turns WHERE id=$1", turn_id
                )
                is None
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM chat_messages WHERE session_id=$1 AND role='user'",
                    session_id,
                )
                == 0
            )
            output = dict(
                session_id=session_id,
                media_session_id=media_id,
                input_turn_id=identity["input_turn_id"],
                turn_id=turn_id,
                response_id="real-response",
                assistant_text="模型生成的回复",
                status="completed",
                context_version="a" * 64,
                playback_scope="unknown",
                final_for_turn=True,
            )
            await history.save_generated_output(**output)
            await history.save_generated_output(**output)
            rows = await conn.fetch(
                "SELECT content,metadata FROM chat_messages WHERE session_id=$1 AND role='assistant'",
                session_id,
            )
            assert len(rows) == 1
            assert rows[0]["content"] == "模型生成的回复"
            import json

            metadata = json.loads(rows[0]["metadata"])
            assert metadata["playback_scope"] == "unknown"
            assert metadata["content_source"] == "model_generation"
            with pytest.raises(Exception):
                async with conn.transaction():
                    await history.save_generated_output(
                        **{**output, "assistant_text": "冲突内容"}
                    )
        finally:
            await transaction.rollback()


class LeasePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *args):
        return False


async def audio_input(conn):
    session = await conn.fetchval(
        "INSERT INTO chat_sessions DEFAULT VALUES RETURNING id"
    )
    media = await conn.fetchval(
        "INSERT INTO voice_realtime_sessions(session_id) VALUES($1) RETURNING id",
        session,
    )
    identity = dict(
        session_id=session,
        media_session_id=media,
        input_turn_id=uuid4(),
        input_order=0,
        provider_item_id="audio-item",
        context_version="a" * 64,
    )
    turn = await RtcVoiceHistory(LeasePool(conn)).start_audio_input(**identity)
    return identity, turn


async def test_rtc_tool_projection_and_task_source_metadata(db_pool):
    async with db_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            identity, turn = await audio_input(conn)
            pool = LeasePool(conn)
            history = VoiceConversationHistory(pool)
            registered = await history.register_tool_call(
                turn_id=turn,
                call_id="call-recall",
                tool_name="voice_recall",
                arguments={"query": "测试记忆"},
            )
            assert registered["status"] == "registered"
            await history.record_tool_result(
                turn_id=turn,
                call_id="call-recall",
                tool_name="voice_recall",
                arguments={"query": "测试记忆"},
                output={"status": "no_results"},
                output_text='{"status":"no_results"}',
                success=True,
            )
            repeated = await history.register_tool_call(
                turn_id=turn,
                call_id="call-recall",
                tool_name="voice_recall",
                arguments={"query": "测试记忆"},
            )
            assert repeated["status"] == "completed"
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM chat_messages WHERE session_id=$1 AND role='user'",
                    identity["session_id"],
                )
                == 0
            )
            command = uuid4()
            auth = {
                "origin_session_id": str(identity["session_id"]),
                "input_turn_id": str(identity["input_turn_id"]),
                "input_order": 0,
                "stimulus_revision": 0,
                "response_id": "rtc-response",
                "supplier_response_id": "rtc-response",
                "request_id": "rtc-event",
                "actual_call_id": "call-submit",
                "input_modality": "audio",
                "media_session_id": str(identity["media_session_id"]),
                "provider_item_id": "audio-item",
            }
            service = M5TaskService(pool)
            result = await service.submit_m5_task(
                identity["session_id"], command, "由模型理解得到的任务参数", None, auth
            )
            assert result["accepted"] is True
            row = await conn.fetchrow(
                "SELECT original_user_request,source_input FROM m5_tasks WHERE id=$1",
                UUID(result["task_id"]),
            )
            assert row["original_user_request"] is None
            assert json.loads(row["source_input"])["provider_item_id"] == "audio-item"
            await conn.execute(
                "UPDATE voice_realtime_sessions SET status='closed' WHERE id=$1",
                identity["media_session_id"],
            )
            replay = await service.submit_m5_task(
                identity["session_id"], command, "由模型理解得到的任务参数", None, auth
            )
            assert replay["replayed"] is True
            with pytest.raises(Exception):
                async with conn.transaction():
                    await service.submit_m5_task(
                        identity["session_id"],
                        uuid4(),
                        "关闭后不接受新任务",
                        None,
                        auth,
                    )
        finally:
            await tx.rollback()


async def test_close_settles_running_turn_and_migrations_are_idempotent(db_pool):
    async with db_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            identity, turn = await audio_input(conn)
            runtime = RtcRuntime(
                pool_provider=lambda: LeasePool(conn),
                config_loader=lambda: None,
                control_url=lambda _: "unused",
            )
            runtime.entries[identity["media_session_id"]] = Entry(
                SimpleNamespace(close=AsyncMock()), asyncio.Queue()
            )
            await runtime.close(identity["media_session_id"], "disconnect")
            assert (
                await conn.fetchval("SELECT status FROM agent_turns WHERE id=$1", turn)
                == "failed"
            )
            for name in ("0279_voice_rtc_metadata.sql", "0280_m5_rtc_source_input.sql"):
                await conn.execute(
                    (
                        Path(__file__).resolve().parents[2] / "db/migrations" / name
                    ).read_text()
                )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM chat_messages WHERE session_id=$1 AND role='user'",
                    identity["session_id"],
                )
                == 0
            )
        finally:
            await tx.rollback()


async def test_expired_owner_recovery_does_not_close_another_live_runtime(db_pool):
    async with db_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            identity, turn = await audio_input(conn)
            owner = uuid4()
            await conn.execute(
                "UPDATE voice_realtime_sessions SET rtc_owner_id=$2,rtc_lease_expires_at=now()+interval '5 minutes' WHERE id=$1",
                identity["media_session_id"],
                owner,
            )
            runtime = RtcRuntime(
                pool_provider=lambda: LeasePool(conn),
                config_loader=lambda: None,
                control_url=lambda _: "unused",
            )
            await runtime.reap_stale(identity["session_id"])
            assert (
                await conn.fetchval(
                    "SELECT status FROM voice_realtime_sessions WHERE id=$1",
                    identity["media_session_id"],
                )
                == "active"
            )
            from fastapi import HTTPException

            with pytest.raises(HTTPException) as denied:
                await runtime.close(identity["media_session_id"], "manual")
            assert denied.value.status_code == 409
            await conn.execute(
                "UPDATE voice_realtime_sessions SET rtc_lease_expires_at=now()-interval '1 second' WHERE id=$1",
                identity["media_session_id"],
            )
            await runtime.reap_stale(identity["session_id"])
            assert (
                await conn.fetchval(
                    "SELECT status FROM voice_realtime_sessions WHERE id=$1",
                    identity["media_session_id"],
                )
                == "failed"
            )
            assert (
                await conn.fetchval("SELECT status FROM agent_turns WHERE id=$1", turn)
                == "failed"
            )
            await runtime.close(identity["media_session_id"], "manual")
        finally:
            await tx.rollback()


@pytest.mark.parametrize("reason", ["manual", "disconnect"])
async def test_late_generation_preserves_closed_turn_terminal_facts(db_pool, reason):
    async with db_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            identity, turn = await audio_input(conn)
            runtime = RtcRuntime(
                pool_provider=lambda: LeasePool(conn),
                config_loader=lambda: None,
                control_url=lambda _: "unused",
            )
            runtime.entries[identity["media_session_id"]] = Entry(
                SimpleNamespace(close=AsyncMock()), asyncio.Queue()
            )
            await runtime.close(identity["media_session_id"], reason)
            query = (
                "SELECT status,stopped_reason,completed_at FROM agent_turns WHERE id=$1"
            )
            closed = await conn.fetchrow(query, turn)
            await RtcVoiceHistory(LeasePool(conn)).save_generated_output(
                session_id=identity["session_id"],
                media_session_id=identity["media_session_id"],
                input_turn_id=identity["input_turn_id"],
                turn_id=turn,
                response_id="late-response",
                assistant_text="迟到的生成文字",
                status="completed",
                context_version=identity["context_version"],
                playback_scope="unknown",
                final_for_turn=True,
            )
            assert await conn.fetchrow(query, turn) == closed
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM voice_rtc_outputs WHERE media_session_id=$1",
                    identity["media_session_id"],
                )
                == 1
            )
        finally:
            await tx.rollback()
