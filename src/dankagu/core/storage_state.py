"""Shared state management for Takasho PlayerStorage revisions."""

import logging

from sqlalchemy import select, update

from dankagu.core.database import get_sessionmaker
from dankagu.models.player import Player

logger = logging.getLogger("dankagu.core.storage_state")


class StorageRevisionManager:
    """Tracks the current revision of PlayerStorage across all services."""

    _current_revision: str = "1"
    _initialized: bool = False

    @classmethod
    async def initialize(cls) -> None:
        """Load the latest storage_revision from the database."""
        if cls._initialized:
            return
        try:
            session_factory = get_sessionmaker()
            async with session_factory() as session:
                result = await session.execute(
                    select(Player.storage_revision).order_by(Player.last_login_at.desc()).limit(1)
                )
                rev = result.scalar_one_or_none()
                if rev:
                    cls._current_revision = str(rev)
                    logger.info(
                        "🔄 [StorageRevision] Initialized from DB: %s", cls._current_revision
                    )
            cls._initialized = True
        except Exception as e:
            logger.warning("Could not initialize storage revision from DB: %s", e)

    @classmethod
    def get_revision(cls) -> str:
        return cls._current_revision

    @classmethod
    def set_revision(cls, next_revision: str | None) -> str:
        if next_revision:
            old_rev = cls._current_revision
            cls._current_revision = str(next_revision)
            logger.info(
                "🔄 [StorageRevision] Revision advanced: %s -> %s", old_rev, cls._current_revision
            )
        return cls._current_revision

    @classmethod
    async def set_revision_and_save(
        cls, next_revision: str | None, player_id: str | None = None
    ) -> str:
        if not next_revision:
            return cls._current_revision
        cls.set_revision(next_revision)
        try:
            session_factory = get_sessionmaker()
            async with session_factory() as session:
                stmt = update(Player).values(storage_revision=str(next_revision))
                if player_id and player_id != "default-player":
                    stmt = stmt.where(Player.id == player_id)
                await session.execute(stmt)
                await session.commit()
        except Exception as e:
            logger.warning("Could not persist storage revision to DB: %s", e)
        return cls._current_revision
