"""Shared state management for Takasho PlayerStorage revisions."""

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.core.database import get_sessionmaker
from dankagu.models.player import Player

logger = logging.getLogger("dankagu.core.storage_state")


class StorageRevisionManager:
    """Tracks the current revision of PlayerStorage per player and globally."""

    _current_revision: str = "1"
    _player_revisions: dict[str, str] = {}
    _initialized: bool = False

    @classmethod
    async def initialize(cls) -> None:
        """Load storage revisions from the database."""
        if cls._initialized:
            return
        try:
            session_factory = get_sessionmaker()
            async with session_factory() as session:
                stmt = select(Player.id, Player.storage_revision)
                res = await session.execute(stmt)
                for pid, rev in res.all():
                    if rev:
                        cls._player_revisions[pid] = str(rev)
                result = await session.execute(
                    select(Player.storage_revision).order_by(Player.last_login_at.desc()).limit(1)
                )
                rev = result.scalar_one_or_none()
                if rev:
                    cls._current_revision = str(rev)
                logger.info(
                    "🔄 [StorageRevision] Initialized %d player revisions from DB (global: %s)",
                    len(cls._player_revisions),
                    cls._current_revision,
                )
            cls._initialized = True
        except Exception as e:
            logger.warning("Could not initialize storage revision from DB: %s", e)

    @classmethod
    def get_revision(cls, player_id: str | None = None) -> str:
        """Get the storage revision for a player, falling back to initial revision '1'."""
        if player_id:
            return cls._player_revisions.get(player_id, "1")
        return cls._current_revision

    @classmethod
    def set_revision(cls, next_revision: str | None, player_id: str | None = None) -> str:
        if next_revision:
            old_rev = cls.get_revision(player_id)
            new_rev = str(next_revision)
            if player_id:
                cls._player_revisions[player_id] = new_rev
            cls._current_revision = new_rev
            logger.info(
                "🔄 [StorageRevision] Revision advanced (player=%s): %s -> %s",
                player_id,
                old_rev,
                new_rev,
            )
        return cls.get_revision(player_id)

    @classmethod
    async def set_revision_and_save(
        cls,
        next_revision: str | None,
        player_id: str | None = None,
        session: AsyncSession | None = None,
    ) -> str:
        if not next_revision:
            return cls.get_revision(player_id)
        cls.set_revision(next_revision, player_id=player_id)
        if player_id:
            stmt = (
                update(Player)
                .where(Player.id == player_id)
                .values(storage_revision=str(next_revision))
            )
            if session is not None:
                await session.execute(stmt)
            else:
                try:
                    session_factory = get_sessionmaker()
                    async with session_factory() as new_session:
                        await new_session.execute(stmt)
                        await new_session.commit()
                except Exception as e:
                    logger.warning("Could not persist storage revision to DB: %s", e)
        return cls.get_revision(player_id)
