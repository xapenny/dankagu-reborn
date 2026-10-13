"""FastAPI router implementing LCX Authentication & Identity endpoints."""

import base64
import gzip
import logging
import time
from typing import Annotated, Any

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.config import settings
from dankagu.core.database import get_db_session
from dankagu.core.identity import find_player, normalise_device_account, resolve_or_create_player
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
from dankagu.lcx.tokens import (
    decode_token_payload,
    decode_token_user_id,
    generate_lcx_tokens,
    token_device_hash,
    user_id_from_payload,
)
from dankagu.models.inventory import PlayerInventoryItem
from dankagu.models.player import Player
from dankagu.models.session import Session
from dankagu.models.storage import PlayerStorageEntry
from dankagu.models.transfer import AccountTransfer
from dankagu.models.wallet import WalletBalance

logger = logging.getLogger("dankagu.lcx.router")

router = APIRouter(tags=["LCX"])


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


def _device_from_verification_token(verification_token: str) -> str | None:
    """Best-effort device hint from an LCX verification token.

    The token is signed by the platform, not by us, so it can only ever be a
    *hint*: it never grants access to an existing account on its own (see
    :mod:`dankagu.core.identity`).
    """
    try:
        payload = jwt.decode(verification_token, options={"verify_signature": False})
    except Exception:
        return None
    iss = str(payload.get("iss", ""))
    if not iss:
        return None
    try:
        # Typically base64 encoded bundle_id:device_identifier
        return base64.b64decode(iss).decode("utf-8")
    except Exception:
        return iss


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
            # NOTE: ``storeAccountIdHash`` is deliberately *not* accepted as a
            # device identifier. This server mints it (per player), so treating it
            # as one used to make every install look like the same device.
            device_account = body.get("deviceAccount")
        if not device_account and body.get("verificationToken"):
            device_account = _device_from_verification_token(str(body["verificationToken"]))

    payload = decode_token_payload(presented_token) if presented_token else None
    if payload is not None:
        user_id = user_id_from_payload(payload)
        bound_device = token_device_hash(payload)
        request_device = normalise_device_account(device_account)
        if bound_device and request_device and bound_device != request_device:
            logger.warning(
                "🔒 [Auth] Rejected token issued to device %r presented from %r",
                bound_device,
                request_device,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": "LCX_INVALID_SESSION_DEVICE",
                    "message": "Session token was issued to another device",
                },
            )
        if not bound_device and request_device and settings.require_device_bound_tokens:
            # A token minted before device binding existed records no device, so there
            # is nothing in the token itself to compare. Refusing it outright locked
            # every existing install out of its own account (2026-10-13), so check the
            # claim against the database instead: the account the token names must
            # already belong to this device, or have no device yet (first claim). A
            # token copied to another device still fails here.
            owner, _matched = await find_player(db, user_id=user_id, device_account=None)
            if owner is None or (owner.device_account and owner.device_account != request_device):
                logger.warning(
                    "🔒 [Auth] Rejected unbound token for device %r: account %s is %s",
                    request_device,
                    user_id,
                    "unknown" if owner is None else f"bound to {owner.device_account!r}",
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={
                        "code": "LCX_SESSION_NOT_BOUND_TO_DEVICE",
                        "message": "Session token predates device binding; sign in again",
                    },
                )
            # The account is this device's: bind it now, so the tokens issued below
            # carry the binding and the strict check applies from the next refresh on.
            if not owner.device_account:
                owner.device_account = request_device
                await db.commit()
            logger.info(
                "🔗 [Auth] Accepted pre-binding token for player %s on device %r",
                owner.id,
                request_device,
            )
    elif presented_token:
        logger.warning("🔒 [Auth] Ignoring unverifiable token presented by the client")

    player, _matched_by = await resolve_or_create_player(
        db, user_id=user_id, device_account=device_account
    )
    request.state.device_account = normalise_device_account(device_account)
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
@router.post("/identity/pre-sign-in")
@router.post("/identity/mock-sign-in")
async def post_identity_session(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
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

    player = await _resolve_or_create_player(db, request)

    # Check if active unused transfer already exists with this ID
    stmt = select(AccountTransfer).where(
        AccountTransfer.transfer_id == transfer_id,
        AccountTransfer.is_used.is_(False),
        AccountTransfer.expires_at > now,
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

    expires_at = now + 90 * 86400  # 90 days validity
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

    if transfer.is_used:
        logger.warning("🎫 [Transfer] Transfer id %r was already used", transfer_id)
        return JSONResponse(
            status_code=400,
            content={
                "code": "LCX_TRANSFER_ID_ALREADY_USED",
                "message": "Transfer ID has already been used",
            },
        )

    if transfer.expires_at < now:
        logger.warning("🎫 [Transfer] Transfer id %r expired", transfer_id)
        return JSONResponse(
            status_code=400,
            content={
                "code": "LCX_TRANSFER_ID_EXPIRED",
                "message": "Transfer ID has expired",
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

    # Transfer succeeds: single use.
    transfer.is_used = True

    device_account = normalise_device_account(
        request.headers.get("deviceAccount")
        or body.get("deviceAccount")
        or _device_from_verification_token(str(body.get("verificationToken", "")))
    )
    if device_account:
        target_player.device_account = device_account

    await db.commit()

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

    pid = player.id
    # Explicit deletes: player_storage_entries has no FK, and SQLite does not
    # enforce ON DELETE CASCADE unless the pragma is on.
    await db.execute(delete(PlayerStorageEntry).where(PlayerStorageEntry.player_id == pid))
    await db.execute(delete(Session).where(Session.player_id == pid))
    # Transfer codes are detached and revoked instead of deleted: an issued code
    # must not vanish silently behind "transfer ID or password is wrong" when the
    # player tries to use it after the account is gone (2026-10-13 report). A
    # detached, revoked row can never restore anything.
    await db.execute(
        update(AccountTransfer)
        .where(AccountTransfer.player_id == pid)
        .values(player_id=None, is_used=True)
    )
    await db.execute(delete(WalletBalance).where(WalletBalance.player_id == pid))
    await db.execute(delete(PlayerInventoryItem).where(PlayerInventoryItem.player_id == pid))
    await db.execute(delete(Player).where(Player.id == pid))
    await db.commit()
    invalidate_player_sessions(pid)

    logger.info("🗑️ Deleted account player_id=%s user_id=%s", pid, player.user_id)
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
