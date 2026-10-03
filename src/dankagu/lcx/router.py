"""FastAPI router implementing LCX Authentication & Identity endpoints."""

import gzip
import logging
import secrets
import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.core.database import get_db_session
from dankagu.lcx.tokens import decode_token_user_id, generate_lcx_tokens
from dankagu.models.player import Player

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
    user_id: str | None = None

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        user_id = decode_token_user_id(auth_header[7:].strip())

    if not user_id:
        try:
            body = await request.json()
            if isinstance(body, dict):
                user_id = body.get("userId")
                if not user_id and "signInSessionToken" in body:
                    user_id = decode_token_user_id(str(body["signInSessionToken"]))
                if not user_id and "accessToken" in body:
                    user_id = decode_token_user_id(str(body["accessToken"]))
        except Exception:
            pass

    now = int(time.time())
    player: Player | None = None
    if user_id:
        result = await db.execute(select(Player).where(Player.user_id == user_id))
        player = result.scalars().first()

    if player is None:
        if user_id:
            player = Player(user_id=user_id, registered_at=now)
            db.add(player)
            await db.commit()
            await db.refresh(player)
        else:
            result = await db.execute(select(Player).limit(1))
            player = result.scalars().first()
            if player is None:
                user_id = str(uuid.uuid4())
                player = Player(user_id=user_id, registered_at=now)
                db.add(player)
                await db.commit()
                await db.refresh(player)

    tokens = generate_lcx_tokens(player.user_id, player.registered_at)
    return {
        "userId": player.user_id,
        "accessToken": tokens["accessToken"],
        "signInSessionId": tokens["signInSessionId"],
        "analyticsSessionId": tokens["analyticsSessionId"],
    }


async def _resolve_or_create_player(
    db: AsyncSession, request: Request
) -> Player:
    """Retrieve player matching client credentials or register a distinct new player."""
    user_id: str | None = None
    device_account: str | None = None

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        user_id = decode_token_user_id(auth_header[7:].strip())

    try:
        body = await request.json()
        if isinstance(body, dict):
            if not user_id and "userId" in body and body["userId"]:
                user_id = str(body["userId"])
            if not user_id and "signInSessionToken" in body and body["signInSessionToken"]:
                user_id = decode_token_user_id(str(body["signInSessionToken"]))
            if not user_id and "sessionToken" in body and body["sessionToken"]:
                user_id = decode_token_user_id(str(body["sessionToken"]))
            device_account = body.get("storeAccountIdHash") or body.get("deviceAccount")
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
        if device_account or user_id:
            uid = user_id or str(uuid.uuid4())
            player = Player(user_id=uid, device_account=device_account, registered_at=now)
            db.add(player)
            await db.commit()
            await db.refresh(player)
        else:
            res = await db.execute(select(Player).order_by(Player.registered_at.asc()).limit(1))
            player = res.scalars().first()
            if player is None:
                uid = str(uuid.uuid4())
                player = Player(user_id=uid, registered_at=now)
                db.add(player)
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
@router.post("/identity/transfer")
@router.post("/identity/transfer/execute")
@router.post("/identity/transfer/{account_id}/execute")
async def post_identity_session(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    """Execute initial sign-in, session establishment, or account transfer."""
    player = await _resolve_or_create_player(db, request)
    return _build_verified_token_response(player)


@router.api_route("/identity/active-published-store", methods=["GET", "POST"])
@router.api_route("/gcp/identity/active-published-store", methods=["GET", "POST"])
async def get_active_published_store() -> dict[str, str]:
    """Return active store type (APPLE/GUEST)."""
    return {"store": "APPLE"}


@router.post("/identity/transfer/suggest")
async def post_transfer_suggest(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, str]:
    """Suggest account transfer credentials."""
    player = await _resolve_or_create_player(db, request)
    suggested_pw = secrets.token_hex(4).upper()
    return {"id": player.user_id[:8].upper(), "password": suggested_pw}


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
    return {"hasAnyLink": True}


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
async def post_deactivate_user() -> dict[str, bool]:
    """Mock user deactivation."""
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
