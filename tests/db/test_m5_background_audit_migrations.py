"""M5 tool-surface audit migration and legacy-upgrade coverage."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

pytestmark = [pytest.mark.asyncio(loop_scope="session")]

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS = _ROOT / "db" / "migrations"
def _legacy_audit_function() -> str:
    source = (_MIGRATIONS / "0205_capability_reachability_audit.sql").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"CREATE OR REPLACE FUNCTION record_tool_surface_decision\(.*?"
        r"\$\$ LANGUAGE plpgsql;",
        source,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(0)


async def _decision_constraint(conn) -> str:
    row = await conn.fetchrow(
        """
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN unnest(c.conkey) AS key(attnum) ON TRUE
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = key.attnum
        WHERE n.nspname = 'public'
          AND t.relname = 'tool_surface_decision_events'
          AND c.contype = 'c'
          AND a.attname = 'decision_kind'
        """
    )
    assert row is not None
    return row["conname"]


async def _restore_legacy_audit(conn) -> list[str]:
    old_rows = []
    for decision_kind in ("selection", "skill_activation"):
        row_id = await conn.fetchval(
            """
            INSERT INTO public.tool_surface_decision_events
                (tool_context, decision_kind, input_text_hash)
            VALUES ('chat', $1, $2)
            RETURNING id
            """,
            decision_kind,
            uuid4().hex * 2,
        )
        old_rows.append(row_id)

    constraint = await _decision_constraint(conn)
    await conn.execute(
        f'ALTER TABLE public.tool_surface_decision_events DROP CONSTRAINT "{constraint}"'
    )
    await conn.execute(
        """
        ALTER TABLE public.tool_surface_decision_events
        ADD CONSTRAINT tool_surface_decision_events_decision_kind_check
        CHECK (decision_kind IN ('selection', 'skill_activation'))
        """
    )
    await conn.execute(_legacy_audit_function())
    return old_rows


async def _run_only_m5_audit_migrations(conn, tmp_path: Path) -> list[str]:
    from core.migrations import apply_pending_migrations

    versions = (
        "0260_m5_background_audit_expand.sql",
        "0261_m5_background_audit_validate.sql",
    )
    for version in versions:
        (tmp_path / version).write_bytes((_MIGRATIONS / version).read_bytes())
    return await apply_pending_migrations(conn, migrations_dir=tmp_path)


async def test_background_default_is_not_collapsed_to_selection(db_pool):
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            event_id = await conn.fetchval(
                """
                SELECT public.record_tool_surface_decision(
                    NULL, 'background', 'chat', 'background_default', $1,
                    ARRAY[]::text[], '[]'::jsonb, ARRAY['recall']::text[],
                    ARRAY['recall']::text[], ARRAY[]::text[], 0, 'default'
                )
                """,
                uuid4().hex * 2,
            )
            assert await conn.fetchval(
                "SELECT decision_kind FROM public.tool_surface_decision_events WHERE id = $1",
                event_id,
            ) == "background_default"
        finally:
            await transaction.rollback()


async def test_legacy_audit_schema_upgrades_and_persists_background_default(
    db_pool, tmp_path
):
    from core.migrations import apply_pending_migrations

    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            old_rows = await _restore_legacy_audit(conn)
            await conn.execute(
                "DELETE FROM public.schema_migrations WHERE version IN ($1, $2)",
                "0260_m5_background_audit_expand",
                "0261_m5_background_audit_validate",
            )

            applied = await _run_only_m5_audit_migrations(conn, tmp_path)
            assert applied == [
                "0260_m5_background_audit_expand",
                "0261_m5_background_audit_validate",
            ]

            constraint = await _decision_constraint(conn)
            constraint_state = await conn.fetchrow(
                """
                SELECT c.convalidated, pg_get_constraintdef(c.oid) AS definition
                FROM pg_constraint c
                WHERE c.conrelid = 'public.tool_surface_decision_events'::regclass
                  AND c.conname = $1
                """,
                constraint,
            )
            assert constraint_state["convalidated"] is True
            assert "background_default" in constraint_state["definition"]

            session_id = uuid4()
            event_id = await conn.fetchval(
                """
                SELECT public.record_tool_surface_decision(
                    $1, 'background', 'chat', 'background_default', $2,
                    ARRAY[]::text[], '[]'::jsonb,
                    ARRAY['recall', 'remember']::text[],
                    ARRAY['recall', 'remember']::text[], ARRAY[]::text[], 4, 'default'
                )
                """,
                session_id,
                uuid4().hex * 2,
            )
            saved = await conn.fetchrow(
                """
                SELECT decision_kind, allowed_tools, reachable_tools, available_skill_count
                FROM public.tool_surface_decision_events WHERE id = $1
                """,
                event_id,
            )
            assert saved["decision_kind"] == "background_default"
            assert saved["allowed_tools"] == ["recall", "remember"]
            assert saved["reachable_tools"] == ["recall", "remember"]
            assert saved["available_skill_count"] == 4

            preserved_kinds = []
            for kind in ("selection", "skill_activation"):
                preserved_id = await conn.fetchval(
                    """
                    SELECT public.record_tool_surface_decision(
                        NULL, 'chat', 'chat', $1, $2, ARRAY[]::text[],
                        '[]'::jsonb, ARRAY[]::text[], ARRAY[]::text[],
                        ARRAY[]::text[], 0, 'default'
                    )
                    """,
                    kind,
                    uuid4().hex * 2,
                )
                preserved_kinds.append(
                    await conn.fetchval(
                        "SELECT decision_kind FROM public.tool_surface_decision_events WHERE id = $1",
                        preserved_id,
                    )
                )
            assert preserved_kinds == ["selection", "skill_activation"]

            unknown_id = await conn.fetchval(
                """
                SELECT public.record_tool_surface_decision(
                    NULL, 'chat', 'chat', 'unexpected-kind', $1,
                    ARRAY[]::text[], '[]'::jsonb, ARRAY[]::text[],
                    ARRAY[]::text[], ARRAY[]::text[], 0, 'default'
                )
                """,
                uuid4().hex * 2,
            )
            assert await conn.fetchval(
                "SELECT decision_kind FROM public.tool_surface_decision_events WHERE id = $1",
                unknown_id,
            ) == "selection"

            assert await conn.fetch(
                "SELECT id FROM public.tool_surface_decision_events WHERE id = ANY($1::uuid[])",
                old_rows,
            )
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO public.tool_surface_decision_events
                            (tool_context, decision_kind, input_text_hash)
                        VALUES ('chat', 'not-valid', $1)
                        """,
                        uuid4().hex * 2,
                    )
            with pytest.raises(asyncpg.RaiseError):
                async with conn.transaction():
                    await conn.execute(
                        "DELETE FROM public.tool_surface_decision_events WHERE id = $1",
                        event_id,
                    )

            assert await apply_pending_migrations(conn, migrations_dir=tmp_path) == []
            migration_rows = await conn.fetch(
                """
                SELECT version, checksum, execution_ms FROM public.schema_migrations
                WHERE version IN ($1, $2) ORDER BY version
                """,
                "0260_m5_background_audit_expand",
                "0261_m5_background_audit_validate",
            )
            assert [row["version"] for row in migration_rows] == [
                "0260_m5_background_audit_expand",
                "0261_m5_background_audit_validate",
            ]
            assert all(row["checksum"] for row in migration_rows)
            assert all(row["execution_ms"] >= 0 for row in migration_rows)
        finally:
            await transaction.rollback()


