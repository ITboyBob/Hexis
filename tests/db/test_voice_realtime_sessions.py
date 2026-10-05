from __future__ import annotations

import asyncpg
import pytest

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


async def test_voice_realtime_sessions_allow_one_active_and_rebuild(db_pool):
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            session_id = await conn.fetchval(
                "INSERT INTO chat_sessions DEFAULT VALUES RETURNING id"
            )
            first_id = await conn.fetchval(
                """
                INSERT INTO voice_realtime_sessions
                    (session_id, qwen_session_id, status, model, endpoint, instructions_hash)
                VALUES ($1::uuid, 'qwen-1', 'active', 'model-1', 'wss://example', $2)
                RETURNING id
                """,
                session_id,
                "a" * 64,
            )

            with pytest.raises(asyncpg.UniqueViolationError):
                async with conn.transaction():
                    await conn.fetchval(
                        """
                        INSERT INTO voice_realtime_sessions
                            (session_id, qwen_session_id, status)
                        VALUES ($1::uuid, 'qwen-2', 'active')
                        """,
                        session_id,
                    )

            await conn.execute(
                """
                UPDATE voice_realtime_sessions
                SET status='closed', closed_reason='manual', closed_at=now(),
                    last_event_at=now()
                WHERE id=$1::uuid
                """,
                first_id,
            )
            second_id = await conn.fetchval(
                """
                INSERT INTO voice_realtime_sessions
                    (session_id, qwen_session_id, status, rebuild_count)
                VALUES ($1::uuid, 'qwen-3', 'active', 1)
                RETURNING id
                """,
                session_id,
            )

            assert str(first_id) != str(second_id)
            assert (
                await conn.fetchval(
                    """
                    SELECT count(*)
                    FROM voice_realtime_sessions
                    WHERE session_id=$1::uuid AND status='active'
                    """,
                    session_id,
                )
                == 1
            )
        finally:
            await transaction.rollback()
