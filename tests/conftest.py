"""Pytest configuration for the DanKagu test suite.

The environment is configured *before* ``dankagu.config`` is imported so that
tests never depend on a developer's local ``.env`` file, and never touch the
real player database.
"""

import os
from collections.abc import AsyncGenerator

import asyncpg
import pytest
import pytest_asyncio

# Placeholder JWT secret so token issuance can be exercised.
os.environ.setdefault("DANKAGU_JWT_SECRET", "test-suite-jwt-secret-not-for-production")

from dankagu.config import settings
from dankagu.core.database import Base, get_engine, init_db, reset_engine

# Ensure test suite always uses 'dankagu_test' instead of production 'dankagu'
if "DANKAGU_TEST_PG_URL" in os.environ:
    settings.db_url = os.environ["DANKAGU_TEST_PG_URL"]
else:
    if "/dankagu" in settings.db_url:
        settings.db_url = settings.db_url.rsplit("/dankagu", 1)[0] + "/dankagu_test"
    else:
        settings.db_url = "postgresql+asyncpg://postgres:postgres@localhost:5432/dankagu_test"

os.environ.setdefault("DANKAGU_TEST_PG_URL", settings.db_url)


#: The ORM schema is rebuilt once per pytest process (see ``_rebuild_schema``).
_SCHEMA_SYNCED = False


async def _rebuild_schema() -> None:
    """Drop and recreate every table once per run so the schema matches the models.

    ``init_db()`` only creates *missing* tables. A model change — a new column, or a
    changed ``ondelete`` clause — therefore left the old shape in place and the suite
    happily validated a schema production no longer had (an ``ON DELETE CASCADE``
    that had become ``SET NULL``, for example). The Alembic migrations remain the
    production source of truth and are covered by ``tests/test_db_migrations.py``,
    which runs on its own scratch database.
    """
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture(autouse=True)
async def initialize_test_database(request: pytest.FixtureRequest) -> AsyncGenerator[None, None]:
    """Ensure database tables exist before test, and clean up test data afterwards."""
    global _SCHEMA_SYNCED
    reset_engine()
    clean_url = settings.db_url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        try:
            conn = await asyncpg.connect(clean_url, timeout=2.0)
            await conn.close()
        except asyncpg.InvalidCatalogNameError:
            base_url = clean_url.rsplit("/", 1)[0] + "/postgres"
            sys_conn = await asyncpg.connect(base_url, timeout=2.0)
            await sys_conn.execute("CREATE DATABASE dankagu_test;")
            await sys_conn.close()
        if not _SCHEMA_SYNCED:
            await _rebuild_schema()
            _SCHEMA_SYNCED = True
        await init_db()
    except Exception as e:
        # If DB connection failed, skip tests that need database
        skip_keywords = ["persistence", "authorize", "token", "transfer", "storage"]
        if any(kw in request.node.nodeid.lower() for kw in skip_keywords):
            pytest.skip(f"PostgreSQL database not reachable at {settings.db_url}: {e!r}")

    existing_player_ids: set[str] = set()
    try:
        conn = await asyncpg.connect(clean_url, timeout=2.0)
        rows = await conn.fetch("SELECT id FROM players;")
        existing_player_ids = {r["id"] for r in rows}
        await conn.close()
    except Exception:
        pass

    yield

    # Clean up ONLY test players and related records created during this test
    try:
        conn = await asyncpg.connect(clean_url, timeout=2.0)
        rows = await conn.fetch("SELECT id FROM players;")
        new_ids = [r["id"] for r in rows if r["id"] not in existing_player_ids]
        if new_ids:
            await conn.execute("DELETE FROM account_transfers WHERE player_id = ANY($1);", new_ids)
            await conn.execute(
                "DELETE FROM player_storage_entries WHERE player_id = ANY($1);", new_ids
            )
            await conn.execute("DELETE FROM wallet_balances WHERE player_id = ANY($1);", new_ids)
            await conn.execute("DELETE FROM player_inventories WHERE player_id = ANY($1);", new_ids)
            await conn.execute("DELETE FROM song_leaderboards WHERE player_id = ANY($1);", new_ids)
            await conn.execute(
                "DELETE FROM friend_relations WHERE follower_id = ANY($1) OR target_id = ANY($1);",
                new_ids,
            )
            await conn.execute(
                "DELETE FROM my_space_likes WHERE from_player_id = ANY($1) OR to_player_id = ANY($1);",
                new_ids,
            )
            await conn.execute("DELETE FROM photo_likes WHERE player_id = ANY($1);", new_ids)
            await conn.execute("DELETE FROM player_photos WHERE player_id = ANY($1);", new_ids)
            await conn.execute("DELETE FROM sessions WHERE player_id = ANY($1);", new_ids)
            await conn.execute("DELETE FROM players WHERE id = ANY($1);", new_ids)
        await conn.close()
    except Exception:
        pass
