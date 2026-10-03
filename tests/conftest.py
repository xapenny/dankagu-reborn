"""Pytest configuration for the DanKagu test suite.

The environment is configured *before* ``dankagu.config`` is imported so that
tests never depend on a developer's local ``.env`` file, and never touch the
real player database.
"""

import os

import asyncpg
import pytest
import pytest_asyncio

# Placeholder JWT secret so token issuance can be exercised.
os.environ.setdefault("DANKAGU_JWT_SECRET", "test-suite-jwt-secret-not-for-production")

from dankagu.config import settings
from dankagu.core.database import init_db, reset_engine

if "DANKAGU_TEST_PG_URL" in os.environ:
    settings.db_url = os.environ["DANKAGU_TEST_PG_URL"]


@pytest_asyncio.fixture(autouse=True)
async def initialize_test_database(request: pytest.FixtureRequest) -> None:
    """Ensure database tables exist before running any test."""
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
