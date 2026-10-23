"""Security and identity invariants for the LCX auth surface.

The ported `test_lcx.py` only asserts that endpoints answer with HTTP 200. These
tests cover the two properties that actually matter operationally:

* token signing fails closed when no secret is configured, and
* repeated sign-in returns a stable identity rather than minting a new player.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from dankagu.config import MissingSecretError, settings
from dankagu.lcx.app import create_lcx_app
from dankagu.lcx.tokens import create_token

_BASE = "https://danmakujp4-v1.lcx.tokyo"


async def _post(
    path: str,
    headers: dict[str, str] | None = None,
    json: object = None,
) -> tuple[int, dict[str, object]]:
    app = create_lcx_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
        response = await client.post(path, headers=headers, json=json)
        body = response.json() if response.content else {}
        return response.status_code, body


def test_missing_jwt_secret_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no secret configured, token creation must raise rather than guess.

    An implicit default would let anyone who reads the source forge a session,
    which is exactly the failure this project moved away from.
    """
    monkeypatch.setattr(settings, "jwt_secret", "", raising=False)
    with pytest.raises(MissingSecretError):
        create_token({"kind": "ACCESS_TOKEN"})


@pytest.mark.asyncio
async def test_sign_in_is_stable_across_repeated_calls() -> None:
    """Two sign-ins from the same device must resolve to the same player identity."""
    dev_headers = {"deviceAccount": "test-stable-device-id"}
    status_a, first = await _post("/identity/sign-in", headers=dev_headers)
    status_b, second = await _post("/identity/transfer/guest_user_1/execute", headers=dev_headers)

    assert status_a == 200
    assert status_b == 200

    user_a = first["user"]
    user_b = second["user"]
    assert isinstance(user_a, dict) and isinstance(user_b, dict)
    assert user_a["store"] == "GUEST"
    assert user_b["store"] == "GUEST"
    assert user_a["id"] == user_b["id"]

    # A different device resolves to a distinct player
    status_c, third = await _post(
        "/identity/sign-in", headers={"deviceAccount": "test-other-device-id"}
    )
    assert status_c == 200
    user_c = third["user"]
    assert isinstance(user_c, dict)
    assert user_c["id"] != user_a["id"]


@pytest.mark.asyncio
async def test_verified_token_response_shape() -> None:
    """The client consumes every one of these fields during boot."""
    status, body = await _post("/identity/sign-in")
    assert status == 200

    for field in (
        "accessToken",
        "lcxUserIdToken",
        "signInSessionToken",
        "signInSessionId",
        "analyticsSessionId",
    ):
        assert body.get(field), f"missing/empty {field}"

    # govPolicy drives a consent popup; it must be present and disable it.
    gov = body["govPolicy"]
    assert isinstance(gov, dict)
    assert gov["enablePopup"] is False


@pytest.mark.asyncio
async def test_user_id_token_is_a_jwt_for_the_same_subject() -> None:
    status, body = await _post("/identity/sign-in")
    assert status == 200

    import jwt

    decoded = jwt.decode(
        str(body["lcxUserIdToken"]),
        settings.require_jwt_secret(),
        algorithms=["HS256"],
    )
    assert decoded["kind"] == "LCX_USER_ID_TOKEN"
    assert decoded["sub"] == body["user"]["id"]  # type: ignore[index]
