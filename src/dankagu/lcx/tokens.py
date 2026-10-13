"""JWT Token utilities for LCX Authentication."""

import hashlib
import logging
import time
import uuid
from typing import Any

import jwt

from dankagu.config import settings

logger = logging.getLogger("dankagu.lcx.tokens")

ALGORITHM = "HS256"

#: Claim carrying the device a token was issued to (see ``token_device_hash``).
DEVICE_CLAIM = "deviceHash"


def create_token(payload: dict[str, Any], expires_in: int = 86400) -> str:
    """Create a signed JWT token.

    Raises:
        MissingSecretError: If ``DANKAGU_JWT_SECRET`` has not been configured.
            Signing with an implicit default would let anyone forge sessions.
    """
    now = int(time.time())
    to_encode = payload.copy()
    to_encode.setdefault("iat", now)
    to_encode.setdefault("exp", now + expires_in)
    return jwt.encode(to_encode, settings.require_jwt_secret(), algorithm=ALGORITHM)


def derive_store_account_hash(user_id: str) -> str:
    """Per-player stand-in for the LCX ``storeAccountIdHash``.

    The old implementation shipped the constant ``"dankagu-local-account-hash"``
    in every token, and the identity resolver happily treated that constant as a
    *device* identifier, which made every install look like the same device.
    Deriving it from the player keeps it stable per account and unique.
    """
    return hashlib.sha256(f"lcx-store:{user_id}".encode()).hexdigest()[:32]


def generate_lcx_tokens(
    user_id: str, registered_at: int, device_hash: str | None = None
) -> dict[str, str]:
    """Generate all tokens required by LCX authentication flow.

    ``device_hash`` is the device the tokens are issued to. When known it is
    embedded as a claim so that a later refresh presenting the same token from a
    *different* device can be rejected (``token_device_hash``).
    """
    session_id = str(uuid.uuid4())
    analytics_session_id = str(uuid.uuid4())
    store_account_hash = device_hash or derive_store_account_hash(user_id)
    binding: dict[str, str] = {DEVICE_CLAIM: device_hash} if device_hash else {}

    access_token_payload = {
        "kind": "ACCESS_TOKEN",
        "jti": str(uuid.uuid4()),
        "data": {
            "ch": "SDK",
            "session": {
                "storeAccountIdHash": store_account_hash,
                "bundleId": "com.dena.a12026801",
                "userRegisteredAtMillis": registered_at * 1000,
                "store": "GUEST",
                "userId": user_id,
                **binding,
            },
        },
    }

    user_id_token_payload = {
        "sub": user_id,
        "kind": "LCX_USER_ID_TOKEN",
        "data": {
            "userRegisteredAt": registered_at,
        },
    }

    sign_in_session_payload = {
        "kind": "SIGN_IN_SESSION_TOKEN",
        "data": {
            "storeAccountIdHash": store_account_hash,
            "isMock": False,
            "store": "GUEST",
            "sessionId": session_id,
            "userId": user_id,
            **binding,
        },
    }

    return {
        "accessToken": create_token(access_token_payload, expires_in=3600),
        "lcxUserIdToken": create_token(user_id_token_payload, expires_in=86400),
        "signInSessionToken": create_token(
            sign_in_session_payload, expires_in=settings.sign_in_session_ttl_seconds
        ),
        "signInSessionId": session_id,
        "analyticsSessionId": analytics_session_id,
    }


def decode_token_payload(token: str) -> dict[str, Any] | None:
    """Verify a token signature and return its payload, or ``None``.

    Verification is mandatory. The previous implementation fell back to
    ``verify_signature: False``, which meant that anybody holding *any* token —
    including one copied from another install — could present it as their own
    identity.
    """
    if not token:
        return None
    try:
        return jwt.decode(
            token,
            settings.require_jwt_secret(),
            algorithms=[ALGORITHM],
            options={"verify_exp": False},
        )
    except Exception as exc:  # pragma: no cover - exercised via tests
        logger.debug("Rejected unverifiable token: %s", exc)
        return None


def user_id_from_payload(payload: dict[str, Any] | None) -> str | None:
    """Extract the user id from a verified token payload."""
    if not payload:
        return None
    if payload.get("sub"):
        return str(payload["sub"])
    data = payload.get("data")
    if isinstance(data, dict):
        if data.get("userId"):
            return str(data["userId"])
        session = data.get("session")
        if isinstance(session, dict) and session.get("userId"):
            return str(session["userId"])
    return None


def token_device_hash(payload: dict[str, Any] | None) -> str | None:
    """Return the device a verified token was issued to, if it recorded one."""
    if not payload:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    bound = data.get(DEVICE_CLAIM)
    if bound:
        return str(bound)
    session = data.get("session")
    if isinstance(session, dict) and session.get(DEVICE_CLAIM):
        return str(session[DEVICE_CLAIM])
    return None


def decode_token_user_id(token: str) -> str | None:
    """Extract user_id from an LCX JWT token (signature verified)."""
    return user_id_from_payload(decode_token_payload(token))
