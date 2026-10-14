"""Account transfer lifecycle: the code must survive, diagnose, and actually move.

Guards the 2026-10-13 report -- a correctly typed transfer ID + password answered
``LCX_INVALID_TRANSFER_ID_OR_PASSWORD`` because the code row had been cascaded away
with its account, and that error string cannot tell the two cases apart.
"""

import time
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from dankagu.config import settings
from dankagu.core.database import get_sessionmaker
from dankagu.core.session import create_session, resolve_session_player_id
from dankagu.lcx.app import create_lcx_app
from dankagu.lcx.router import FAR_FUTURE
from dankagu.models.player import Player
from dankagu.models.transfer import AccountTransfer

_BASE = "https://danmakujp4-v1.lcx.tokyo"
_BODY = {"gameServerNotificationUrlFilters": []}


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_lcx_app()), base_url=_BASE)


async def _sign_in(client: AsyncClient, device: str) -> dict:
    res = await client.post("/identity/sign-in", headers={"deviceAccount": device})
    assert res.status_code == 200
    return res.json()


async def _register_code(client: AsyncClient, device: str, password: str) -> dict:
    sign_in = await _sign_in(client, device)
    transfer_id = f"DK{uuid.uuid4().hex[:6].upper()}"
    res = await client.post(
        "/identity/transfer",
        headers={"deviceAccount": device},
        json={"id": transfer_id, "password": password},
    )
    assert res.status_code == 200, res.text
    return {"id": transfer_id, "password": password, **sign_in}


async def _execute(client: AsyncClient, transfer_id: str, password: str, device: str):
    return await client.post(
        f"/identity/transfer/{transfer_id}/execute",
        headers={"deviceAccount": device},
        json={"password": password},
    )


@pytest.mark.asyncio
async def test_code_survives_account_deletion_and_says_so() -> None:
    """Deleting the account must not silently invalidate an issued code."""
    device = f"transfer-src-{uuid.uuid4()}"
    async with _client() as client:
        code = await _register_code(client, device, "smoke-pass-1")

        deleted = await client.post(
            "/identity/deactivate-user",
            json=_BODY,
            headers={"Authorization": f"Bearer {code['accessToken']}"},
        )
        assert deleted.status_code == 200

        # The row survives with player_id = NULL instead of being cascaded away.
        async with get_sessionmaker()() as session:
            row = (
                await session.execute(
                    select(AccountTransfer).where(AccountTransfer.transfer_id == code["id"])
                )
            ).scalar_one_or_none()
        assert row is not None, "the transfer code was deleted together with the account"
        assert row.player_id is None

        res = await _execute(client, code["id"], "smoke-pass-1", f"transfer-dst-{uuid.uuid4()}")

    assert res.status_code == 404
    assert res.json()["code"] == "LCX_TRANSFER_SOURCE_ACCOUNT_DELETED"


@pytest.mark.asyncio
async def test_wrong_password_keeps_the_client_visible_code_and_the_code_usable() -> None:
    """A mistyped password is the one failure a player can fix: keep the old string."""
    device = f"transfer-src-{uuid.uuid4()}"
    async with _client() as client:
        code = await _register_code(client, device, "correct-horse")

        wrong = await _execute(client, code["id"], "wrong-horse", f"dst-{uuid.uuid4()}")
        assert wrong.status_code == 400
        assert wrong.json()["code"] == "LCX_INVALID_TRANSFER_ID_OR_PASSWORD"

        async with get_sessionmaker()() as session:
            row = (
                await session.execute(
                    select(AccountTransfer).where(AccountTransfer.transfer_id == code["id"])
                )
            ).scalar_one()
        assert row.is_used is False, "a failed attempt must not burn the code"

        ok = await _execute(client, code["id"], "correct-horse", f"dst-{uuid.uuid4()}")

    assert ok.status_code == 200
    assert ok.json()["user"]["id"] == code["user"]["id"]


@pytest.mark.asyncio
async def test_unknown_code_is_reported_as_not_found() -> None:
    async with _client() as client:
        res = await _execute(
            client, f"DK{uuid.uuid4().hex[:6].upper()}", "x", f"dst-{uuid.uuid4()}"
        )
    assert res.status_code == 400
    assert res.json()["code"] == "LCX_TRANSFER_ID_NOT_FOUND"


