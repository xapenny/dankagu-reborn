"""Identity resolution shared by the LCX HTTP surface and the Takasho gRPC surface.

Both entry points have to agree on which player a request belongs to. Keeping the
decision here means they can no longer drift apart (previously the LCX path keyed
accounts on ``bundleId:deviceId`` while ``AuthorizeV3`` keyed them on a per-session
value, so one device could own two accounts).

Rules
-----
* A **verified** ``user_id`` (extracted from a JWT this server signed) may
  re-attach a request to an existing account.
* A ``device_account`` that identifies one device re-attaches an un-onboarded shell
  or a token-authenticated request to the account bound to it. However, a tokenless
  request (fresh install) never claims an already-onboarded account solely via
  ``device_account``, preventing Android devices that share a hardware identifier
  from landing inside an existing player's account.
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


def is_shared_device_account(value: str | None) -> bool:
    """Whether this identifier is one every install of the build reports."""
    if not value:
        return False
    text = str(value).strip().lower()
    configured = {
        item.strip().lower() for item in settings.shared_device_ids.split(",") if item.strip()
    }
    return text in KNOWN_SHARED_DEVICE_IDS or text in configured


def is_onboarded(player: Player) -> bool:
    """True once the player has completed the nickname / terms-of-service steps."""
    return bool(player.nickname or player.consented_tos_version)


async def find_player(
    session: AsyncSession,
    *,
    user_id: str | None,
    device_account: str | None,
    allow_onboarded_device_match: bool = True,
) -> tuple[Player | None, str]:
    """Return ``(player, matched_by)`` with ``matched_by`` in ``user_id|device_account|onboarded_collision|""``."""
    if user_id:
        res = await session.execute(select(Player).where(Player.user_id == user_id))
        player = res.scalars().first()
        if player is not None:
            return player, "user_id"

    if device_account:
        res = await session.execute(select(Player).where(Player.device_account == device_account))
        player = res.scalars().first()
        if player is not None:
            if not allow_onboarded_device_match and is_onboarded(player):
                logger.warning(
                    "🔒 [Auth] Refusing tokenless device match on already-onboarded player=%s "
                    "(device_account=%r); a fresh account will be registered instead",
                    player.id,
                    device_account,
                )
                return None, "onboarded_collision"
            return player, "device_account"

    return None, ""


async def create_player(
    session: AsyncSession,
    *,
    user_id: str | None,
    device_account: str | None,
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
    session: AsyncSession,
    *,
    user_id: str | None,
    device_account: str | None,
    allow_onboarded_device_match: bool = True,
) -> tuple[Player, str]:
    """Resolve the player for a request, registering one when the identity is new."""
    raw_device = str(device_account).strip() if device_account else None
    device_account = normalise_device_account(raw_device)

    player, matched_by = await find_player(
        session,
        user_id=user_id,
        device_account=device_account,
        allow_onboarded_device_match=allow_onboarded_device_match,
    )

    if player is not None and device_account and player.device_account:
        if player.device_account != device_account:
            # The account is bound to a different device, so a shared or copied token must not
            # carry it here. Resolve this device to its own bound account (even if onboarded)
            # or register and bind a fresh one for it.
            logger.warning(
                "🔒 [Auth] identity for %s is bound to another device (%r != %r); "
                "resolving this device separately",
                player.id,
                player.device_account,
                device_account,
            )
            user_id = None
            player, matched_by = await find_player(
                session,
                user_id=None,
                device_account=device_account,
                allow_onboarded_device_match=True,
            )

    if player is None:
        # If the device_account is already held by an onboarded player and this was a tokenless
        # fresh install, do not attempt to bind the colliding device_account to the new row.
        bind_device = None if matched_by == "onboarded_collision" else device_account
        player = await _create_with_retry(
            session,
            user_id=user_id,
            device_account=bind_device,
        )
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
    session: AsyncSession,
    *,
    user_id: str | None,
    device_account: str | None,
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
            player, _ = await find_player(
                session,
                user_id=user_id,
                device_account=device_account,
                allow_onboarded_device_match=True,
            )
            if player is not None:
                return player

    raise RuntimeError("could not register a player: identity kept colliding") from last_error
