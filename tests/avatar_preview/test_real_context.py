import os

import pytest

from services.avatar_emotion import read_db_context


@pytest.mark.asyncio
async def test_real_context_has_only_four_classes():
    variable_name = os.getenv("AVATAR_TEST_DSN_ENV")
    if not variable_name:
        pytest.skip("未显式选择真实 DB DSN 变量")
    dsn = os.environ[variable_name]
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction(readonly=True):
            context = await read_db_context(conn)
        assert set(context) == {"identity", "worldview", "goals", "relationships"}
    finally:
        await conn.close()
