"""Credentials must not reach a log line.

The LCX logger printed the raw request body, so a transfer registration wrote
``{"id": "...", "password": "..."}`` and every token refresh wrote the whole
``signInSessionToken``; the gRPC interceptor printed message text the same way.
"""

import base64
import json
import logging

import pytest

from dankagu.core.redaction import REDACTED, redact_mapping, redact_text


def _b64(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


#: A structurally valid JWT, assembled at runtime so this file contains no literal
#: that a secret scanner (or a reviewer) would have to investigate.
#: The contents are irrelevant; the three-dot shape is what leaks into logs.
TOKEN = ".".join(
    (
        _b64(b'{"alg":"HS256","typ":"JWT"}'),
        _b64(b'{"kind":"SIGN_IN_SESSION_TOKEN","data":{"userId":"u-1"}}'),
        "signature-placeholder",
    )
)


def test_json_password_is_redacted() -> None:
    body = json.dumps({"id": "DK123456", "password": "hunter2"})
    out = redact_text(body)
    assert "hunter2" not in out
    assert REDACTED in out
    # The shape of the request survives, which is what makes logs useful.
    assert "DK123456" in out


def test_json_tokens_are_redacted() -> None:
    body = json.dumps(
        {
            "userId": "u-1",
            "signInSessionToken": TOKEN,
            "accessToken": TOKEN,
            "lcxUserIdToken": TOKEN,
        }
    )
    out = redact_text(body)
    assert TOKEN not in out
    assert out.count(REDACTED) == 3
    assert "u-1" in out


def test_bare_token_is_redacted() -> None:
    out = redact_text(f"authorization: Bearer {TOKEN} sent")
    assert TOKEN not in out
    assert REDACTED in out


def test_protobuf_text_format_is_redacted() -> None:
    message = f'id_token: "{TOKEN}"\ndevice_account: "com.dena.a12026801:abc"'
    out = redact_text(message)
    assert TOKEN not in out
    assert "com.dena.a12026801:abc" in out


def test_truncated_token_is_still_redacted() -> None:
    """A log line is cut at 1000 characters; the token must be gone before that."""
    body = json.dumps({"signInSessionToken": TOKEN})[:40]
    out = redact_text(body)
    assert "eyJ" not in out, f"half a token survived: {out}"


def test_mapping_redaction_keeps_other_fields() -> None:
    out = redact_mapping({"password": "hunter2", "id": "DK1", "productId": "G_1_10_TT_L"})
    assert out["password"] == REDACTED
    assert out["id"] == "DK1"
    assert out["productId"] == "G_1_10_TT_L"


@pytest.mark.asyncio
async def test_the_lcx_request_logger_does_not_write_the_password(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """End to end through the middleware: the logged line must be safe."""
    from httpx import ASGITransport, AsyncClient

    from dankagu.lcx.app import create_lcx_app

    caplog.set_level(logging.INFO, logger="dankagu.http")
    app = create_lcx_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://danmakujp4-v1.lcx.tokyo"
    ) as client:
        await client.post(
            "/identity/transfer",
            headers={"deviceAccount": "redaction-test-device"},
            json={"id": "DK654321", "password": "hunter2"},
        )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "hunter2" not in logged, f"password leaked into the log: {logged}"
    assert "DK654321" in logged or "identity/transfer" in logged
