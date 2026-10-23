"""Session context management and token resolution for multi-tenant isolation."""

import contextvars
import logging
import time
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.core.database import get_sessionmaker
from dankagu.models.session import Session

logger = logging.getLogger("dankagu.core.session")

# Context variables tracking the active authenticated player in the current async task
_current_player_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_player_id", default=None
)
_current_session_token: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_session_token", default=None
)

# In-memory fast cache: session_token -> (player_id, user_id, expires_at)
_session_cache: dict[str, tuple[str, str, int]] = {}


def get_current_player_id() -> str | None:
    """Return the player_id bound to the current execution context, if any."""
    return _current_player_id.get()


def require_current_player_id() -> str:
    """Return current player_id, or raise RuntimeError if unauthenticated."""
    pid = _current_player_id.get()
    if not pid:
        raise RuntimeError("Operation requires an active authenticated session.")
    return pid


def get_current_session_token() -> str | None:
    """Return the session_token bound to the current execution context."""
    return _current_session_token.get()


def set_current_player(
    player_id: str | None, session_token: str | None = None
) -> tuple[contextvars.Token[str | None], contextvars.Token[str | None]]:
    """Set the current player_id and session_token context variables."""
    token_p = _current_player_id.set(player_id)
    token_s = _current_session_token.set(session_token)
    return token_p, token_s


def reset_current_player(
    tokens: tuple[contextvars.Token[str | None], contextvars.Token[str | None]],
) -> None:
    """Reset the context variables using the provided tokens."""
    _current_player_id.reset(tokens[0])
    _current_session_token.reset(tokens[1])


async def create_session(
    player_id: str,
    user_id: str,
    ttl_seconds: int = 86400 * 30,
    session_token: str | None = None,
    session: AsyncSession | None = None,
) -> str:
    """Create a new session token, save to DB, and cache it."""
    token = session_token or str(uuid.uuid4())
    now = int(time.time())
    expires_at = now + ttl_seconds

    # Cache locally
    _session_cache[token] = (player_id, user_id, expires_at)

    db_session = Session(
        session_token=token,
        player_id=player_id,
        user_id=user_id,
        created_at=now,
        expires_at=expires_at,
    )
    if session is not None:
        session.add(db_session)
    else:
        session_factory = get_sessionmaker()
        async with session_factory() as new_session:
            new_session.add(db_session)
            await new_session.commit()

    return token


async def resolve_session_player_id(session_token: str) -> str | None:
    """Resolve player_id from a session token via cache or database."""
    if not session_token:
        return None

    now = int(time.time())
    # 1. Check in-memory cache
    cached = _session_cache.get(session_token)
    if cached is not None:
        p_id, _, exp = cached
        if exp > now:
            return p_id
        # Expired in cache
        _session_cache.pop(session_token, None)

    # 2. Check database
    try:
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(Session).where(
                Session.session_token == session_token,
                Session.expires_at > now,
            )
            result = await session.execute(stmt)
            db_sess = result.scalar_one_or_none()
            if db_sess:
                _session_cache[session_token] = (
                    db_sess.player_id,
                    db_sess.user_id,
                    db_sess.expires_at,
                )
                return db_sess.player_id
    except Exception as exc:
        logger.error("Failed to resolve session token from DB: %s", exc)

    return None


async def invalidate_session(session_token: str) -> None:
    """Invalidate and remove a session token."""
    _session_cache.pop(session_token, None)
    try:
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            await session.execute(delete(Session).where(Session.session_token == session_token))
            await session.commit()
    except Exception as exc:
        logger.error("Failed to delete session from DB: %s", exc)


def invalidate_player_sessions(player_id: str) -> None:
    """Evict all cached in-memory session tokens for the given player."""
    stale = [tok for tok, (p_id, _, _) in _session_cache.items() if p_id == player_id]
    for tok in stale:
        _session_cache.pop(tok, None)


async def revoke_player_sessions(player_id: str) -> int:
    """Delete every stored session for a player and drop them from the cache.

    Used when the account changes hands (an account transfer) or is closed: the
    old device must lose access immediately, not when its token happens to expire.

    Returns:
        int: number of stored sessions that were revoked.
    """
    invalidate_player_sessions(player_id)
    revoked = 0
    try:
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            result = await session.execute(delete(Session).where(Session.player_id == player_id))
            await session.commit()
            revoked = int(result.rowcount or 0)
    except Exception as exc:
        logger.error("Failed to revoke sessions for player %s: %s", player_id, exc)
        raise
    if revoked:
        logger.info("🚪 Revoked %d session(s) for player %s", revoked, player_id)
    return revoked
