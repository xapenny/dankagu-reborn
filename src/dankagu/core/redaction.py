"""Redact credentials before anything reaches a log line.

The LCX request logger wrote the raw body, so a transfer registration printed
``{"id":"...","password":"..."}`` and a token refresh printed the whole
``signInSessionToken``; the gRPC interceptor printed message text the same way. Logs
get copied into bug reports and sit on the host for weeks, so secrets must not be in
them -- while an operator investigating an incident still needs the *shape* of the
request.

Three shapes are covered, because the same value arrives in all of them:

* JSON bodies: ``{"password": "..."}``
* protobuf text format: ``id_token: "eyJ..."``
* a bare token anywhere: ``eyJhbGciOi...`` with its two dots
"""

from __future__ import annotations

import re

#: Field names whose value is a credential. Matched case-insensitively.
SENSITIVE_FIELDS: frozenset[str] = frozenset(
    {
        "password",
        "newpassword",
        "passwordhash",
        "signinsessiontoken",
        "sessiontoken",
        "accesstoken",
        "lcxuseridtoken",
        "idtoken",
        "id_token",
        "verificationtoken",
        "refreshtoken",
        "authorization",
        "secret",
        "jwtsecret",
    }
)

REDACTED = "<redacted>"

#: ``"key": "value"`` in JSON. The closing quote may be missing when the caller
#: truncated the line first, so the value is matched up to the quote *or* the end.
_JSON_PAIR = re.compile(r'"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"(?:[^"\\]|\\.)*(?:"|$)')
#: ``key: "value"`` in protobuf text format (unquoted key), same tolerance.
_PROTO_PAIR = re.compile(r'\b(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*"(?:[^"\\]|\\.)*(?:"|$)')
#: A JWT, wherever it appears.
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")


def redact_text(text: str) -> str:
    """Return ``text`` with credential values replaced by ``<redacted>``.

    Redact *before* truncating for a log line: a token cut in half has no closing
    quote, and the remainder of it would otherwise be written out verbatim.
    """
    if not text:
        return text

    def _json_replacement(match: re.Match[str]) -> str:
        if match.group("key").lower() in SENSITIVE_FIELDS:
            return f'"{match.group("key")}": "{REDACTED}"'
        return match.group(0)

    def _proto_replacement(match: re.Match[str]) -> str:
        key = match.group("key")
        if key.lower() in SENSITIVE_FIELDS:
            return f'{key}: "{REDACTED}"'
        return match.group(0)

    redacted = _JSON_PAIR.sub(_json_replacement, text)
    redacted = _PROTO_PAIR.sub(_proto_replacement, redacted)
    return _JWT.sub(REDACTED, redacted)


def redact_mapping(payload: dict[str, object]) -> dict[str, object]:
    """Redact credentials from an already parsed body, for structured logging."""
    return {
        key: (REDACTED if key.lower() in SENSITIVE_FIELDS else value)
        for key, value in payload.items()
    }
