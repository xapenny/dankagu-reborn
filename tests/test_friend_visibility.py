"""Friend follow/follower visibility and player search gating.

Regression coverage for two live bugs:

1. Following someone could be stored under the 8-digit プレイヤーID instead of
   ``players.id``, after which *neither* list showed the relation and the follow
   status stayed false.
2. The 検索 tab listed accounts that had never finished the tutorial, and
   published the DB default name ("Player") as a nickname.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncGenerator

import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.core.database import get_engine, get_sessionmaker
from dankagu.core.session import reset_current_player, set_current_player
from dankagu.grpc.generated.takasho.schema.fes.player_api import friend_pb2
from dankagu.grpc.generated.takasho.schema.fes.resource.friend import v1_pb2 as friend_v1_pb2
from dankagu.models.player import Player
from dankagu.models.social import FriendRelation
from dankagu.models.storage import PlayerStorageEntry
from dankagu.services.friend import FriendService

# Synthetic blobs on purpose. The service only requires a *non-empty* profile
# entry (see ``_public_profile_clause``), so bytes lifted from a real player's
# save have no business in this public repository.
#   PROFILE_VALUE       -> {1: 1, 2: 1, 3: "world"}
#   PUBLIC_STATUS_VALUE -> {1: 1, 2: "world"}
PROFILE_VALUE = bytes.fromhex("080110011a05776f726c64")
PUBLIC_STATUS_VALUE = bytes.fromhex("08011205776f726c64")

KEYS = ["PlayerBase--1", "PlayerStatus--1", "PlayerPublicStatus--1"]


@contextlib.contextmanager
def as_player(player_id: str):
    """Run the servicer as if the request came from ``player_id``'s session."""
    tokens = set_current_player(player_id, f"test-token-{player_id}")
    try:
        yield
    finally:
        reset_current_player(tokens)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_sessionmaker()
    async with factory() as db:
        yield db


@pytest_asyncio.fixture(autouse=True)
async def clean_friend_relations() -> AsyncGenerator[None, None]:
    """Delete the relation rows a test created, by row id.

    ``tests/conftest.py`` removes rows keyed by the *player ids* of the players a
    test created. Some tests here deliberately write legacy rows under a raw
    ``short_id``, which that filter cannot see, so they are tracked by id instead.
    """
    factory = get_sessionmaker()
    async with factory() as db:
        before = set((await db.execute(select(FriendRelation.id))).scalars().all())
    yield
    async with factory() as db:
        after = set((await db.execute(select(FriendRelation.id))).scalars().all())
        for relation_id in after - before:
            await db.execute(delete(FriendRelation).where(FriendRelation.id == relation_id))
        await db.commit()


