"""Upgrade an M2-only database without changing its legacy task records."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest


pytestmark = [pytest.mark.asyncio(loop_scope="session")]
_MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"


async def test_m5_s4_forward_migration_preserves_m2_tasks(db_pool, tmp_path):
    from core.migrations import apply_pending_migrations, migration_status

    async with db_pool.acquire() as conn:
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
            await conn.execute(
                "DROP FUNCTION IF EXISTS public.append_m5_message(uuid,uuid,uuid,text,jsonb)"
            )
            await conn.execute(
                "DROP FUNCTION IF EXISTS public.claim_next_m5_message(uuid)"
            )
            await conn.execute(
                "DROP FUNCTION IF EXISTS public.finish_m5_round(uuid,uuid,text)"
            )
            await conn.execute(
                "DROP FUNCTION IF EXISTS public.finish_m5_work_round(uuid,uuid,text,jsonb,text,uuid)"
            )
            await conn.execute(
                "DROP FUNCTION IF EXISTS public.claim_m5_work_round(uuid)"
            )
            await conn.execute(
                "DROP FUNCTION IF EXISTS public.submit_m5_task(uuid,uuid,text,text,jsonb)"
            )
            await conn.execute(
                "DROP TABLE IF EXISTS public.m5_pending_messages CASCADE"
            )
            await conn.execute(
                "DROP TABLE IF EXISTS public.m5_tasks, public.m5_work_rounds, public.m5_actions CASCADE"
            )
            await conn.execute(
                "DELETE FROM public.schema_migrations WHERE version IN ($1,$2)",
                "0262_m5_tasks",
                "0263_m5_pending",
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
            assert not await conn.fetchval(
                "SELECT to_regclass('public.m5_pending_messages') IS NOT NULL"
            )
            assert await apply_pending_migrations(conn, migrations_dir=tmp_path) == []

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


async def _m5_s5_schema_snapshot(conn):
    tables = await conn.fetch(
        """
        SELECT table_name, column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema='public'
          AND table_name IN ('m5_actions','m5_tasks','m5_work_rounds','m5_pending_messages')
        ORDER BY table_name, ordinal_position
        """
    )
    constraints = await conn.fetch(
        """
        SELECT t.relname, c.conname, c.contype, c.condeferrable, c.condeferred,
               pg_get_constraintdef(c.oid)
        FROM pg_constraint c
        JOIN pg_class t ON t.oid=c.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname='public'
          AND t.relname IN ('m5_actions','m5_tasks','m5_work_rounds','m5_pending_messages')
        ORDER BY t.relname, c.conname
        """
    )
    indexes = await conn.fetch(
        """
        SELECT tablename,indexname,indexdef FROM pg_indexes
        WHERE schemaname='public'
          AND tablename IN ('m5_actions','m5_tasks','m5_work_rounds','m5_pending_messages')
        ORDER BY tablename,indexname
        """
    )
    functions = await conn.fetch(
        """
        SELECT p.proname,pg_get_functiondef(p.oid)
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND p.proname IN (
            'submit_m5_task','claim_m5_work_round',
            'claim_next_m5_message','append_m5_message','finish_m5_round'
        ) ORDER BY p.proname
        """
    )
    return tuple(
        tuple(tuple(row.values()) for row in group)
        for group in (tables, constraints, indexes, functions)
    )


async def test_m5_pending_forward_matches_baseline_and_preserves_work(db_pool, tmp_path):
    from core.migrations import apply_pending_migrations
    from services.m5_task_service import M5TaskService

    async with db_pool.acquire() as conn:
        final_snapshot = await _m5_s5_schema_snapshot(conn)
        origin = await conn.fetchval(
            "SELECT (get_or_create_chat_session(NULL,'chat',$1,'{}'::jsonb)->>'session_id')::uuid",
            f"m5-pending-migration-{uuid4()}",
        )
    task = await M5TaskService(db_pool).submit_m5_task(
        origin,
        uuid4(),
        "Preserve this S4 work round",
        "Preserve the original request in the new task model.",
        {
            "origin_session_id": str(origin),
            "input_turn_id": str(uuid4()),
            "input_order": 1,
            "stimulus_revision": 0,
            "response_id": "local-migration",
            "supplier_response_id": "qwen-migration",
            "request_id": "event-migration",
            "actual_call_id": "call-migration",
        },
    )

    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            await conn.execute("DROP FUNCTION IF EXISTS public.claim_next_m5_message(uuid)")
            await conn.execute(
                "DROP FUNCTION IF EXISTS public.append_m5_message(uuid,uuid,uuid,text,jsonb)"
            )
            await conn.execute("DROP FUNCTION IF EXISTS public.finish_m5_round(uuid,uuid,text)")
            await conn.execute("DROP TABLE IF EXISTS public.m5_pending_messages CASCADE")
            await conn.execute(
                "ALTER TABLE public.m5_work_rounds DROP CONSTRAINT IF EXISTS m5_work_rounds_message_id_key"
            )
            await conn.execute(
                "DELETE FROM public.schema_migrations WHERE version=$1",
                "0263_m5_pending",
            )

            s4_dir = tmp_path / "s4"
            s4_dir.mkdir()
            s4_migration = _MIGRATIONS / "0262_m5_tasks.sql"
            (s4_dir / s4_migration.name).write_bytes(s4_migration.read_bytes())
            await conn.execute(
                "DELETE FROM public.schema_migrations WHERE version=$1",
                "0262_m5_tasks",
            )
            assert await apply_pending_migrations(conn, migrations_dir=s4_dir) == [
                "0262_m5_tasks"
            ]
            s4_snapshot = await _m5_s5_schema_snapshot(conn)

            pending_dir = tmp_path / "s5"
            pending_dir.mkdir()
            pending_migration = _MIGRATIONS / "0263_m5_pending.sql"
            (pending_dir / pending_migration.name).write_bytes(
                pending_migration.read_bytes()
            )
            assert await apply_pending_migrations(
                conn, migrations_dir=pending_dir
            ) == ["0263_m5_pending"]
            assert await apply_pending_migrations(
                conn, migrations_dir=pending_dir
            ) == []
            assert await _m5_s5_schema_snapshot(conn) == final_snapshot

            work = await conn.fetchrow(
                "SELECT task_id,input,status,message_id FROM m5_work_rounds WHERE id=$1",
                task["work_execution_id"],
            )
            assert str(work["task_id"]) == task["task_id"]
            assert work["input"] == "Preserve this S4 work round"
            assert work["status"] == "claimed"
            assert work["message_id"] is None
        finally:
            await transaction.rollback()


async def _m5_s6_schema_snapshot(conn):
    tables = await conn.fetch(
        """
        SELECT table_name,column_name,data_type,is_nullable,column_default
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name='m5_reports'
        ORDER BY ordinal_position
        """
    )
    constraints = await conn.fetch(
        """
        SELECT c.conname,c.contype,c.condeferrable,c.condeferred,pg_get_constraintdef(c.oid)
        FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname='public' AND t.relname='m5_reports'
        ORDER BY c.conname
        """
    )
    indexes = await conn.fetch(
        "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public' "
        "AND tablename IN ('m5_reports','m5_tasks') ORDER BY indexname"
    )
    functions = await conn.fetch(
        """
        SELECT p.proname,pg_get_functiondef(p.oid)
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND p.proname IN ('finish_m5_work_round','deliver_m5_report')
        ORDER BY p.proname
        """
    )
    return tuple(tuple(tuple(row.values()) for row in group) for group in (tables, constraints, indexes, functions))


async def test_m5_reports_forward_matches_baseline(db_pool, tmp_path):
    from core.migrations import apply_pending_migrations, migration_status

    async with db_pool.acquire() as conn:
        expected = await _m5_s6_schema_snapshot(conn)
        transaction = conn.transaction()
        await transaction.start()
        try:
            await conn.execute("DROP FUNCTION IF EXISTS public.deliver_m5_report(uuid,uuid,text,text)")
            await conn.execute("DROP TABLE IF EXISTS public.m5_reports CASCADE")
            await conn.execute("DROP INDEX IF EXISTS public.idx_m5_reports_pending")
            await conn.execute("DROP INDEX IF EXISTS public.idx_m5_tasks_id_origin")
            await conn.execute(
                "DELETE FROM public.schema_migrations WHERE version=$1",
                "0264_m5_reports",
            )
            migration = _MIGRATIONS / "0264_m5_reports.sql"
            (tmp_path / migration.name).write_bytes(migration.read_bytes())
            assert await apply_pending_migrations(conn, migrations_dir=tmp_path) == [
                "0264_m5_reports"
            ]
            assert (await migration_status(conn, migrations_dir=tmp_path))["pending"] == []
            assert await _m5_s6_schema_snapshot(conn) == expected
        finally:
            await transaction.rollback()


async def _m5_s7_function_snapshot(conn):
    rows = await conn.fetch(
        """
        SELECT p.proname,pg_get_functiondef(p.oid)
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND p.proname IN (
            'reserve_m5_report_attempt','record_m5_report_failure',
            'finish_m5_report_terminal'
        )
        ORDER BY p.proname
        """
    )
    return tuple(tuple(row.values()) for row in rows)


async def test_m5_report_recovery_forward_matches_baseline(db_pool, tmp_path):
    from core.migrations import apply_pending_migrations, migration_status

    async with db_pool.acquire() as conn:
        expected = await _m5_s7_function_snapshot(conn)
        assert {row[0] for row in expected} == {
            "finish_m5_report_terminal",
            "record_m5_report_failure",
            "reserve_m5_report_attempt",
        }
        transaction = conn.transaction()
        await transaction.start()
        try:
            await conn.execute(
                "DROP FUNCTION IF EXISTS public.finish_m5_report_terminal(uuid,text,text,text,text)"
            )
            await conn.execute(
                "DROP FUNCTION IF EXISTS public.record_m5_report_failure(uuid,text,text,text,text)"
            )
            await conn.execute(
                "DROP FUNCTION IF EXISTS public.reserve_m5_report_attempt(uuid,text)"
            )
            await conn.execute(
                "DELETE FROM public.schema_migrations WHERE version=$1",
                "0265_m5_report_recovery",
            )
            migration = _MIGRATIONS / "0265_m5_report_recovery.sql"
            (tmp_path / migration.name).write_bytes(migration.read_bytes())
            assert await apply_pending_migrations(conn, migrations_dir=tmp_path) == [
                "0265_m5_report_recovery"
            ]
            assert (await migration_status(conn, migrations_dir=tmp_path))["pending"] == []
            assert await _m5_s7_function_snapshot(conn) == expected
        finally:
            await transaction.rollback()
