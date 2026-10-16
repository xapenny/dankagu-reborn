"""Identity resolution shared by the LCX HTTP surface and the Takasho gRPC surface.

Both entry points have to agree on which player a request belongs to. Keeping the
decision here means they can no longer drift apart (previously the LCX path keyed
accounts on ``bundleId:deviceId`` while ``AuthorizeV3`` keyed them on a per-session
value, so one device could own two accounts).

Rules
-----
* A **verified** ``user_id`` (extracted from a JWT this server signed) may
  re-attach a request to an existing account.
* A ``device_account`` that identifies one device re-attaches the account bound to
  it: the same device opening the game again is the same player, which is what
  players here expect.
* A ``device_account`` that **every install reports** cannot do that, so it is
  replaced by an *install identity* derived from it plus the per-install
  ``device_password`` the client sends with ``AuthorizeV3``. One install keeps one
  account across launches, separate installs stay separate, and a token minted for
  one install cannot be replayed on another.
* A concurrent insert of the same identity is recovered by re-resolving instead of
  bubbling up an ``IntegrityError`` (which used to surface as HTTP 500).
"""

from __future__ import annotations

import hashlib
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


def install_identity(device_account: str | None, device_password: str | None) -> str | None:
    """Identify one *install* of a build whose device identifier is shared.

    ``device_password`` is generated per install by the client and stays with it, so the pair
    is stable for that install and different for every other one. Returns ``None`` when the
    identifier is usable on its own or the password is missing.
    """
    if not device_account or not device_password:
        return None
    if not is_shared_device_account(device_account):
        return None
    digest = hashlib.sha256(f"{device_account}\x00{device_password}".encode()).hexdigest()
    return digest[:32]


def is_onboarded(player: Player) -> bool:
    """True once the player has completed the nickname / terms-of-service steps."""
    return bool(player.nickname or player.consented_tos_version)


async def find_player(
    session: AsyncSession,
    *,
    user_id: str | None,
    device_account: str | None,
    install_id: str | None = None,
) -> tuple[Player | None, str]:
    """Return ``(player, matched_by)`` with ``matched_by`` in ``user_id|device_account|""``."""
    if user_id:
        res = await session.execute(select(Player).where(Player.user_id == user_id))
        player = res.scalars().first()
        if player is not None:
            return player, "user_id"

    if install_id:
        res = await session.execute(select(Player).where(Player.install_id == install_id))
        player = res.scalars().first()
        if player is not None:
            return player, "install_id"

    if device_account:
        res = await session.execute(select(Player).where(Player.device_account == device_account))
        player = res.scalars().first()
        if player is not None:
            return player, "device_account"

    return None, ""


async def create_player(
    session: AsyncSession,
    *,
    user_id: str | None,
    device_account: str | None,
    install_id: str | None = None,
) -> Player:
    """Insert and seed a brand new player."""
    now = int(time.time())
    player = Player(
        user_id=user_id or str(uuid.uuid4()),
        device_account=device_account,
        install_id=install_id,
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
    device_password: str | None = None,
) -> tuple[Player, str]:
    """Resolve the player for a request, registering one when the identity is new.

    ``device_password`` separates two installs of a build that reports one shared device
    identifier (see :func:`install_identity`); for a unique identifier it is not needed.
    """
    raw_device = str(device_account).strip() if device_account else None
    install_id = install_identity(raw_device, device_password)
    device_account = normalise_device_account(raw_device)

    player, matched_by = await find_player(
        session, user_id=user_id, device_account=device_account, install_id=install_id
    )

    if player is not None and install_id:
        if player.install_id is None:
            # Signed in without a password before, or created before this column existed:
            # claim the install so every later launch lands on this account.
            player.install_id = install_id
            await session.flush()
            logger.info("🔗 [Auth] claimed install for player %s", player.id)
        elif player.install_id != install_id:
            # The identity presented here belongs to another install of the same build -- a
            # copied or shared token. The account is not this install's to use, so the claim
            # is dropped and this install resolves on its own (its own account, or a new one).
            logger.warning(
                "🔒 [Auth] identity for %s belongs to another install (%s… != %s…); "
                "resolving this install separately",
                player.id,
                player.install_id[:8],
                install_id[:8],
            )
            user_id = None
            player, matched_by = await find_player(
                session, user_id=None, device_account=device_account, install_id=install_id
            )

    if player is not None and device_account and player.device_account:
        if player.device_account != device_account:
            # Same story for a device identifier that names one device: the account is bound
            # elsewhere, so a shared or copied token must not carry it here.
            logger.warning(
                "🔒 [Auth] identity for %s is bound to another device (%r != %r); "
                "resolving this device separately",
                player.id,
                player.device_account,
                device_account,
            )
            user_id = None
            player, matched_by = await find_player(
                session, user_id=None, device_account=device_account, install_id=install_id
            )

    if player is None:
        player = await _create_with_retry(
            session,
            user_id=user_id,
            device_account=device_account,
            install_id=install_id,
        )
        matched_by = "created"

    logger.info(
        "🔑 Resolved player=%s matched_by=%s user_id=%s device_account=%s install_id=%s",
        player.id,
        matched_by,
        user_id,
        device_account,
        f"{install_id[:8]}…" if install_id else None,
    )
    return player, matched_by


async def _create_with_retry(
    session: AsyncSession,
    *,
    user_id: str | None,
    device_account: str | None,
    install_id: str | None = None,
) -> Player:
    """Create a player, recovering from a concurrent insert of the same identity."""
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            return await create_player(
                session, user_id=user_id, device_account=device_account, install_id=install_id
            )
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
                install_id=install_id,
            )
            if player is not None:
                return player

    raise RuntimeError("could not register a player: identity kept colliding") from last_error
