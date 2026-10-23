"""Account deletion (`/identity/deactivate-user`) must actually remove data.

Each test signs in from its own fresh ``deviceAccount`` so it only ever deletes
the player it created, never a real account in the shared database.

Identities are taken from the server response rather than asserted upfront: a
bare ``userId`` in the request body is deliberately **not** trusted as an identity
any more (it let anybody claim somebody else's account).
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text

from dankagu.core.database import get_sessionmaker
from dankagu.lcx.app import create_lcx_app
from dankagu.models.player import Player
from dankagu.models.social import MySpaceLikeRecord, PhotoLikeRecord, PlayerPhoto
from dankagu.models.storage import PlayerStorageEntry

_BASE = "https://danmakujp4-v1.lcx.tokyo"
_BODY = {"gameServerNotificationUrlFilters": []}


async def _player_and_storage_count(user_id: str) -> tuple[Player | None, int]:
    async with get_sessionmaker()() as session:
        player = (
            await session.execute(select(Player).where(Player.user_id == user_id))
        ).scalar_one_or_none()
        if player is None:
            return None, 0
        count = (
            await session.execute(
                select(func.count())
                .select_from(PlayerStorageEntry)
                .where(PlayerStorageEntry.player_id == player.id)
            )
        ).scalar_one()
        return player, int(count)


@pytest.mark.asyncio
async def test_deactivate_user_deletes_player_and_storage() -> None:
    device = f"delete-test-device-{uuid.uuid4()}"
    app = create_lcx_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
        sign_in = await client.post("/identity/sign-in", headers={"deviceAccount": device})
        assert sign_in.status_code == 200
        sign_in_body = sign_in.json()
        user_id = sign_in_body["user"]["id"]
        access_token = sign_in_body["accessToken"]

        player, storage_rows = await _player_and_storage_count(user_id)
        assert player is not None
        assert storage_rows > 0, "starter seed should have created storage rows"

        res = await client.post(
            "/identity/deactivate-user",
            json=_BODY,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert res.status_code == 200

    player, storage_rows = await _player_and_storage_count(user_id)
    assert player is None
    assert storage_rows == 0


@pytest.mark.asyncio
async def test_deactivate_user_without_token_deletes_nothing() -> None:
    device = f"delete-test-device-{uuid.uuid4()}"
    app = create_lcx_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
        sign_in = await client.post("/identity/sign-in", headers={"deviceAccount": device})
        assert sign_in.status_code == 200
        user_id = sign_in.json()["user"]["id"]

        res = await client.post("/identity/deactivate-user", json=_BODY)
        assert res.status_code == 401

    player, _ = await _player_and_storage_count(user_id)
    assert player is not None


async def test_deleting_the_player_row_cascades_every_player_scoped_table() -> None:
    """The database itself must remove the account's data, not a hand-kept table list.

    ``player_storage_entries``, ``player_photos``, ``photo_likes`` and
    ``my_space_likes`` had no foreign key, so anything the application forgot survived
    a deletion. This deletes the ``players`` row directly -- the way an operator's SQL
    would -- and expects every dependent row to be gone.
    """
    device = f"cascade-test-{uuid.uuid4()}"
    app = create_lcx_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
        sign_in = await client.post("/identity/sign-in", headers={"deviceAccount": device})
        assert sign_in.status_code == 200
        user_id = sign_in.json()["user"]["id"]

    async with get_sessionmaker()() as session:
        player = (
            await session.execute(select(Player).where(Player.user_id == user_id))
        ).scalar_one()
        player_id = player.id

        other = Player(user_id=f"cascade-other-{uuid.uuid4()}", registered_at=0)
        session.add(other)
        await session.flush()

        session.add(
            PlayerPhoto(
                photo_id=f"photo-{uuid.uuid4()}",
                player_id=player_id,
                character_ids=[],
                category="",
                content_value=b"",
                original_url="",
                thumbnail_url="",
                created_at=0,
            )
        )
        await session.flush()
        photo_id = (
            await session.execute(
                select(PlayerPhoto.photo_id).where(PlayerPhoto.player_id == player_id)
            )
        ).scalar_one()

        session.add(PhotoLikeRecord(photo_id=photo_id, player_id=other.id, created_at=0))
        session.add(MySpaceLikeRecord(from_player_id=other.id, to_player_id=player_id, liked_at=0))
        await session.commit()

    async with get_sessionmaker()() as session:
        await session.execute(delete(Player).where(Player.id == player_id))
        await session.commit()

        remaining = {
            "player_storage_entries": await _count(
                "player_storage_entries", "player_id", player_id
            ),
            "player_photos": await _count("player_photos", "player_id", player_id),
            "photo_likes": await _count("photo_likes", "player_id", player_id),
            "my_space_likes": await _count("my_space_likes", "to_player_id", player_id),
        }
    assert remaining == {
        "player_storage_entries": 0,
        "player_photos": 0,
        "photo_likes": 0,
        "my_space_likes": 0,
    }


async def _count(table: str, column: str, value: str) -> int:
    async with get_sessionmaker()() as session:
        return int(
            (
                await session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE {column} = :value"),
                    {"value": value},
                )
            ).scalar_one()
        )


@pytest.mark.asyncio
async def test_deactivate_and_re_signin_creates_fresh_player() -> None:
    """After deactivation, a subsequent sign-in with verificationToken creates a new account."""
    import base64

    import jwt

    dev_token_id = str(uuid.uuid4())[:8]
    encoded_iss = base64.b64encode(f"com.dena.test:{dev_token_id}".encode()).decode()
    vt = jwt.encode(
        {"iss": encoded_iss, "aud": "https://danmakujp4-v1.lcx.tokyo"},
        "dummy-secret-at-least-32-bytes-long-for-hmac",
        algorithm="HS256",
    )

    app = create_lcx_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
        # Step 1: Initial sign-in with verificationToken
        sign_in_1 = await client.post("/identity/sign-in", json={"verificationToken": vt})
        assert sign_in_1.status_code == 200
        data_1 = sign_in_1.json()
        user_1_id = data_1["user"]["id"]
        token_1 = data_1["accessToken"]

        # Step 2: Delete account
        del_res = await client.post(
            "/identity/deactivate-user",
            json=_BODY,
            headers={"Authorization": f"Bearer {token_1}"},
        )
        assert del_res.status_code == 200

        # Step 3: Same device signs in again after deletion
        sign_in_2 = await client.post("/identity/sign-in", json={"verificationToken": vt})
        assert sign_in_2.status_code == 200
        data_2 = sign_in_2.json()
        user_2_id = data_2["user"]["id"]

        # Must be a brand new user, not the deleted user
        assert user_2_id != user_1_id


@pytest.mark.asyncio
async def test_deactivate_user_does_not_affect_other_players() -> None:
    """Deleting User A must strictly isolate and leave User B completely intact."""
    app = create_lcx_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
        # Create Player A
        sign_in_a = await client.post(
            "/identity/sign-in", headers={"deviceAccount": f"user-a-{uuid.uuid4()}"}
        )
        assert sign_in_a.status_code == 200
        body_a = sign_in_a.json()
        user_a = body_a["user"]["id"]
        token_a = body_a["accessToken"]

        # Create Player B
        sign_in_b = await client.post(
            "/identity/sign-in", headers={"deviceAccount": f"user-b-{uuid.uuid4()}"}
        )
        assert sign_in_b.status_code == 200
        user_b = sign_in_b.json()["user"]["id"]

        # Verify both exist before deletion
        p_a, count_a = await _player_and_storage_count(user_a)
        p_b, count_b = await _player_and_storage_count(user_b)
        assert p_a is not None and count_a > 0
        assert p_b is not None and count_b > 0
        b_player_id = p_b.id

        # Player A deletes their account
        del_res = await client.post(
            "/identity/deactivate-user",
            json=_BODY,
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert del_res.status_code == 200

        # Verify Player A is gone
        p_a_after, count_a_after = await _player_and_storage_count(user_a)
        assert p_a_after is None
        assert count_a_after == 0

        # Verify Player B is 100% intact and untouched
        p_b_after, count_b_after = await _player_and_storage_count(user_b)
        assert p_b_after is not None
        assert p_b_after.id == b_player_id
        assert p_b_after.user_id == user_b
        assert count_b_after == count_b
