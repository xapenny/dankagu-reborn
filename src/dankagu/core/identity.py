"""Identity resolution shared by the LCX HTTP surface and the Takasho gRPC surface.

Both entry points have to agree on which player a request belongs to. Keeping the
decision here means they can no longer drift apart (previously the LCX path keyed
accounts on ``bundleId:deviceId`` while ``AuthorizeV3`` keyed them on a per-session
value, so one device could own two accounts).

Rules
-----
* A **verified** ``user_id`` (extracted from a JWT this server signed) may
  re-attach a request to an existing account.
* A bare ``device_account`` match only re-attaches while the account has **not**
  been onboarded (nickname / consented ToS still empty), which covers
  re-installing on the same device. Once an account has been onboarded, matching
  it by device alone creates a fresh player instead — that is what guarantees the
  registration flow for a new device and stops a copied identifier from walking
  into somebody else's account.
* A concurrent insert of the same identity is recovered by re-resolving instead of
  bubbling up an ``IntegrityError`` (which used to surface as HTTP 500).
"""

from __future__ import annotations

import logging
import time
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.config import settings
from dankagu.core.starter import seed_starter_player
from dankagu.models.player import Player, allocate_short_id

logger = logging.getLogger("dankagu.core.identity")

#: Identifiers that cannot identify a device, because every install reports the same
#: value. ``dankagu-local-account-hash`` is the constant this project used to mint
#: itself. Deployments add their own through ``DANKAGU_SHARED_DEVICE_IDS``: an Android
#: build that cannot obtain a real device identity reports one shared value to every
#: install (a live example turned up where all Android phones and the emulator sent the
#: same identifier while iOS devices sent their own ``identifierForVendor``). Keying
#: accounts on such a value hands the first player's account -- already onboarded, so
#: the client asks for nothing -- to every later install.
KNOWN_SHARED_DEVICE_IDS = frozenset({"dankagu-local-account-hash"})


def normalise_device_account(value: str | None) -> str | None:
    """Return a usable device identifier, or ``None`` for empty/shared values."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    configured = {
        item.strip().lower() for item in settings.shared_device_ids.split(",") if item.strip()
    }
    if text.lower() in KNOWN_SHARED_DEVICE_IDS or text.lower() in configured:
        logger.warning("Ignoring shared device identifier %r", text)
        return None
    return text


def is_onboarded(player: Player) -> bool:
    """True once the player has completed the nickname / terms-of-service steps."""
    return bool(player.nickname or player.consented_tos_version)


async def find_player(
    session: AsyncSession, *, user_id: str | None, device_account: str | None
) -> tuple[Player | None, str]:
    """Return ``(player, matched_by)`` with ``matched_by`` in ``user_id|device_account|""``."""
    if user_id:
        res = await session.execute(select(Player).where(Player.user_id == user_id))
        player = res.scalars().first()
        if player is not None:
            return player, "user_id"

    if device_account:
        res = await session.execute(select(Player).where(Player.device_account == device_account))
        player = res.scalars().first()
        if player is not None:
            return player, "device_account"

    return None, ""


async def create_player(
    session: AsyncSession, *, user_id: str | None, device_account: str | None
) -> Player:
    """Insert and seed a brand new player."""
    now = int(time.time())
    player = Player(
        user_id=user_id or str(uuid.uuid4()),
        device_account=device_account,
        short_id=await allocate_short_id(session),
        registered_at=now,
        last_login_at=now,
        total_login_days=1,
    )
    session.add(player)
    await session.flush()
    await seed_starter_player(session, player)
    await session.commit()
    await session.refresh(player)
    logger.info("🆕 Registered player=%s (short_id=%s)", player.id, player.short_id)
    return player


async def resolve_or_create_player(
    session: AsyncSession, *, user_id: str | None, device_account: str | None
) -> tuple[Player, str]:
    """Resolve the player for a request, registering one when the identity is new."""
    device_account = normalise_device_account(device_account)
    player, matched_by = await find_player(session, user_id=user_id, device_account=device_account)

    if (
        player is not None
        and matched_by == "device_account"
        and is_onboarded(player)
        and not settings.reuse_device_account_when_onboarded
    ):
        logger.warning(
            "🔒 device_account %r already belongs to onboarded player %s; "
            "registering a separate account instead",
            device_account,
            player.id,
        )
        if device_account and player.device_account == device_account:
            # Release the identifier so the new account can take it over (the
            # column is unique). The old account stays reachable through the token
            # it already holds and through an account-transfer code; it simply no
            # longer answers to a bare device lookup.
            player.device_account = None
            await session.flush()
        player, matched_by = None, ""

    if player is None:
        player = await _create_with_retry(session, user_id=user_id, device_account=device_account)
        matched_by = "created"

    logger.info(
        "🔑 Resolved player=%s matched_by=%s user_id=%s device_account=%s",
        player.id,
        matched_by,
        user_id,
        device_account,
    )
    return player, matched_by


async def _create_with_retry(
    session: AsyncSession, *, user_id: str | None, device_account: str | None
) -> Player:
    """Create a player, recovering from a concurrent insert of the same identity."""
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            return await create_player(session, user_id=user_id, device_account=device_account)
        except IntegrityError as exc:
            last_error = exc
            await session.rollback()
            logger.warning(
                "Concurrent registration for the same identity (attempt %d): %s",
                attempt + 1,
                exc.orig,
            )
            player, _ = await find_player(session, user_id=user_id, device_account=device_account)
            if player is not None:
                return player

    raise RuntimeError("could not register a player: identity kept colliding") from last_error
