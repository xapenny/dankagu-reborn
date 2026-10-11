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
from dankagu.core.database import init_db, reset_engine

# Ensure test suite always uses 'dankagu_test' instead of production 'dankagu'
if "DANKAGU_TEST_PG_URL" in os.environ:
    settings.db_url = os.environ["DANKAGU_TEST_PG_URL"]
else:
    if "/dankagu" in settings.db_url:
        settings.db_url = settings.db_url.rsplit("/dankagu", 1)[0] + "/dankagu_test"
    else:
        settings.db_url = "postgresql+asyncpg://postgres:postgres@localhost:5432/dankagu_test"

os.environ.setdefault("DANKAGU_TEST_PG_URL", settings.db_url)


@pytest_asyncio.fixture(autouse=True)
async def initialize_test_database(request: pytest.FixtureRequest) -> AsyncGenerator[None, None]:
    """Ensure database tables exist before test, and clean up test data afterwards."""
    reset_engine()
    clean_url = settings.db_url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        conn = await asyncpg.connect(clean_url, timeout=2.0)
        await conn.close()
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
            await conn.execute("DELETE FROM sessions WHERE player_id = ANY($1);", new_ids)
            await conn.execute("DELETE FROM players WHERE id = ANY($1);", new_ids)
        await conn.close()
    except Exception:
        pass
