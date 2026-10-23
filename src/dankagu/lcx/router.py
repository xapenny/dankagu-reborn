"""FastAPI router implementing LCX Authentication & Identity endpoints."""

import base64
import gzip
import hashlib
import json
import logging
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.config import settings
from dankagu.core.database import get_db_session
from dankagu.core.identity import (
    find_player,
    normalise_device_account,
    resolve_or_create_player,
)
from dankagu.core.session import (
    invalidate_player_sessions,
    invalidate_session,
    revoke_player_sessions,
)
from dankagu.core.transfer import (
    generate_transfer_id,
    generate_transfer_password,
    hash_transfer_password,
    verify_transfer_password,
)
from dankagu.lcx.errors import LcxApiError
from dankagu.lcx.tokens import (
    decode_token_payload,
    decode_token_user_id,
    generate_lcx_tokens,
    token_device_hash,
    user_id_from_payload,
)
from dankagu.models.inventory import PlayerInventoryItem
from dankagu.models.player import Player
from dankagu.models.receipt import PurchaseReceipt
from dankagu.models.session import Session
from dankagu.models.storage import PlayerStorageEntry
from dankagu.models.transfer import AccountTransfer
from dankagu.models.wallet import WalletBalance

logger = logging.getLogger("dankagu.lcx.router")

router = APIRouter(tags=["LCX"])

#: Sentinel the rest of the codebase uses for "no expiry" on integer timestamps.
FAR_FUTURE = 4102498799

#: Failed transfer attempts, per transfer id, as a list of epoch seconds. A long-lived
#: code is worth guessing at, so guesses are cheap to count and expensive to repeat.
_transfer_attempts: dict[str, list[int]] = {}


def _throttle_check(transfer_id: str, now: int) -> bool:
    """Record an attempt and say whether it is still within the hourly allowance."""
    hour_ago = now - 3600
    attempts = [stamp for stamp in _transfer_attempts.get(transfer_id, []) if stamp > hour_ago]
    attempts.append(now)
    _transfer_attempts[transfer_id] = attempts
    return len(attempts) <= settings.transfer_attempts_per_hour


class CustomerServiceResponse(BaseModel):
    url: str = ""
    accountCancellationUrl: str = ""


class PrivacyAgreementResponse(BaseModel):
    privacyAgreementId: str = ""
    userAgreementId: str = ""
    privacyAgreementUrl: str = ""
    staticHtmlVersion: str = ""


class EnvironmentResponse(BaseModel):
    """Mirrors the client's EnvironmentResponseEntity.

    The client deserialises all five members (see
    `LCX.Internal.Entity.EnvironmentResponseEntity` and
    `LCX.Internal.Translator.EnvironmentTranslator.Translate`), so every field
    must be present even when empty.
    """

    applicationId: str = "danmakujp4-v1"
    isProd: bool = True
    ads: str = ""
    cs: CustomerServiceResponse = CustomerServiceResponse()
    agreement: PrivacyAgreementResponse = PrivacyAgreementResponse()


@router.get("/environment", response_model=EnvironmentResponse)
@router.post("/environment", response_model=EnvironmentResponse)
async def get_or_post_environment() -> EnvironmentResponse:
    """Check app environment status."""
    return EnvironmentResponse()


