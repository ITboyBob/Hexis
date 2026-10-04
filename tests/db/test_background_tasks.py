import json
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


async def test_background_task_lifecycle_and_execution_guard(db_pool):
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            session_id = await conn.fetchval(
                "INSERT INTO chat_sessions DEFAULT VALUES RETURNING id"
            )
            command_id = uuid4()
            task_id = await conn.fetchval(
                "SELECT create_background_task($1::uuid, $2::uuid, $3::jsonb)",
                session_id,
                command_id,
                json.dumps(
                    {"instruction": "测试任务", "success_criteria": "text"}
                ),
            )
            retry_id = await conn.fetchval(
                "SELECT create_background_task($1::uuid, $2::uuid, $3::jsonb)",
                session_id,
                command_id,
                json.dumps(
                    {"instruction": "测试任务", "success_criteria": "text"}
                ),
            )
            assert str(task_id) == str(retry_id)

            old_execution = await conn.fetchval(
                "SELECT claim_background_task($1::uuid)", task_id
            )
            assert old_execution is not None
            assert (
                await conn.fetchval(
                    "SELECT status FROM background_tasks WHERE id=$1", task_id
                )
                == "running"
            )

            new_execution = await conn.fetchval(
                "SELECT claim_background_task($1::uuid)", task_id
            )
            assert new_execution is not None
            assert str(new_execution) != str(old_execution)

            failed = await conn.fetchval(
                "SELECT fail_background_task($1::uuid, $2::uuid, $3::text, $4::uuid)",
                task_id,
                old_execution,
                "旧实例失败",
                None,
            )
            assert failed is False
            completed = await conn.fetchval(
                "SELECT complete_background_task($1::uuid, $2::uuid, $3::jsonb, $4::uuid)",
                task_id,
                old_execution,
                json.dumps({"text": "旧实例结果"}),
                None,
            )
            assert completed is False
            assert (
                await conn.fetchval(
                    "SELECT status FROM background_tasks WHERE id=$1", task_id
                )
                == "running"
            )

            completed = await conn.fetchval(
                "SELECT complete_background_task($1::uuid, $2::uuid, $3::jsonb, $4::uuid)",
                task_id,
                new_execution,
                json.dumps({"text": "新实例结果"}),
                None,
            )
            assert completed is True
            task = await conn.fetchrow(
                """
                SELECT status, result, error, current_execution_id, completed_at
                FROM background_tasks WHERE id=$1
                """,
                task_id,
            )
            assert task["status"] == "completed"
            assert json.loads(task["result"]) == {"text": "新实例结果"}
            assert task["error"] is None
            assert task["current_execution_id"] == new_execution
            assert task["completed_at"] is not None

            assert (
                await conn.fetchval(
                    "SELECT claim_background_task($1::uuid)", task_id
                )
                is None
            )
        finally:
            await transaction.rollback()


async def test_background_task_unknown_is_reclaimable(db_pool):
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            session_id = await conn.fetchval(
                "INSERT INTO chat_sessions DEFAULT VALUES RETURNING id"
            )
            task_id = await conn.fetchval(
                "SELECT create_background_task($1::uuid, $2::uuid, $3::jsonb)",
                session_id,
                uuid4(),
                json.dumps(
                    {"instruction": "无法确认结果", "success_criteria": "text"}
                ),
            )
            old_execution = await conn.fetchval(
                "SELECT claim_background_task($1::uuid)", task_id
            )

            unknown = await conn.fetchval(
                "SELECT mark_background_task_unknown($1::uuid, $2::uuid, $3::text, $4::uuid)",
                task_id,
                old_execution,
                "结果不可确认",
                None,
            )
            assert unknown is True
            task = await conn.fetchrow(
                """
                SELECT status, error, completed_at, current_execution_id
                FROM background_tasks WHERE id=$1
                """,
                task_id,
            )
            assert task["status"] == "unknown"
            assert task["error"] == "结果不可确认"
            assert task["completed_at"] is None
            assert task["current_execution_id"] == old_execution

            new_execution = await conn.fetchval(
                "SELECT claim_background_task($1::uuid)", task_id
            )
            assert new_execution is not None
            assert str(new_execution) != str(old_execution)
            assert (
                await conn.fetchval(
                    "SELECT status FROM background_tasks WHERE id=$1", task_id
                )
                == "running"
            )

            failed = await conn.fetchval(
                "SELECT fail_background_task($1::uuid, $2::uuid, $3::text, $4::uuid)",
                task_id,
                old_execution,
                "旧实例失败",
                None,
            )
            assert failed is False
            failed = await conn.fetchval(
                "SELECT fail_background_task($1::uuid, $2::uuid, $3::text, $4::uuid)",
                task_id,
                new_execution,
                "重试失败",
                None,
            )
            assert failed is True
            task = await conn.fetchrow(
                """
                SELECT status, error, current_execution_id, completed_at
                FROM background_tasks WHERE id=$1
                """,
                task_id,
            )
            assert task["status"] == "failed"
            assert task["error"] == "重试失败"
            assert task["current_execution_id"] == new_execution
            assert task["completed_at"] is not None
        finally:
            await transaction.rollback()