@pytest_asyncio.fixture
async def without_friend_foreign_keys() -> AsyncGenerator[None, None]:
    """Allow planting rows the way a pre-``0002`` database could contain them.

    The ``ON DELETE CASCADE`` foreign keys arrive with migration
    ``0002_friend_relations_normalize``, so only a database that predates it (or
    one running with migrations disabled) can hold a raw ``short_id`` reference.
    A freshly created database already has them from the ORM metadata, so they are
    dropped for the test and restored the way the migration does afterwards.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        # Fail fast instead of hanging if another session still holds a lock.
        await conn.execute(text("SET lock_timeout = '10s'"))
        for name in ("fk_friend_follower", "fk_friend_target"):
            await conn.execute(
                text(f"ALTER TABLE friend_relations DROP CONSTRAINT IF EXISTS {name}")
            )
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET lock_timeout = '10s'"))
            # A row planted by the test would block the constraint, exactly like
            # the migration's orphan pass removes them.
            await conn.execute(
                text(
                    "DELETE FROM friend_relations r"
                    " WHERE NOT EXISTS (SELECT 1 FROM players p WHERE p.id = r.follower_id)"
                    "    OR NOT EXISTS (SELECT 1 FROM players p WHERE p.id = r.target_id)"
                )
            )
            for name, column in (
                ("fk_friend_follower", "follower_id"),
                ("fk_friend_target", "target_id"),
            ):
                present = (
                    await conn.execute(
                        text("SELECT 1 FROM pg_constraint WHERE conname = :name"), {"name": name}
                    )
                ).scalar()
                if present:
                    continue
                await conn.execute(
                    text(
                        f"ALTER TABLE friend_relations ADD CONSTRAINT {name}"
                        f" FOREIGN KEY ({column}) REFERENCES players(id)"
                        " ON DELETE CASCADE NOT VALID"
                    )
                )
                await conn.execute(text(f"ALTER TABLE friend_relations VALIDATE CONSTRAINT {name}"))


async def make_player(
    db: AsyncSession,
    *,
    short_id: str,
    nickname: str = "",
    name: str = "Player",
    last_login_at: int | None = None,
    profile: bool = True,
    banned: bool = False,
) -> Player:
    """Create a player, optionally with the tutorial profile a real account has."""
    now = last_login_at or int(time.time())
    player = Player(
        user_id=f"friend-test-{short_id}",
        device_account=f"friend-test-dev-{short_id}",
        short_id=short_id,
        name=name,
        nickname=nickname,
        registered_at=now,
        last_login_at=now,
        is_banned=banned,
    )
    db.add(player)
    await db.flush()
    if profile:
        db.add_all(
            [
                PlayerStorageEntry(
                    player_id=player.id,
                    key="PlayerBase--1",
                    value=PROFILE_VALUE,
                    created_at=now,
                    updated_at=now,
                ),
                PlayerStorageEntry(
                    player_id=player.id,
                    key="PlayerPublicStatus--1",
                    value=PUBLIC_STATUS_VALUE,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
    await db.commit()
    return player


def follower_request(**filter_kwargs: int) -> friend_pb2.FriendGetMyFollowersV1.Request:
    request = friend_pb2.FriendGetMyFollowersV1.Request(max_results=100)
    request.player_storage_keys.extend(KEYS)
    request.criterion.order_field = friend_v1_pb2.Criterion.OrderField.LAST_LOGGED_IN_AT
    request.criterion.order_direction = friend_v1_pb2.Criterion.OrderDirection.DESC
    request.criterion.filter.SetInParent()
    for key, value in filter_kwargs.items():
        setattr(request.criterion.filter, key, value)
    return request


def following_request(**filter_kwargs: int) -> friend_pb2.FriendGetMyFollowingsV1.Request:
    request = friend_pb2.FriendGetMyFollowingsV1.Request(max_results=100)
    request.player_storage_keys.extend(KEYS)
    request.criterion.order_field = friend_v1_pb2.Criterion.OrderField.LAST_LOGGED_IN_AT
    request.criterion.order_direction = friend_v1_pb2.Criterion.OrderDirection.DESC
    request.criterion.filter.SetInParent()
    for key, value in filter_kwargs.items():
        setattr(request.criterion.filter, key, value)
    return request


async def relations(db: AsyncSession) -> list[FriendRelation]:
    return list((await db.execute(select(FriendRelation))).scalars().all())


class TestFollowNormalisation:
    """FollowV1 must persist players.id, whatever form the client sent."""

    async def test_follow_by_player_id_is_visible(self, session: AsyncSession) -> None:
        alice = await make_player(session, short_id="70000001", nickname="Alice")
        bob = await make_player(session, short_id="70000002", nickname="Bob")

        with as_player(alice.id):
            await FriendService().FollowV1(
                friend_pb2.FriendFollowV1.Request(player_id=bob.id), None
            )

        with as_player(bob.id):
            followers = await FriendService().GetMyFollowersV1(follower_request(), None)
        assert [p.player_id for p in followers.players] == [alice.id]

    async def test_follow_by_short_id_is_normalised(self, session: AsyncSession) -> None:
        alice = await make_player(session, short_id="70000011", nickname="Alice")
        bob = await make_player(session, short_id="70000012", nickname="Bob")

        with as_player(alice.id):
            await FriendService().FollowV1(
                friend_pb2.FriendFollowV1.Request(player_id=bob.short_id), None
            )

        # Stored canonicalised, so both directions resolve.
        rows = await relations(session)
        assert [(r.follower_id, r.target_id) for r in rows] == [(alice.id, bob.id)]

        with as_player(bob.id):
            followers = await FriendService().GetMyFollowersV1(follower_request(), None)
        assert [p.player_id for p in followers.players] == [alice.id]
        assert followers.players[0].is_followed is True
        assert followers.players[0].is_following is False

        with as_player(alice.id):
            followings = await FriendService().GetMyFollowingsV1(following_request(), None)
        assert [p.player_id for p in followings.players] == [bob.id]

        with as_player(bob.id):
            statuses = await FriendService().GetFollowStatusesV1(
                friend_pb2.FriendGetFollowStatusesV1.Request(player_ids=[alice.id]), None
            )
        assert [(s.player_id, s.is_following, s.is_followed) for s in statuses.follow_statuses] == [
            (alice.id, False, True)
        ]

    async def test_follow_with_unknown_id_is_ignored(self, session: AsyncSession) -> None:
        alice = await make_player(session, short_id="70000021", nickname="Alice")

        with as_player(alice.id):
            await FriendService().FollowV1(
                friend_pb2.FriendFollowV1.Request(player_id="99999999"), None
            )
            await FriendService().FollowV1(friend_pb2.FriendFollowV1.Request(player_id=""), None)

        assert await relations(session) == []

    async def test_follow_self_is_ignored(self, session: AsyncSession) -> None:
        alice = await make_player(session, short_id="70000031", nickname="Alice")

        with as_player(alice.id):
            await FriendService().FollowV1(
                friend_pb2.FriendFollowV1.Request(player_id=alice.id), None
            )

        assert await relations(session) == []

    async def test_unfollow_removes_legacy_short_id_rows(
        self, session: AsyncSession, without_friend_foreign_keys: None
    ) -> None:
        alice = await make_player(session, short_id="70000041", nickname="Alice")
        bob = await make_player(session, short_id="70000042", nickname="Bob")
        # Row written by an older build: the 8-digit id instead of players.id.
        session.add(FriendRelation(follower_id=alice.id, target_id=bob.short_id, created_at=1))
        await session.commit()

        with as_player(alice.id):
            await FriendService().UnfollowV1(
                friend_pb2.FriendUnfollowV1.Request(player_id=bob.short_id), None
            )

        assert await relations(session) == []

    async def test_remove_my_follower_removes_legacy_rows(
        self, session: AsyncSession, without_friend_foreign_keys: None
    ) -> None:
        alice = await make_player(session, short_id="70000051", nickname="Alice")
        bob = await make_player(session, short_id="70000052", nickname="Bob")
        session.add(
            FriendRelation(follower_id=bob.short_id, target_id=alice.short_id, created_at=1)
        )
        await session.commit()

        with as_player(alice.id):
            await FriendService().RemoveMyFollowerV1(
                friend_pb2.FriendRemoveMyFollowerV1.Request(player_id=bob.short_id), None
            )

        assert await relations(session) == []


class TestLegacyRelationReadability:
    """Pre-existing rows under a raw reference stay visible without a migration."""

    async def test_legacy_short_id_follower_is_listed(
        self, session: AsyncSession, without_friend_foreign_keys: None
    ) -> None:
        alice = await make_player(session, short_id="70000061", nickname="Alice")
        bob = await make_player(session, short_id="70000062", nickname="Bob")
        session.add(
            FriendRelation(follower_id=alice.short_id, target_id=bob.short_id, created_at=1)
        )
        await session.commit()

        with as_player(bob.id):
            followers = await FriendService().GetMyFollowersV1(follower_request(), None)
        assert [p.player_id for p in followers.players] == [alice.id]

        with as_player(alice.id):
            followings = await FriendService().GetMyFollowingsV1(following_request(), None)
        assert [p.player_id for p in followings.players] == [bob.id]


class TestFollowFilterAndLimit:
    async def test_filter_is_applied_before_limit(self, session: AsyncSession) -> None:
        """A mutual follower must survive a small LIMIT even when they sort last."""
        oldest = await make_player(session, short_id="70000071", nickname="Mutual", last_login_at=1)
        newer = await make_player(
            session, short_id="70000072", nickname="Newer", last_login_at=2_000_000_000
        )
        newest = await make_player(
            session, short_id="70000073", nickname="Newest", last_login_at=2_000_000_100
        )

        # everyone follows me; I follow back only the oldest one
        for follower in (oldest, newer, newest):
            session.add(FriendRelation(follower_id=follower.id, target_id=oldest.id, created_at=1))
        session.add(FriendRelation(follower_id=oldest.id, target_id=newest.id, created_at=1))
        await session.commit()

        request = follower_request(follow=friend_v1_pb2.Criterion.Filter.Follow.MUTUAL)
        request.max_results = 2
        with as_player(newest.id):
            followers = await FriendService().GetMyFollowersV1(request, None)

        assert [p.player_id for p in followers.players] == [oldest.id]

    async def test_all_follow_keeps_everyone(self, session: AsyncSession) -> None:
        alice = await make_player(session, short_id="70000081", nickname="Alice")
        bob = await make_player(session, short_id="70000082", nickname="Bob")
        session.add(FriendRelation(follower_id=alice.id, target_id=bob.id, created_at=1))
        await session.commit()

        with as_player(bob.id):
            followers = await FriendService().GetMyFollowersV1(follower_request(), None)
        assert [p.player_id for p in followers.players] == [alice.id]


class TestPlayerSearchGating:
    async def test_search_list_hides_unfinished_and_banned(self, session: AsyncSession) -> None:
        viewer = await make_player(session, short_id="70000091", nickname="Viewer")
        playable = await make_player(session, short_id="70000092", nickname="Playable")
        # no profile: an account that never finished the tutorial
        unfinished = await make_player(session, short_id="70000093", profile=False)
        banned = await make_player(session, short_id="70000094", nickname="Banned", banned=True)

        with as_player(viewer.id):
            result = await FriendService().GetRecentlyLoggedInPlayersV1(
                friend_pb2.FriendGetRecentlyLoggedInPlayersV1.Request(max_results=100), None
            )

        # Scoped to this test's players: the suite shares one database, so
        # unrelated rows must not decide the outcome.
        returned = {p.player_id for p in result.players}
        mine = {viewer.id, playable.id, unfinished.id, banned.id}
        assert returned & mine == {playable.id}

    async def test_search_by_short_id_hides_unfinished(self, session: AsyncSession) -> None:
        viewer = await make_player(session, short_id="70000101", nickname="Viewer")
        # no profile: an account that never finished the tutorial
        unfinished = await make_player(session, short_id="70000102", profile=False)

        with as_player(viewer.id):
            hidden = await FriendService().SearchPlayerByPlayerShortIDV1(
                friend_pb2.FriendSearchPlayerByPlayerShortIDV1.Request(
                    player_short_id=unfinished.short_id, player_storage_keys=KEYS
                ),
                None,
            )
            visible = await FriendService().SearchPlayerByPlayerShortIDV1(
                friend_pb2.FriendSearchPlayerByPlayerShortIDV1.Request(
                    player_short_id=viewer.short_id, player_storage_keys=KEYS
                ),
                None,
            )

        assert hidden.player.player_id != unfinished.id
        assert visible.player.player_id == viewer.id

    async def test_empty_storage_values_do_not_count_as_a_profile(
        self, session: AsyncSession
    ) -> None:
        """Legacy rows with an empty blob must not make an account searchable."""
        viewer = await make_player(session, short_id="70000111", nickname="Viewer")
        empty = await make_player(session, short_id="70000112", profile=False)
        session.add(
            PlayerStorageEntry(
                player_id=empty.id,
                key="PlayerBase--1",
                value=b"",
                created_at=1,
                updated_at=1,
            )
        )
        await session.commit()

        with as_player(viewer.id):
            result = await FriendService().GetRecentlyLoggedInPlayersV1(
                friend_pb2.FriendGetRecentlyLoggedInPlayersV1.Request(max_results=100), None
            )

        returned = {p.player_id for p in result.players}
        assert returned & {viewer.id, empty.id} == set()


class TestNicknameFallback:
    async def test_db_default_name_is_not_published_as_nickname(
        self, session: AsyncSession
    ) -> None:
        """``players.name`` defaults to "Player"; it must never become a nickname."""
        viewer = await make_player(session, short_id="70000121", nickname="Viewer")
        nameless = await make_player(session, short_id="70000122", nickname="", name="Player")
        session.add(FriendRelation(follower_id=nameless.id, target_id=viewer.id, created_at=1))
        await session.commit()

        with as_player(viewer.id):
            followers = await FriendService().GetMyFollowersV1(follower_request(), None)

        assert [p.player_id for p in followers.players] == [nameless.id]
        assert followers.players[0].nickname == ""
