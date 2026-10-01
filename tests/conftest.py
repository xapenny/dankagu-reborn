"""Pytest configuration for the DanKagu test suite.

The environment is configured *before* ``dankagu.config`` is imported so that
tests never depend on a developer's local ``.env`` file, and never touch the
real player database.
"""

import os

# Force (not setdefault) so tests are unaffected by a developer's .env file:
# without this, DANKAGU_DB_URL from .env would point the suite at the real
# player database.
#
# Placeholder JWT secret so token issuance can be exercised. This value is a
# test fixture, not a credential: it is never used outside the test suite.
os.environ["DANKAGU_JWT_SECRET"] = "test-suite-jwt-secret-not-for-production"

# Run against an in-memory database instead of data/dankagu.db so tests are
# isolated and repeatable. SQLite's shared cache keeps the schema visible across
# the multiple connections the async session factory opens.
os.environ["DANKAGU_DB_URL"] = (
    "sqlite+aiosqlite:///file:dankagu_test?mode=memory&cache=shared&uri=true"
)

import pytest_asyncio  # noqa: E402

from dankagu.core.database import init_db  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def initialize_test_database() -> None:
    """Ensure database tables exist before running any test."""
    await init_db()
