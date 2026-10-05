"""FastAPI router implementing LCX Authentication & Identity endpoints."""

import base64
import gzip
import logging
import time
import uuid
from typing import Annotated, Any

import jwt
from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.core.database import get_db_session
from dankagu.core.session import invalidate_player_sessions
from dankagu.core.starter import seed_starter_player
from dankagu.core.transfer import (
    generate_transfer_id,
    generate_transfer_password,
    hash_transfer_password,
    verify_transfer_password,
)
from dankagu.lcx.tokens import decode_token_user_id, generate_lcx_tokens
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
    tokens = generate_lcx_tokens(player.user_id, player.registered_at)
    return {
        "userId": player.user_id,
        "accessToken": tokens["accessToken"],
        "signInSessionId": tokens["signInSessionId"],
        "analyticsSessionId": tokens["analyticsSessionId"],
    }


async def _resolve_or_create_player(db: AsyncSession, request: Request) -> Player:
    """Retrieve player matching client credentials or register a distinct new player."""
    user_id: str | None = None
    device_account: str | None = None

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        user_id = decode_token_user_id(auth_header[7:].strip())

    if not device_account:
        device_account = request.headers.get("deviceAccount") or request.headers.get(
            "x-device-account"
        )

    try:
        body = await request.json()
        if isinstance(body, dict):
            if not user_id and "userId" in body and body["userId"]:
                user_id = str(body["userId"])
            if not user_id and "signInSessionToken" in body and body["signInSessionToken"]:
                user_id = decode_token_user_id(str(body["signInSessionToken"]))
            if not user_id and "sessionToken" in body and body["sessionToken"]:
                user_id = decode_token_user_id(str(body["sessionToken"]))
            if not device_account:
                device_account = body.get("storeAccountIdHash") or body.get("deviceAccount")
            if not device_account and "verificationToken" in body and body["verificationToken"]:
                try:
                    vt_payload = jwt.decode(
                        str(body["verificationToken"]),
                        options={"verify_signature": False},
                    )
                    iss = str(vt_payload.get("iss", ""))
                    if iss:
                        try:
                            # Typically base64 encoded bundle_id:device_identifier
                            decoded_iss = base64.b64decode(iss).decode("utf-8")
                            device_account = decoded_iss
                        except Exception:
                            device_account = iss
                except Exception:
                    pass
    except Exception:
        pass

    now = int(time.time())
    player: Player | None = None

    if user_id:
        res = await db.execute(select(Player).where(Player.user_id == user_id))
        player = res.scalars().first()

    if player is None and device_account:
        res = await db.execute(select(Player).where(Player.device_account == device_account))
        player = res.scalars().first()

    if player is None:
        uid = user_id or str(uuid.uuid4())
        player = Player(user_id=uid, device_account=device_account, registered_at=now)
        db.add(player)
        await db.flush()
        await seed_starter_player(db, player)
        await db.commit()
        await db.refresh(player)

    return player


def _build_verified_token_response(player: Player) -> dict[str, Any]:
    """Build LCX VerifiedTokenEntity response payload."""
    tokens = generate_lcx_tokens(player.user_id, player.registered_at)
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
    return _build_verified_token_response(player)


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
        return _build_verified_token_response(player)

    if not transfer_id:
        return JSONResponse(
            status_code=400,
            content={
                "code": "LCX_INVALID_TRANSFER_ID_OR_PASSWORD",
                "message": "Transfer ID and password required",
            },
        )

    stmt = select(AccountTransfer).where(
        AccountTransfer.transfer_id == transfer_id,
        AccountTransfer.is_used.is_(False),
    )
    res = await db.execute(stmt)
    transfer = res.scalars().first()

    if transfer is None or not verify_transfer_password(password, transfer.password_hash):
        return JSONResponse(
            status_code=400,
            content={
                "code": "LCX_INVALID_TRANSFER_ID_OR_PASSWORD",
                "message": "Invalid transfer ID or password",
            },
        )

    if transfer.expires_at < now:
        return JSONResponse(
            status_code=400,
            content={
                "code": "LCX_TRANSFER_ID_EXPIRED",
                "message": "Transfer ID has expired",
            },
        )

    # Transfer succeeds: mark used
    transfer.is_used = True

    target_player = await db.get(Player, transfer.player_id)
    if target_player is None:
        return JSONResponse(
            status_code=404,
            content={
                "code": "LCX_TRANSFER_TARGET_USER_NOT_FOUND",
                "message": "Target player not found",
            },
        )

    device_account = request.headers.get("deviceAccount") or body.get("deviceAccount")
    if device_account:
        target_player.device_account = str(device_account)

    await db.commit()

    tokens = generate_lcx_tokens(target_player.user_id, target_player.registered_at)
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
async def post_sign_out() -> Response:
    """Handle sign out request."""
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
    await db.execute(delete(AccountTransfer).where(AccountTransfer.player_id == pid))
    await db.execute(delete(WalletBalance).where(WalletBalance.player_id == pid))
    await db.execute(delete(PlayerInventoryItem).where(PlayerInventoryItem.player_id == pid))
    await db.execute(delete(Player).where(Player.id == pid))
    await db.commit()
    invalidate_player_sessions(pid)

    logger.info("🗑️  Deleted account player_id=%s user_id=%s", pid, player.user_id)
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