async def test_expand_migration_failure_leaves_no_partial_schema_change(db_pool, tmp_path):
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            constraint = await _decision_constraint(conn)
            await conn.execute(
                f'ALTER TABLE public.tool_surface_decision_events DROP CONSTRAINT "{constraint}"'
            )
            await conn.execute(
                """
                ALTER TABLE public.tool_surface_decision_events
                ADD CONSTRAINT unexpected_decision_kind_check
                CHECK (decision_kind <> 'selection')
                """
            )
            await conn.execute(
                "DELETE FROM public.schema_migrations WHERE version = $1",
                "0260_m5_background_audit_expand",
            )
            with pytest.raises(asyncpg.PostgresError):
                await _run_only_m5_audit_migrations(conn, tmp_path)

            assert await conn.fetchval(
                "SELECT to_regclass('public.tool_surface_decision_events') IS NOT NULL"
            )
            assert await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'unexpected_decision_kind_check')"
            )
            assert not await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM public.schema_migrations WHERE version = $1)",
                "0260_m5_background_audit_expand",
            )
        finally:
            await transaction.rollback()


async def test_validate_failure_keeps_expansion_committed_and_pending(db_pool, tmp_path):
    from core.migrations import apply_pending_migrations

    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            constraint = await _decision_constraint(conn)
            await conn.execute(
                f'ALTER TABLE public.tool_surface_decision_events DROP CONSTRAINT "{constraint}"'
            )
            await conn.execute(
                """
                INSERT INTO public.tool_surface_decision_events
                    (tool_context, decision_kind, input_text_hash)
                VALUES ('chat', 'legacy-invalid', $1)
                """,
                uuid4().hex * 2,
            )
            await conn.execute(
                """
                ALTER TABLE public.tool_surface_decision_events
                ADD CONSTRAINT tool_surface_decision_events_decision_kind_check
                CHECK (decision_kind IN ('selection', 'skill_activation', 'background_default'))
                NOT VALID
                """
            )
            await conn.execute(
                "DELETE FROM public.schema_migrations WHERE version IN ($1, $2)",
                "0260_m5_background_audit_expand",
                "0261_m5_background_audit_validate",
            )
            expand_path = tmp_path / "0260_m5_background_audit_expand.sql"
            expand_path.write_bytes(
                (_MIGRATIONS / expand_path.name).read_bytes()
            )
            await apply_pending_migrations(conn, migrations_dir=tmp_path)
            validate_dir = tmp_path / "validate-only"
            validate_dir.mkdir()
            validate_path = validate_dir / "0261_m5_background_audit_validate.sql"
            validate_path.write_bytes(
                (_MIGRATIONS / validate_path.name).read_bytes()
            )

            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await apply_pending_migrations(
                        conn,
                        migrations_dir=validate_dir,
                    )

            assert await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM public.schema_migrations WHERE version = $1)",
                "0260_m5_background_audit_expand",
            )
            assert not await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM public.schema_migrations WHERE version = $1)",
                "0261_m5_background_audit_validate",
            )
            assert await conn.fetchval(
                """
                SELECT NOT convalidated FROM pg_constraint
                WHERE conrelid = 'public.tool_surface_decision_events'::regclass
                  AND conname = 'tool_surface_decision_events_decision_kind_check'
                """
            )
        finally:
            await transaction.rollback()
