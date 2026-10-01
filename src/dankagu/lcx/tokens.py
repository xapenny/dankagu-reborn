"""JWT Token utilities for LCX Authentication."""

import time
import uuid
from typing import Any

import jwt

from dankagu.config import settings

ALGORITHM = "HS256"


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


def generate_lcx_tokens(user_id: str, registered_at: int) -> dict[str, str]:
    """Generate all tokens required by LCX authentication flow."""
    session_id = str(uuid.uuid4())
    analytics_session_id = str(uuid.uuid4())

    access_token_payload = {
        "kind": "ACCESS_TOKEN",
        "jti": str(uuid.uuid4()),
        "data": {
            "ch": "SDK",
            "session": {
                "storeAccountIdHash": "dankagu-local-account-hash",
                "bundleId": "com.dena.a12026801",
                "userRegisteredAtMillis": registered_at * 1000,
                "store": "GUEST",
                "userId": user_id,
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
            "storeAccountIdHash": "dankagu-local-account-hash",
            "isMock": False,
            "store": "GUEST",
            "sessionId": session_id,
            "userId": user_id,
        },
    }

    return {
        "accessToken": create_token(access_token_payload, expires_in=3600),
        "lcxUserIdToken": create_token(user_id_token_payload, expires_in=86400),
        "signInSessionToken": create_token(sign_in_session_payload, expires_in=86400 * 30),
        "signInSessionId": session_id,
        "analyticsSessionId": analytics_session_id,
    }
