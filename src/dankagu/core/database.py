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
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
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


async def init_db() -> None:
    """Initialize database tables and run lightweight migrations."""
    import dankagu.models  # noqa: F401 Ensure all ORM models are registered

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Ensure sequence for short_id exists on PostgreSQL
        try:
            await conn.execute(text("CREATE SEQUENCE IF NOT EXISTS player_short_id_seq START WITH 1;"))
        except Exception:
            pass

        # Ensure new player columns exist if running on an existing DB instance
        for col_def in [
            "short_id VARCHAR(16)",
            "nickname VARCHAR(64) DEFAULT ''",
            "birth_year INTEGER DEFAULT 0",
            "birth_month INTEGER DEFAULT 0",
            "consented_tos_version VARCHAR(32) DEFAULT ''",
        ]:
            try:
                await conn.execute(text(f"ALTER TABLE players ADD COLUMN IF NOT EXISTS {col_def};"))
            except Exception:
                pass

        try:
            await conn.execute(
                text("ALTER TABLE master_announcements ADD COLUMN IF NOT EXISTS enabled BOOLEAN DEFAULT FALSE;")
            )
        except Exception:
            pass

    from dankagu.core.gacha_seed import seed_gacha_data_if_empty
    from dankagu.core.master_seed import seed_master_data_if_empty

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        await seed_gacha_data_if_empty(session)
        await seed_master_data_if_empty(session)
