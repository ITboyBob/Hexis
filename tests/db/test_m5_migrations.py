"""M5 additive baseline/forward parity and preservation of the M2 task model."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest


pytestmark = [pytest.mark.asyncio(loop_scope="session")]
_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS = _ROOT / "db" / "migrations"


async def _m5_schema_snapshot(conn):
    columns = await conn.fetch(
        """
        SELECT table_name, column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema='public'
          AND table_name IN ('m5_actions', 'm5_tasks', 'm5_work_rounds')
        ORDER BY table_name, ordinal_position
        """
    )
    constraints = await conn.fetch(
        """
        SELECT t.relname AS table_name, c.conname, c.contype,
               c.condeferrable, c.condeferred, pg_get_constraintdef(c.oid) AS definition
        FROM pg_constraint c
        JOIN pg_class t ON t.oid=c.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname='public'
          AND t.relname IN ('m5_actions', 'm5_tasks', 'm5_work_rounds')
        ORDER BY t.relname, c.conname
        """
    )
    indexes = await conn.fetch(
        """
        SELECT tablename, indexname, indexdef
        FROM pg_indexes
        WHERE schemaname='public'
          AND tablename IN ('m5_actions', 'm5_tasks', 'm5_work_rounds')
        ORDER BY tablename, indexname
        """
    )
    functions = await conn.fetch(
        """
        SELECT p.proname, pg_get_functiondef(p.oid) AS definition
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public'
          AND p.proname IN ('submit_m5_task', 'claim_m5_work_round', 'finish_m5_work_round')
        ORDER BY p.proname
        """
    )
    return tuple(
        tuple(tuple(row.values()) for row in group)
        for group in (columns, constraints, indexes, functions)
    )


async def test_m5_forward_migration_recreates_baseline_and_preserves_m2_rows(
    db_pool, tmp_path
):
    from core.migrations import apply_pending_migrations, migration_status

    async with db_pool.acquire() as conn:
        baseline_snapshot = await _m5_schema_snapshot(conn)
        origin_session_id = await conn.fetchval(
            "SELECT (get_or_create_chat_session(NULL, 'chat', $1, '{}'::jsonb)->>'session_id')::uuid",
            f"m5-migration-{uuid4()}",
        )
        legacy_task_id = await conn.fetchval(
            "SELECT create_background_task($1, $2, $3::jsonb)",
            origin_session_id,
            uuid4(),
            '{"instruction":"legacy M2","success_criteria":"text"}',
        )
        legacy_execution_id = await conn.fetchval(
            "SELECT claim_background_task($1)", legacy_task_id
        )
        legacy_before = await conn.fetchrow(
            "SELECT status, input, current_execution_id FROM background_tasks WHERE id=$1",
            legacy_task_id,
        )

        transaction = conn.transaction()
        await transaction.start()
        try:
            await conn.execute("DROP FUNCTION IF EXISTS public.submit_m5_task(uuid,uuid,text,text,jsonb)")
            await conn.execute("DROP FUNCTION IF EXISTS public.claim_m5_work_round(uuid)")
            await conn.execute(
                "DROP TABLE IF EXISTS public.m5_tasks, public.m5_work_rounds, public.m5_actions CASCADE"
            )
            await conn.execute(
                "DELETE FROM public.schema_migrations WHERE version=$1",
                "0262_m5_tasks",
            )
            migration = _MIGRATIONS / "0262_m5_tasks.sql"
            (tmp_path / migration.name).write_bytes(migration.read_bytes())

            assert await apply_pending_migrations(conn, migrations_dir=tmp_path) == [
                "0262_m5_tasks"
            ]
            status = await migration_status(conn, migrations_dir=tmp_path)
            assert status["pending"] == []
            assert await conn.fetchval(
                "SELECT to_regclass('public.m5_actions') IS NOT NULL"
                " AND to_regclass('public.m5_tasks') IS NOT NULL"
                " AND to_regclass('public.m5_work_rounds') IS NOT NULL"
            )
            assert await conn.fetchval(
                "SELECT to_regprocedure('public.submit_m5_task(uuid,uuid,text,text,jsonb)') IS NOT NULL"
            )
            assert await conn.fetchval(
                "SELECT to_regprocedure('public.claim_m5_work_round(uuid)') IS NOT NULL"
            )
            assert await apply_pending_migrations(conn, migrations_dir=tmp_path) == []
            forward_snapshot = await _m5_schema_snapshot(conn)
            assert forward_snapshot == baseline_snapshot

            legacy_after = await conn.fetchrow(
                "SELECT status, input, current_execution_id FROM background_tasks WHERE id=$1",
                legacy_task_id,
            )
            assert dict(legacy_after) == dict(legacy_before)
            assert await conn.fetchval(
                "SELECT status FROM background_task_executions WHERE id=$1",
                legacy_execution_id,
            ) == "running"
            ledger = await conn.fetchrow(
                "SELECT checksum, execution_ms FROM schema_migrations WHERE version=$1",
                "0262_m5_tasks",
            )
            assert ledger["checksum"]
            assert ledger["execution_ms"] >= 0
        finally:
            await transaction.rollback()
