"""Account transfer lifecycle: codes are issued, survive, diagnose, and move an account."""

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

        # Transfer codes remain reusable across future migrations.
        device_c = f"dst-{uuid.uuid4()}"
        again = await _execute(client, code["id"], "smoke-pass-1", device_c)
        assert again.status_code == 200
        assert again.json()["user"]["id"] == code["user"]["id"]

    # The old device cannot keep using its session.
    assert await resolve_session_player_id(old_session) is None

    async with get_sessionmaker()() as session:
        player = await session.get(Player, player_id)
    assert player is not None
    assert player.device_account == device_c


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


@pytest.mark.asyncio
async def test_a_code_replaces_the_account_that_held_the_target_device() -> None:
    """Changing phones: the account already on the new phone is replaced, not just unbound.

    ``players.device_account`` is unique, so assigning the identifier while the phone's
    current account still held it raised IntegrityError and the client showed a generic
    server error -- on the one flow a player cannot work around. The device belongs to the
    account being moved, so the account that lived there is deleted the way the client's
    own deactivate flow deletes one, and its still-valid token must not come back and take
    the device with it.
    """
    source_device = f"transfer-src-{uuid.uuid4()}"
    target_device = f"transfer-dst-{uuid.uuid4()}"

    async with _client() as client:
        # the phone's current account owns the identifier
        resident = await _sign_in(client, target_device)
        resident_user = resident["user"]["id"]

        code = await _register_code(client, source_device, "smoke-pass-1")
        moved = await _execute(client, code["id"], "smoke-pass-1", target_device)

        assert moved.status_code == 200, moved.text
        assert moved.json()["user"]["id"] == code["user"]["id"]

        # the replaced account is gone, and its still-valid token signs this device in as
        # a new player instead of resurrecting it or taking anything from the moved one
        replaced = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": target_device},
            json={"signInSessionToken": resident["signInSessionToken"]},
        )
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["userId"] != resident_user

        # the account that moved here is untouched
        refreshed = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": target_device},
            json={"signInSessionToken": moved.json()["signInSessionToken"]},
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["userId"] == code["user"]["id"]

    async with get_sessionmaker()() as session:
        moved_player = await session.get(Player, await _player_id(session, code["user"]["id"]))
    assert moved_player is not None
    assert moved_player.device_account == target_device

    async with get_sessionmaker()() as session:
        gone = (
            (await session.execute(select(Player).where(Player.user_id == resident_user)))
            .scalars()
            .first()
        )
    assert gone is None, "the account that held the device must have been deleted"


@pytest.mark.asyncio
async def test_executing_a_code_on_its_own_device_never_deletes_the_account() -> None:
    """Self-transfer: the device's account *is* the account being moved.

    The transfer deletes whichever account holds the target device, because changing
    phones replaces the account that was there. When that account is the one being moved,
    deleting it would destroy the very account the player asked to keep -- so the guard
    on player id matters more than the deletion itself.
    """
    device = f"transfer-self-{uuid.uuid4()}"
    async with _client() as client:
        code = await _register_code(client, device, "smoke-pass-1")
        moved = await _execute(client, code["id"], "smoke-pass-1", device)
        assert moved.status_code == 200, moved.text

        # the account is intact and still reachable from that device
        refreshed = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": device},
            json={"signInSessionToken": moved.json()["signInSessionToken"]},
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["userId"] == code["user"]["id"]

    async with get_sessionmaker()() as session:
        player = await session.get(Player, await _player_id(session, code["user"]["id"]))
    assert player is not None, "a self-transfer must never delete the account"
    assert player.device_account == device


async def _player_id(session, user_id: str) -> str:
    """The player row id behind an LCX user id."""
    return (await session.execute(select(Player.id).where(Player.user_id == user_id))).scalar_one()


@pytest.mark.asyncio
async def test_a_spent_code_id_can_be_registered_again() -> None:
    """Re-registering an existing transfer id by its owner updates the password cleanly."""
    device = f"reuse-id-{uuid.uuid4()}"
    dst_device = f"dst-{uuid.uuid4()}"
    transfer_id = f"DK{uuid.uuid4().hex[:6].upper()}"
    async with _client() as client:
        await _sign_in(client, device)
        first = await client.post(
            "/identity/transfer",
            headers={"deviceAccount": device},
            json={"id": transfer_id, "password": "smoke-pass-1"},
        )
        assert first.status_code == 200, first.text
        spent = await _execute(client, transfer_id, "smoke-pass-1", dst_device)
        assert spent.status_code == 200

        again = await client.post(
            "/identity/transfer",
            headers={"deviceAccount": dst_device},
            json={"id": transfer_id, "password": "smoke-pass-2"},
        )
        assert again.status_code == 200, again.text
        reuse = await _execute(client, transfer_id, "smoke-pass-2", f"dst-{uuid.uuid4()}")
        assert reuse.status_code == 200