@pytest.mark.asyncio
async def test_transfer_moves_the_account_and_kills_the_old_device_session() -> None:
    """After a transfer the account lives on one device, not two."""
    device_a = f"transfer-src-{uuid.uuid4()}"
    device_b = f"transfer-dst-{uuid.uuid4()}"

    async with _client() as client:
        code = await _register_code(client, device_a, "smoke-pass-1")

        async with get_sessionmaker()() as session:
            player = (
                await session.execute(select(Player).where(Player.user_id == code["user"]["id"]))
            ).scalar_one()
        player_id = player.id
        old_session = await create_session(player_id, code["user"]["id"])
        assert await resolve_session_player_id(old_session) == player_id

        moved = await _execute(client, code["id"], "smoke-pass-1", device_b)
        assert moved.status_code == 200
        assert moved.json()["user"]["id"] == code["user"]["id"]

        # The code is single use.
        again = await _execute(client, code["id"], "smoke-pass-1", f"dst-{uuid.uuid4()}")
        assert again.status_code == 400
        assert again.json()["code"] == "LCX_TRANSFER_ID_ALREADY_USED"

    # The old device cannot keep using its session.
    assert await resolve_session_player_id(old_session) is None

    async with get_sessionmaker()() as session:
        player = await session.get(Player, player_id)
    assert player is not None
    assert player.device_account == device_b


@pytest.mark.asyncio
async def test_a_code_outlives_a_phone_by_default() -> None:
    """Ten years, so a code is the recovery path for a lost device, not a 90 day one."""
    device = f"ttl-{uuid.uuid4()}"
    async with _client() as client:
        code = await _register_code(client, device, "smoke-pass-1")

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(AccountTransfer).where(AccountTransfer.transfer_id == code["id"])
            )
        ).scalar_one()
    remaining_days = (row.expires_at - int(time.time())) / 86400
    assert remaining_days > 3640


@pytest.mark.asyncio
async def test_the_ttl_can_be_made_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``DANKAGU_TRANSFER_TTL_DAYS=0`` means the code never expires."""
    monkeypatch.setattr(settings, "transfer_ttl_days", 0, raising=False)
    device = f"forever-{uuid.uuid4()}"
    async with _client() as client:
        code = await _register_code(client, device, "smoke-pass-1")

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(AccountTransfer).where(AccountTransfer.transfer_id == code["id"])
            )
        ).scalar_one()
    assert row.expires_at == FAR_FUTURE


@pytest.mark.asyncio
async def test_a_short_password_is_refused() -> None:
    """The ID is eight digits, so the password carries the account's safety."""
    device = f"shortpw-{uuid.uuid4()}"
    async with _client() as client:
        await _sign_in(client, device)
        res = await client.post(
            "/identity/transfer",
            headers={"deviceAccount": device},
            json={"id": f"DK{uuid.uuid4().hex[:6].upper()}", "password": "1234"},
        )
    assert res.status_code == 400
    assert res.json()["code"] == "LCX_TRANSFER_PASSWORD_TOO_SHORT"


@pytest.mark.asyncio
async def test_registering_a_new_code_retires_the_old_one() -> None:
    """A player who regenerates should not leave live credentials behind."""
    device = f"rotate-{uuid.uuid4()}"
    async with _client() as client:
        first = await _register_code(client, device, "smoke-pass-1")
        second = await _register_code(client, device, "smoke-pass-2")

        retired = await _execute(client, first["id"], "smoke-pass-1", f"dst-{uuid.uuid4()}")
        current = await _execute(client, second["id"], "smoke-pass-2", f"dst-{uuid.uuid4()}")

    assert retired.status_code == 400
    assert retired.json()["code"] == "LCX_TRANSFER_ID_NOT_FOUND"
    assert current.status_code == 200


@pytest.mark.asyncio
async def test_repeated_guesses_are_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long-lived code must not be guessable at script speed."""
    monkeypatch.setattr(settings, "transfer_attempts_per_hour", 3, raising=False)
    device = f"throttle-{uuid.uuid4()}"
    async with _client() as client:
        code = await _register_code(client, device, "smoke-pass-1")

        statuses = []
        for _ in range(5):
            attempt = await _execute(client, code["id"], "wrong-password", f"dst-{uuid.uuid4()}")
            statuses.append((attempt.status_code, attempt.json().get("code")))

    assert statuses[0] == (400, "LCX_INVALID_TRANSFER_ID_OR_PASSWORD")
    assert statuses[-1] == (429, "LCX_TOO_MANY_ATTEMPTS")
