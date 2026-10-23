"""Insert a real player row so player-scoped writes satisfy their foreign keys.

``player_storage_entries``, ``player_photos``, ``photo_likes`` and ``my_space_likes``
now carry ``ON DELETE CASCADE`` foreign keys to ``players`` (migration 0005). Tests
that used to invent a player id -- ``"player-101"``, ``f"player-tenant-A-{uuid}"`` --
therefore have to create the row first, exactly as the application always did.

Add to a test module::

    from tests.player_factory import ensure_player
    tokens = set_current_player(await ensure_player(pid))
"""

from __future__ import annotations

import uuid

from dankagu.core.database import get_sessionmaker
from dankagu.models.player import Player


async def ensure_player(player_id: str | None = None, *, nickname: str = "") -> str:
    """Return ``player_id``, inserting the ``players`` row when it is missing."""
    pid = player_id or f"test-{uuid.uuid4()}"
    async with get_sessionmaker()() as session:
        existing = await session.get(Player, pid)
        if existing is None:
            session.add(
                Player(
                    id=pid,
                    user_id=f"user-{pid}",
                    registered_at=0,
                    nickname=nickname,
                )
            )
            await session.commit()
    return pid