@router.post("/auth/user-access-token")
async def post_user_access_token(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    """Refresh or issue new user access token."""
    player = await _resolve_or_create_player(db, request)
    tokens = generate_lcx_tokens(
        player.user_id, player.registered_at, device_hash=_request_device(request)
    )
    return {
        "userId": player.user_id,
        "accessToken": tokens["accessToken"],
        "signInSessionId": tokens["signInSessionId"],
        "analyticsSessionId": tokens["analyticsSessionId"],
    }


async def _resolve_or_create_player(db: AsyncSession, request: Request) -> Player:
    """Retrieve player matching client credentials or register a distinct new player.

    Identity rules live in :mod:`dankagu.core.identity`. This wrapper only pulls
    credentials out of the HTTP request and enforces the device binding that the
    presented token recorded when it was issued.
    """
    user_id: str | None = None
    device_account: str | None = None
    presented_token: str | None = None

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        presented_token = auth_header[7:].strip()

    device_account = request.headers.get("deviceAccount") or request.headers.get("x-device-account")

    try:
        body = await request.json()
    except Exception:
        body = None

    if isinstance(body, dict):
        if not presented_token:
            for key in ("signInSessionToken", "sessionToken"):
                if body.get(key):
                    presented_token = str(body[key])
                    break
        if not device_account:
            # NOTE: ``storeAccountIdHash`` and ``verificationToken`` are deliberately
            # *not* accepted as a ``device_account``. ``storeAccountIdHash`` is minted
            # by this server, and ``verificationToken`` decodes to ``bundleId:sha1``
            # which is identical across Android devices and differs in format from the
            # 32-character ``device_account`` sent to Takasho ``System.AuthorizeV3``.
            device_account = body.get("deviceAccount")

    request_device = normalise_device_account(device_account)
    payload = decode_token_payload(presented_token) if presented_token else None
    original_user_id: str | None = None
    token_redirect_key: str | None = None

    if payload is not None:
        user_id = user_id_from_payload(payload)
        original_user_id = user_id
        bound_device = token_device_hash(payload)
        expires_at = payload.get("exp")
        expired_claim = isinstance(expires_at, int | float) and float(expires_at) < time.time()

        if bound_device and request_device and bound_device != request_device:
            logger.warning(
                "🔒 [Auth] Rejected token issued to device %r presented from %r",
                bound_device,
                request_device,
            )
            raise LcxApiError(
                status.HTTP_401_UNAUTHORIZED,
                "LCX_INVALID_SESSION_DEVICE",
                "Session token was issued to another device",
            )

        # If this token previously named a deleted or device-conflicting account and was
        # assigned a replacement player on /auth/user-access-token (which cannot overwrite
        # the client's stored signInSessionToken), resolve directly to that replacement
        # player across subsequent game restarts.
        if presented_token:
            redir_scope = (
                f"{request_device}:{presented_token}" if request_device else presented_token
            )
            token_redirect_key = (
                "lcx_redir:" + hashlib.sha256(redir_scope.encode("utf-8")).hexdigest()[:32]
            )
            now_ts = int(time.time())
            redir_sess = (
                await db.execute(
                    select(Session).where(
                        Session.session_token == token_redirect_key,
                        Session.expires_at > now_ts,
                    )
                )
            ).scalar_one_or_none()
            if redir_sess is not None:
                redir_player = await db.get(Player, redir_sess.player_id)
                if redir_player is not None:
                    request.state.device_account = request_device
                    return redir_player

        # An unbound claim proves nothing about the account it names, so it is checked
        # against the database: the account must belong to this device already, or have
        # no device bound yet. (When bound_device is set and matches request_device,
        # device ownership is already verified even if the token is expired.)
        if request_device and settings.require_device_bound_tokens and not bound_device:
            owner, _matched = await find_player(db, user_id=user_id, device_account=None)
            owns = owner is not None and (
                not owner.device_account or owner.device_account == request_device
            )
            owns_what = owner.device_account if owner is not None else None

            if not owns:
                # Not this device's account: register a new player instead of refusing, or
                # the client re-presents the same token forever. The user id is dropped, so
                # the account it named can never be reached from here.
                state = "expired" if expired_claim else "pre-dates device binding"
                logger.warning(
                    "🆕 [Auth] %s token for device %r names account %s, which is %s; "
                    "registering a new player for this device instead",
                    state,
                    request_device,
                    user_id,
                    "unknown" if owner is None else f"bound to {owns_what!r}",
                )
                user_id = None
            else:
                # This device owns the account: accept it and claim the binding if still free.
                if not owner.device_account:
                    owner.device_account = request_device
                    await db.commit()
                logger.info(
                    "🔗 [Auth] Accepted %s token for player %s on %r",
                    "expired" if expired_claim else "pre-binding",
                    owner.id,
                    request_device,
                )
        elif expired_claim and not bound_device:
            # Nothing to verify the claim against; refusing here would lock out any
            # client that does not send device information at all.
            logger.warning(
                "⚠️  [Auth] Accepting an expired token without a device to verify it "
                "against (player %s)",
                user_id,
            )
    elif presented_token:
        logger.warning("🔒 [Auth] Ignoring unverifiable token presented by the client")

    if user_id:
        # The named account is gone (replaced by a transfer, or deleted): same rule, a new
        # player for this device.
        known, _matched = await find_player(db, user_id=user_id, device_account=None)
        if known is None:
            logger.warning(
                "🆕 [Auth] Token names unknown user_id=%s from device %r; "
                "registering a new player instead",
                user_id,
                request_device,
            )
            user_id = None

    player, _matched_by = await resolve_or_create_player(
        db,
        user_id=user_id,
        device_account=device_account,
        allow_onboarded_device_match=(payload is not None),
    )
    if token_redirect_key and original_user_id and player.user_id != original_user_id:
        now_ts = int(time.time())
        existing_redir = (
            await db.execute(select(Session).where(Session.session_token == token_redirect_key))
        ).scalar_one_or_none()
        if existing_redir is None:
            db.add(
                Session(
                    session_token=token_redirect_key,
                    player_id=player.id,
                    user_id=player.user_id,
                    created_at=now_ts,
                    expires_at=FAR_FUTURE,
                )
            )
        else:
            existing_redir.player_id = player.id
            existing_redir.user_id = player.user_id
            existing_redir.expires_at = FAR_FUTURE
        await db.commit()

    request.state.device_account = request_device
    return player


def _build_verified_token_response(
    player: Player, device_hash: str | None = None
) -> dict[str, Any]:
    """Build LCX VerifiedTokenEntity response payload."""
    tokens = generate_lcx_tokens(
        player.user_id, player.registered_at, device_hash=normalise_device_account(device_hash)
    )
    return {
        "accessToken": tokens["accessToken"],
        "lcxUserIdToken": tokens["lcxUserIdToken"],
        "user": {
            "id": player.user_id,
            "registeredAt": player.registered_at,
            "store": "GUEST",
        },
        "signInSessionToken": tokens["signInSessionToken"],
        "signInSessionId": tokens["signInSessionId"],
        "govPolicy": {"enablePopup": False, "popupSkippable": True},
        "analyticsSessionId": tokens["analyticsSessionId"],
    }


def _request_device(request: Request) -> str | None:
    """Device the current request was resolved for (set by _resolve_or_create_player)."""
    return getattr(request.state, "device_account", None)


@router.post("/identity/sign-in")
@router.post("/identity/force-sign-in")
# The SDK calls the force-sign-in path under /gcp/ (see the client's own string
# table); serving only the /identity/ one left the client's recovery call at 404.
@router.post("/gcp/identity/force-sign-in")
@router.post("/identity/pre-sign-in")
@router.post("/identity/mock-sign-in")
async def post_identity_session(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Any:
    """Execute initial sign-in or session establishment."""
    player = await _resolve_or_create_player(db, request)
    return _build_verified_token_response(player, _request_device(request))


@router.api_route("/identity/active-published-store", methods=["GET", "POST"])
@router.api_route("/gcp/identity/active-published-store", methods=["GET", "POST"])
async def get_active_published_store() -> dict[str, str]:
    """Return active store type (GUEST)."""
    return {"store": "GUEST"}


@router.post("/identity/transfer/suggest")
async def post_transfer_suggest(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, str]:
    """Suggest account transfer credentials."""
    await _resolve_or_create_player(db, request)
    suggested_id = generate_transfer_id()
    suggested_pw = generate_transfer_password()
    return {"id": suggested_id, "password": suggested_pw}


@router.post("/identity/transfer")
async def post_transfer_register(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Any:
    """Register account transfer credentials for the authenticated player."""
    now = int(time.time())
    body: dict[str, Any] = {}
    try:
        raw_body = await request.json()
        if isinstance(raw_body, dict):
            body = raw_body
    except Exception:
        pass

    transfer_id = str(body.get("id", "")).strip().upper()
    password = str(body.get("password", "")).strip()

    if not transfer_id or not password:
        return JSONResponse(
            status_code=400,
            content={
                "code": "LCX_INVALID_TRANSFER_ID_OR_PASSWORD",
                "message": "Transfer ID and password required",
            },
        )

    if len(password) < settings.transfer_min_password_length:
        # The ID is eight digits; the password is the whole lock, so refuse a short one
        # instead of minting a credential that a script can brute force.
        return JSONResponse(
            status_code=400,
            content={
                "code": "LCX_TRANSFER_PASSWORD_TOO_SHORT",
                "message": (
                    f"Password must be at least {settings.transfer_min_password_length} characters"
                ),
            },
        )

    player = await _resolve_or_create_player(db, request)

    # Check if this transfer ID is already claimed by another player
    stmt = select(AccountTransfer).where(
        AccountTransfer.transfer_id == transfer_id,
        AccountTransfer.player_id != player.id,
    )
    res = await db.execute(stmt)
    existing = res.scalars().first()
    if existing is not None:
        return JSONResponse(
            status_code=400,
            content={
                "code": "LCX_TRANSFER_ID_ALREADY_EXISTS",
                "message": "Transfer ID already exists",
            },
        )

    # Re-registering or updating the player's transfer code replaces existing codes
    await db.execute(
        delete(AccountTransfer).where(
            (AccountTransfer.player_id == player.id) | (AccountTransfer.transfer_id == transfer_id)
        )
    )

    # Codes are permanently valid (FAR_FUTURE) and do not expire after use
    expires_at = FAR_FUTURE
    transfer = AccountTransfer(
        transfer_id=transfer_id,
        player_id=player.id,
        password_hash=hash_transfer_password(password),
        is_used=False,
        created_at=now,
        expires_at=expires_at,
    )
    db.add(transfer)
    await db.commit()

    return {"id": transfer_id, "expiresAt": expires_at}


@router.post("/identity/transfer/execute")
@router.post("/identity/transfer/{account_id}/execute")
async def post_transfer_execute(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    account_id: str | None = None,
) -> Any:
    """Execute account transfer using Transfer ID and password on new device."""
    now = int(time.time())
    body: dict[str, Any] = {}
    try:
        raw_body = await request.json()
        if isinstance(raw_body, dict):
            body = raw_body
    except Exception:
        pass

    transfer_id = str(account_id or body.get("id", "")).strip().upper()
    password = str(body.get("password", "")).strip()

    if not password:
        # Fallback for passwordless test calls / legacy mock invocations
        player = await _resolve_or_create_player(db, request)
        return _build_verified_token_response(player, _request_device(request))

    if not transfer_id:
        return JSONResponse(
            status_code=400,
            content={
                "code": "LCX_INVALID_TRANSFER_ID_OR_PASSWORD",
                "message": "Transfer ID and password required",
            },
        )

    stmt = select(AccountTransfer).where(AccountTransfer.transfer_id == transfer_id)
    res = await db.execute(stmt)
    transfer = res.scalars().first()

    # Throttle before looking anything up: a long-lived code must not be guessable at
    # script speed, and counting the attempt costs nothing.
    if not _throttle_check(transfer_id, now):
        logger.warning(
            "🎫 [Transfer] Throttled after %d attempts for id %r",
            settings.transfer_attempts_per_hour,
            transfer_id,
        )
        return JSONResponse(
            status_code=429,
            content={
                "code": "LCX_TOO_MANY_ATTEMPTS",
                "message": "Too many attempts; try again later",
            },
        )

    # Diagnostics matter here: the client shows one generic string for every
    # failure, so the server has to say *which* one happened or an operator is
    # left guessing (see docs/AUTH_IDENTITY_FIX_PLAN.md, transfer section).
    if transfer is None:
        logger.warning("🎫 [Transfer] Unknown transfer id %r", transfer_id)
        return JSONResponse(
            status_code=400,
            content={
                "code": "LCX_TRANSFER_ID_NOT_FOUND",
                "message": "Transfer ID not found",
            },
        )

    if not verify_transfer_password(password, transfer.password_hash):
        # Keep the client's familiar string for the one failure a player can fix.
        logger.warning("🎫 [Transfer] Wrong password for transfer id %r", transfer_id)
        return JSONResponse(
            status_code=400,
            content={
                "code": "LCX_INVALID_TRANSFER_ID_OR_PASSWORD",
                "message": "Invalid transfer ID or password",
            },
        )

    if transfer.player_id is None:
        # The account was deleted after the code was issued. The row survives (FK is
        # ON DELETE SET NULL, and deactivation detaches it) so we can say so instead
        # of pretending the player mistyped a correct code.
        logger.warning("🎫 [Transfer] Transfer id %r points at a deleted account", transfer_id)
        return JSONResponse(
            status_code=404,
            content={
                "code": "LCX_TRANSFER_SOURCE_ACCOUNT_DELETED",
                "message": "The account this transfer ID belongs to no longer exists",
            },
        )

    target_player = await db.get(Player, transfer.player_id)
    if target_player is None:
        logger.warning(
            "🎫 [Transfer] Transfer id %r references missing player %s",
            transfer_id,
            transfer.player_id,
        )
        return JSONResponse(
            status_code=404,
            content={
                "code": "LCX_TRANSFER_TARGET_USER_NOT_FOUND",
                "message": "Target player not found",
            },
        )

    # Transfer code remains permanently valid; do not mark as used or expire.
    transfer.is_used = False

    device_account = normalise_device_account(
        request.headers.get("deviceAccount")
        or request.headers.get("x-device-account")
        or body.get("deviceAccount")
    )
    if device_account:
        # The device belongs to the account being moved, so the account living on it is
        # replaced -- the same deletion the client's own deactivate flow performs.
        resident, _matched = await find_player(db, user_id=None, device_account=device_account)
        if resident is not None and resident.id != target_player.id:
            logger.warning(
                "🎫 [Transfer] id=%s replaces the account on device %r: "
                "deleting player=%s (user_id=%s)",
                transfer_id,
                device_account,
                resident.id,
                resident.user_id,
            )
            await _delete_player_account(db, resident)
            await db.flush()

    # The account is moving to another device: bind it to the explicit target device when
    # provided, or clear the old binding so System.AuthorizeV3 can bind the new device's
    # Takasho device_account on the immediately following login call.
    old_device = target_player.device_account
    target_player.device_account = device_account
    if old_device and old_device != device_account:
        logger.info(
            "🎫 [Transfer] id=%s updated device binding of player=%s (%r -> %r)",
            transfer_id,
            target_player.id,
            old_device,
            device_account,
        )

    try:
        await db.commit()
    except IntegrityError:
        # Roll back so the code stays unused, and answer in the envelope the client reads.
        await db.rollback()
        logger.exception(
            "🎫 [Transfer] id=%s could not move player=%s to device=%r",
            transfer_id,
            transfer.player_id,
            device_account,
        )
        return JSONResponse(
            status_code=400,
            content={
                "code": "LCX_TRANSFER_FAILED",
                "message": "The account could not be moved to this device; try again",
            },
        )

    # The account now lives on the new device: every session the old device held
    # must stop working, otherwise the transfer leaves two live devices behind.
    revoked = await revoke_player_sessions(target_player.id)
    logger.info(
        "🎫 [Transfer] id=%s moved player=%s to device=%r, revoked %d session(s)",
        transfer_id,
        target_player.id,
        device_account,
        revoked,
    )

    tokens = generate_lcx_tokens(
        target_player.user_id, target_player.registered_at, device_hash=device_account
    )
    return {
        "accessToken": tokens["accessToken"],
        "signInSessionToken": tokens["signInSessionToken"],
        "signInSessionId": tokens["signInSessionId"],
        "lcxUserIdToken": tokens["lcxUserIdToken"],
        "user": {
            "id": target_player.user_id,
            "registeredAt": target_player.registered_at,
            "store": "GUEST",
        },
        "govPolicy": {"enablePopup": False, "popupSkippable": True},
        "analyticsSessionId": tokens["analyticsSessionId"],
    }


@router.get("/gcp/analytics/config")
async def get_analytics_config() -> dict[str, Any]:
    """Mock GCP analytics PubSub configuration."""
    return {
        "isPubsubEnabled": False,
        "topicPath": "",
        "pubsubAccessToken": "",
    }


@router.api_route("/identity/user-id-token/{user_id}", methods=["GET", "POST"])
async def get_user_id_token(user_id: str) -> dict[str, Any]:
    """Get LCX User ID token."""
    tokens = generate_lcx_tokens(user_id, int(time.time()))
    return {"lcxUserIdToken": tokens["lcxUserIdToken"]}


@router.api_route("/identity/link-state", methods=["GET", "POST"])
@router.api_route("/identity/link-state/{user_id}", methods=["GET", "POST"])
async def get_or_post_link_state(user_id: str | None = None) -> dict[str, bool]:
    """Return linked account status."""
    return {"hasAnyLink": False}


@router.api_route("/identity/link", methods=["GET", "POST"])
@router.api_route("/identity/link/{user_id}", methods=["GET", "POST"])
async def post_link(user_id: str | None = None) -> dict[str, bool]:
    """Link account status."""
    return {"hasAnyLink": True}


@router.api_route("/identity/mock-link", methods=["GET", "POST"])
@router.api_route("/identity/mock-link/{user_id}", methods=["GET", "POST"])
async def post_mock_link(user_id: str | None = None) -> dict[str, bool]:
    """Mock link account status."""
    return {"hasAnyLink": True}


@router.post("/identity/sign-out", status_code=status.HTTP_204_NO_CONTENT)
async def post_sign_out(request: Request) -> Response:
    """Sign out: revoke the presented session token so it cannot be reused."""
    token: str | None = None
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()

    try:
        body = await request.json()
    except Exception:
        body = None
    if isinstance(body, dict) and not token:
        for key in ("signInSessionToken", "sessionToken", "accessToken"):
            if body.get(key):
                token = str(body[key])
                break

    if token:
        await invalidate_session(token)
        logger.info("🚪 [Auth] Signed out and revoked session token")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _delete_player_account(db: AsyncSession, player: Player) -> None:
    """Delete an account and everything it owns. Commits nothing; the caller decides."""
    pid = player.id
    # Explicit deletes, because ``player_storage_entries`` carries no foreign key.
    await db.execute(delete(PlayerStorageEntry).where(PlayerStorageEntry.player_id == pid))
    await db.execute(delete(Session).where(Session.player_id == pid))
    # Codes are revoked, not deleted, so an issued one cannot vanish behind "wrong code".
    await db.execute(
        update(AccountTransfer)
        .where(AccountTransfer.player_id == pid)
        .values(player_id=None, is_used=True)
    )
    await db.execute(delete(WalletBalance).where(WalletBalance.player_id == pid))
    await db.execute(delete(PlayerInventoryItem).where(PlayerInventoryItem.player_id == pid))
    await db.execute(delete(Player).where(Player.id == pid))
    invalidate_player_sessions(pid)
    logger.warning("🗑️ Deleted account player_id=%s user_id=%s", pid, player.user_id)


@router.post("/identity/deactivate-user")
async def post_deactivate_user(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Any:
    """Permanently delete the calling player's account and all owned data.

    The client body only carries ``gameServerNotificationUrlFilters``; the
    identity comes from the bearer access token. Unlike sign-in, this must
    never fall back to creating or picking a player.
    """
    user_id: str | None = None
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        user_id = decode_token_user_id(auth_header[7:].strip())

    player: Player | None = None
    if user_id:
        res = await db.execute(select(Player).where(Player.user_id == user_id))
        player = res.scalars().first()

    if player is None:
        logger.warning(
            "deactivate-user rejected: no resolvable player (user_id=%r, headers=%s)",
            user_id,
            sorted(request.headers.keys()),
        )
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"code": "LCX_UNAUTHORIZED", "message": "Unknown or missing user"},
        )

    await _delete_player_account(db, player)
    await db.commit()

    return {"success": True}


@router.api_route("/push/user-token/{user_id}", methods=["GET", "POST"])
@router.api_route("/push/user-token", methods=["GET", "POST"])
async def post_push_user_token(user_id: str | None = None) -> dict[str, Any]:
    """Mock APNS push token registration."""
    return {}


@router.api_route("/subs", methods=["GET", "POST"])
@router.api_route("/subs/{path:path}", methods=["GET", "POST"])
async def get_subscriptions(path: str = "") -> list[Any]:
    """Mock subscription list."""
    return []


# --------------------------------------------------------------------------------------- IAP
#
# The store the client verifies against no longer exists, so every product is free: the
# catalogue comes from the master's ``ShopProduct_*`` rows, a purchase verifies without a real
# receipt, and orders are recorded so a retry answers identically instead of granting twice.

#: Product payload prefixes that name an in-game currency, for the ``exchanged`` description.
_CURRENCY_PREFIXES: dict[str, str] = {
    "Stone": "STONE",
    "FreeStone": "FREE_STONE",
    "PaidStone": "PAID_STONE",
    "Mitama": "MITAMA",
}

STORE_NAME = "GUEST"

#: The catalogue is built once: decoding 267 master payloads on every request took ~8s, long
#: enough for the client to give up and drop back to the title screen.
_product_cache: dict[str, list[dict[str, Any]]] = {}


def _product_payload(raw: str | None) -> dict[str, Any]:
    """Decode the base64 payload the master keeps next to each game product."""
    if not raw:
        return {}
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:
        return {}


def _store_product_entity(product_id: str, data: dict[str, Any], store: str) -> dict[str, Any]:
    """One ``ServerProductResponseEntity`` for the store screen, priced at zero."""
    payload = _product_payload(data.get("value"))
    prefix = str(payload.get("Prefix", ""))
    amount = int(payload.get("Count") or payload.get("Value") or 0)
    return {
        "storeProduct": {"store": store, "id": product_id},
        "tier": 0,
        "tierPrice": 0.0,
        # The client keeps this string as-is; the master's own payload is the honest value.
        "productPayload": str(data.get("value") or product_id),
        "description": prefix or str(data.get("search_label", "")),
        "title": product_id,
        "currency": "JPY",
        # Always present: the client reads its fields unconditionally.
        "exchanged": {
            "inGameCurrencyName": _CURRENCY_PREFIXES.get(prefix, prefix or "ITEM"),
            "amount": amount,
            "amountDetail": {"freeAmount": 0, "paidAmount": 0},
        },
    }


async def _product_catalogue(db: AsyncSession, store: str) -> list[dict[str, Any]]:
    """The free catalogue for one store, built once and reused."""
    cached = _product_cache.get(store)
    if cached is not None:
        return cached

    from dankagu.models.master import MasterGameProduct

    result = await db.execute(
        select(MasterGameProduct.id, MasterGameProduct.data).where(
            MasterGameProduct.id.like("ShopProduct%")
        )
    )
    rows = result.all()

    products: list[dict[str, Any]] = []
    for product_id, raw in rows:
        try:
            data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except Exception:
            data = {}
        products.append(_store_product_entity(product_id, data, store))
    _product_cache[store] = products
    logger.info("🛒 [IAP] built the catalogue: %d product(s) for store=%s", len(products), store)
    return products


@router.api_route("/iap/product", methods=["GET", "POST"])
async def get_iap_products(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[dict[str, Any]]:
    """The store catalogue: every master ``ShopProduct_*`` row, priced at zero."""
    store = (request.query_params.get("store") or STORE_NAME).upper()
    products = await _product_catalogue(db, store)
    logger.info("🛒 [IAP] /iap/product store=%s -> %d free product(s)", store, len(products))
    return products


@router.api_route("/iap/purchase-summary", methods=["GET", "POST"])
@router.api_route("/iap/purchase-summary/{path:path}", methods=["GET", "POST"])
async def get_iap_purchase_summary(path: str = "") -> dict[str, Any]:
    """Nothing was ever really bought, so the summary is empty."""
    return {
        "storeProduct": {"store": STORE_NAME, "id": path},
        "count": 0,
        "amount": 0.0,
        "currencyCode": "JPY",
    }


@router.api_route("/iap/purchase/retry-incomplete-purchases", methods=["POST"])
async def post_iap_retry_incomplete_purchases() -> dict[str, Any]:
    """No purchase is left unfinished when everything is free."""
    return {"success": [], "error": []}


@router.api_route("/iap/purchase", methods=["POST"])
async def post_iap_purchase(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    """Verify a store purchase. Free: the receipt is accepted and the order recorded once."""
    body: dict[str, Any] = {}
    try:
        raw_body = await request.json()
        if isinstance(raw_body, dict):
            body = raw_body
    except Exception:
        pass

    store_order_id = str(body.get("storeOrderId") or "").strip()
    if not store_order_id:
        store_order_id = f"free-{int(time.time() * 1000)}"

    player = await _resolve_or_create_player(db, request)
    existing = await db.get(PurchaseReceipt, (player.id, store_order_id))
    if existing is not None:
        logger.info("🛒 [IAP] purchase replayed player=%s order=%s", player.id, store_order_id)
    else:
        db.add(
            PurchaseReceipt(
                player_id=player.id,
                transaction_id=store_order_id,
                product_id=str(body.get("storeProductId") or body.get("productId") or ""),
                card_ids="[]",
            )
        )
        await db.commit()
        logger.info(
            "🛒 [IAP] purchase accepted player=%s order=%s store=%s",
            player.id,
            store_order_id,
            body.get("store", STORE_NAME),
        )

    return {
        "storeOrderId": store_order_id,
        "receipt": str(body.get("receipt") or ""),
        "originalOrderId": store_order_id,
    }


@router.api_route("/iap/purchase/{path:path}", methods=["GET", "POST"])
async def get_iap_purchase_status(path: str = "") -> dict[str, Any]:
    """Purchase verification by path (the client's mock flow reaches this)."""
    logger.info("🛒 [IAP] purchase verify path=%r", path)
    return {
        "storeOrderId": path,
        "originalOrderId": path,
        "state": "verified",
        "productId": path,
        "errorMessage": "",
        "errorType": "",
        "errorCode": "",
    }


@router.api_route("/currency-exchange/user-balance", methods=["GET", "POST"])
@router.api_route("/currency-exchange/user-balance/ts", methods=["GET", "POST"])
@router.api_route("/currency-exchange/user-balance/{path:path}", methods=["GET", "POST"])
async def get_currency_exchange_user_balance(path: str = "") -> list[dict[str, Any]]:
    """Mock user currency exchange balance."""
    return []


@router.api_route("/in-game-user-info", methods=["GET", "POST"])
@router.api_route("/in-game-user-info/{path:path}", methods=["GET", "POST"])
async def get_in_game_user_info(path: str = "") -> dict[str, Any]:
    """Mock in-game user info."""
    return {}


@router.api_route("/analytics/permission", methods=["GET", "POST"])
@router.api_route("/analytics/permission/{path:path}", methods=["GET", "POST"])
async def get_analytics_permission(path: str = "") -> dict[str, bool]:
    """Mock analytics permission."""
    return {"isPermitted": True}


@router.post("/analytics/event", status_code=status.HTTP_204_NO_CONTENT)
async def post_analytics_event(request: Request) -> Response:
    """Mock analytics event ingestion and log decompressed telemetry."""
    try:
        body = await request.body()
        if body.startswith(b"\x1f\x8b"):
            decompressed = gzip.decompress(body)
            logger.info(
                "📊 [Analytics Event (gzip)] %s", decompressed.decode("utf-8", errors="replace")
            )
        elif body:
            logger.info("📊 [Analytics Event] %s", body.decode("utf-8", errors="replace"))
    except Exception as exc:
        logger.debug("Failed to decompress analytics event: %s", exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _decode_sdk_body(body: bytes, content_encoding: str) -> str:
    """Best-effort decode of an SDK log payload.

    The client may send the body gzip-encoded (advertising it via
    `Content-Encoding`), or as a bare gzip/deflate stream. It is usually text, but
    some SDK builds post protobuf, so fall back to a hex preview rather than
    raising.
    """
    if content_encoding.lower() == "gzip" or body.startswith(b"\x1f\x8b"):
        try:
            body = gzip.decompress(body)
        except Exception:
            pass
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        head = body[:512]
        return f"<binary {len(body)} bytes> {head.hex(' ')}"


class SdkLogResponse(BaseModel):
    """Acknowledged response; the client only checks that the call succeeded."""

    result: bool = True


@router.post("/sdk-log", response_model=SdkLogResponse)
@router.post("/gcp/sdk-log", response_model=SdkLogResponse)
@router.post("/gcp/log/sdk-log", response_model=SdkLogResponse)
async def post_sdk_log(request: Request) -> SdkLogResponse:
    """Capture the LCX SDK's own log uploads.

    The client posts its SDK logs (and, on failure paths, tamper/security reports)
    here. Logging the payload verbatim is the point: it surfaces what the client
    thinks went wrong, which the earlier 404 was throwing away.
    """
    try:
        body = await request.body()
        encoding = request.headers.get("content-encoding", "")
        text = _decode_sdk_body(body, encoding)
        logger.warning(
            "📥 [SDK Log] %d bytes (encoding=%r) from %s\n%s",
            len(body),
            encoding or "identity",
            request.client.host if request.client else "?",
            text,
        )
    except Exception as exc:  # never fail the upload
        logger.warning("📥 [SDK Log] could not read body: %s", exc)
    return SdkLogResponse()


@router.api_route("/identity/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def fallback_identity(path: str) -> dict[str, Any]:
    """Fallback catch-all for any unhandled identity requests."""
    return {"hasAnyLink": True, "success": True}
