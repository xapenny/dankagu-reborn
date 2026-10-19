"""Database infrastructure using SQLAlchemy 2.0 Async."""

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from dankagu.config import settings


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def reset_engine() -> None:
    """Reset the cached engine and sessionmaker (used when switching db URLs)."""
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.db_url,
            echo=False,
            future=True,
            pool_pre_ping=False,
            pool_recycle=settings.db_pool_recycle,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _sessionmaker


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for yielding an async database session."""
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all_tables() -> None:
    """Create any missing table straight from the ORM metadata.

    This is the *legacy* initialisation path: it can create a schema from
    nothing, but it cannot evolve an existing one. It is kept for the test suite;
    the server startup path uses the versioned migrations in
    ``dankagu.db.migrations`` (see ``src/dankagu/db/README.md``).
    """
    import dankagu.models  # noqa: F401 Ensure all ORM models are registered

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Not modelled by the ORM: AuthorizeV3 allocates 8-digit player short ids
        # from this sequence. The baseline migration creates it for databases that
        # are migrated instead of created.
        await conn.execute(text("CREATE SEQUENCE IF NOT EXISTS player_short_id_seq START WITH 1;"))


async def seed_runtime_data() -> None:
    """Populate master/gacha rows when their tables are empty (idempotent) and warm caches."""
    from dankagu.core.gacha_seed import seed_gacha_data_if_empty
    from dankagu.core.master_seed import seed_master_data_if_empty
    from dankagu.core.storage_state import StorageRevisionManager
    from dankagu.services.loot_box import ensure_gacha_cache_loaded

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        await seed_gacha_data_if_empty(session)
        await seed_master_data_if_empty(session)
        await ensure_gacha_cache_loaded(session)
    await StorageRevisionManager.initialize()


async def init_db() -> None:
    """Create the schema from the ORM models and seed runtime data.

    Kept for the test suite and one-off scripts. Server startup performs the same
    work in two explicit steps: ``run_migrations()`` (versioned schema) followed
    by :func:`seed_runtime_data`.
    """
    await create_all_tables()
    await seed_runtime_data()
