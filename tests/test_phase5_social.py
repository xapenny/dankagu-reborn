"""End-to-end tests for Phase 5 Social, MySpace, and Photo Album services."""

import time
import uuid

import pytest

from dankagu.core.database import get_sessionmaker, init_db
from dankagu.core.session import reset_current_player, set_current_player
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_storage import (
    v2_pb2 as storage_v2_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    friend_pb2,
    like_published_photo_pb2,
    my_album_pb2,
    my_space_pb2,
    player_pb2,
    published_photo_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.resource.friend import (
    v1_pb2 as friend_v1_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.resource.published_photo import (
    v1_pb2 as published_photo_v1_pb2,
)
from dankagu.models.player import Player, allocate_short_id
from dankagu.models.storage import PlayerStorageEntry
from dankagu.services.fes_player import FesPlayerService
from dankagu.services.friend import FriendService
from dankagu.services.my_space import MySpaceService
from dankagu.services.photo_album import (
    LikePublishedPhotoService,
    MyAlbumService,
    PublishedPhotoService,
)


class DummyContext:
    async def abort(self, code: object, details: str) -> None:
        raise RuntimeError(f"Aborted: {code} - {details}")


@pytest.mark.asyncio
async def test_phase5_social_and_fes_player_flow() -> None:
    """Verify FesPlayer.GetV1 and Friend follow/search/mutual lifecycle."""
    await init_db()
    now = int(time.time())
    p1_id = f"p5-alice-{uuid.uuid4().hex[:8]}"
    p2_id = f"p5-bob-{uuid.uuid4().hex[:8]}"

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        async with session.begin():
            s1 = await allocate_short_id(session)
            s2 = await allocate_short_id(session)
            session.add_all(
                [
                    Player(
                        id=p1_id,
                        user_id=f"u-{p1_id}",
                        short_id=s1,
                        nickname="ReimuFan",
                        level=15,
                        last_login_at=now - 60,
                    ),
                    Player(
                        id=p2_id,
                        user_id=f"u-{p2_id}",
                        short_id=s2,
                        nickname="MarisaMain",
                        level=25,
                        last_login_at=now,
                    ),
                    # PlayerSearchByPlayerShortIDV1 only publishes accounts that
                    # finished the tutorial, which the client marks by writing
                    # PlayerBase--1. Without it Bob would be hidden from search.
                    PlayerStorageEntry(
                        player_id=p2_id,
                        key="PlayerBase--1",
                        value=b"\x08\x01\x10\x02",
                        created_at=now,
                        updated_at=now,
                    ),
                    PlayerStorageEntry(
                        player_id=p2_id,
                        key="PlayerProfile--1",
                        value=b"\x08\x01\x10\x02",
                        created_at=now,
                        updated_at=now,
                    ),
                    PlayerStorageEntry(
                        player_id=p2_id,
                        key="CardStatus--101",
                        value=b"\x08\x65\x10\x01",
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )

    friend_svc = FriendService()
    fes_player_svc = FesPlayerService()
    ctx = DummyContext()

    tokens = set_current_player(p1_id)
    try:
        # 1. Search Bob by his 8-digit short_id
        search_req = friend_pb2.FriendSearchPlayerByPlayerShortIDV1.Request(
            player_short_id=s2,
            player_storage_keys=["PlayerProfile--1"],
        )
        search_resp = await friend_svc.SearchPlayerByPlayerShortIDV1(search_req, ctx)
        assert search_resp.player.player_id == p2_id
        assert search_resp.player.nickname == "MarisaMain"
        assert search_resp.player.is_following is False
        assert len(search_resp.player.player_storage_entries) == 1
        assert search_resp.player.player_storage_entries[0].key == "PlayerProfile--1"

        # 2. Alice follows Bob
        await friend_svc.FollowV1(friend_pb2.FriendFollowV1.Request(player_id=p2_id), ctx)

        # 3. Verify FollowStatus and FesPlayer.GetV1
        status_resp = await friend_svc.GetFollowStatusesV1(
            friend_pb2.FriendGetFollowStatusesV1.Request(player_ids=[p2_id]),
            ctx,
        )
        assert len(status_resp.follow_statuses) == 1
        assert status_resp.follow_statuses[0].is_following is True
        assert status_resp.follow_statuses[0].is_followed is False

        fes_req = player_pb2.FesPlayerGetPlayerServiceV1.Request(player_id=p2_id)
        c1 = fes_req.criteria.add()
        c1.key = "CardStatus--"
        c1.matching_type = storage_v2_pb2.Criterion.MatchingType.FORWARD
        fes_resp = await fes_player_svc.GetV1(fes_req, ctx)
        assert fes_resp.player.player_id == p2_id
        assert fes_resp.player.player_short_id == s2
        assert fes_resp.player.nickname == "MarisaMain"
        assert fes_resp.player.is_following is True
        assert fes_resp.player.is_followed is False
        assert len(fes_resp.player.player_storage_entries) == 1
        assert fes_resp.player.player_storage_entries[0].key == "CardStatus--101"

        # 4. Check Alice's followings
        followings_resp = await friend_svc.GetMyFollowingsV1(
            friend_pb2.FriendGetMyFollowingsV1.Request(max_results=10),
            ctx,
        )
        assert any(fp.player_id == p2_id for fp in followings_resp.players)
    finally:
        reset_current_player(tokens)

    # 5. Switch to Bob and verify followers + mutual follow
    tokens_b = set_current_player(p2_id)
    try:
        followers_resp = await friend_svc.GetMyFollowersV1(
            friend_pb2.FriendGetMyFollowersV1.Request(max_results=10),
            ctx,
        )
        assert any(
            fp.player_id == p1_id and fp.is_followed is True and fp.is_following is False
            for fp in followers_resp.players
        )

        # Bob follows Alice back -> mutual!
        await friend_svc.FollowV1(friend_pb2.FriendFollowV1.Request(player_id=p1_id), ctx)
        mutual_req = friend_pb2.FriendGetMyFollowersV1.Request(max_results=10)
        mutual_req.criterion.filter.follow = friend_v1_pb2.Criterion.Filter.Follow.MUTUAL
        mutual_resp = await friend_svc.GetMyFollowersV1(mutual_req, ctx)
        assert any(fp.player_id == p1_id and fp.is_following is True for fp in mutual_resp.players)

        # Unfollow and remove follower
        await friend_svc.UnfollowV1(friend_pb2.FriendUnfollowV1.Request(player_id=p1_id), ctx)
        await friend_svc.RemoveMyFollowerV1(
            friend_pb2.FriendRemoveMyFollowerV1.Request(player_id=p1_id), ctx
        )
        after_resp = await friend_svc.GetMyFollowersV1(
            friend_pb2.FriendGetMyFollowersV1.Request(max_results=10),
            ctx,
        )
        assert not any(fp.player_id == p1_id for fp in after_resp.players)
    finally:
        reset_current_player(tokens_b)


@pytest.mark.asyncio
async def test_phase5_myspace_and_photo_album_flow() -> None:
    """Verify MySpace likes and MyAlbum / PublishedPhoto / LikePublishedPhoto lifecycle."""
    await init_db()
    now = int(time.time())
    p1_id = f"p5-creator-{uuid.uuid4().hex[:8]}"
    p2_id = f"p5-visitor-{uuid.uuid4().hex[:8]}"

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        async with session.begin():
            session.add_all(
                [
                    Player(
                        id=p1_id,
                        user_id=f"u-{p1_id}",
                        short_id=await allocate_short_id(session),
                        nickname="SakuyaSpace",
                        last_login_at=now,
                    ),
                    Player(
                        id=p2_id,
                        user_id=f"u-{p2_id}",
                        short_id=await allocate_short_id(session),
                        nickname="RemiliaVisitor",
                        last_login_at=now,
                    ),
                ]
            )

    myspace_svc = MySpaceService()
    album_svc = MyAlbumService()
    pub_svc = PublishedPhotoService()
    like_photo_svc = LikePublishedPhotoService()
    ctx = DummyContext()

    # 1. Visitor draws a player and likes Creator's MySpace
    tokens_v = set_current_player(p2_id)
    try:
        draw_resp = await myspace_svc.DrawPlayerV1(
            my_space_pb2.MySpaceDrawPlayerV1.Request(), ctx
        )
        assert draw_resp.player_id != ""

        status_before = await myspace_svc.GetPlayerLikeStatusV1(
            my_space_pb2.MySpaceGetPlayerLikeStatusV1.Request(player_id=p1_id), ctx
        )
        assert status_before.is_liked is False

        await myspace_svc.LikeV1(my_space_pb2.MySpaceLikeV1.Request(player_id=p1_id), ctx)

        status_after = await myspace_svc.GetPlayerLikeStatusV1(
            my_space_pb2.MySpaceGetPlayerLikeStatusV1.Request(player_id=p1_id), ctx
        )
        assert status_after.is_liked is True

        sent_hist = await myspace_svc.GetSentLikesHistoryV1(
            my_space_pb2.MySpaceGetSentLikesHistoryV1.Request(max_results=10), ctx
        )
        assert len(sent_hist.likes) == 1
        assert sent_hist.likes[0].player.player_id == p1_id
        assert sent_hist.likes[0].player.nickname == "SakuyaSpace"
    finally:
        reset_current_player(tokens_v)

    # 2. Creator checks received MySpace likes, prepares & publishes a photo
    tokens_c = set_current_player(p1_id)
    try:
        recv_hist = await myspace_svc.GetReceivedLikesHistoryV1(
            my_space_pb2.MySpaceGetReceivedLikesHistoryV1.Request(max_results=10), ctx
        )
        assert len(recv_hist.likes) == 1
        assert recv_hist.likes[0].player.player_id == p2_id
        assert recv_hist.likes[0].player.nickname == "RemiliaVisitor"

        prep_req = my_album_pb2.MyAlbumPrepareV1.Request(
            character_ids=["1001", "1002"],
            category="HAKONIWA",
            content_value=b"photo-meta-bytes",
        )
        prep_req.created_at.seconds = now
        prep_resp = await album_svc.PrepareV1(prep_req, ctx)
        photo_id = prep_resp.photo.photo_id
        assert photo_id != ""
        assert prep_resp.signed_url_for_origin.endswith(f"{photo_id}_orig.png")

        await album_svc.NotifyUploadedV1(
            my_album_pb2.MyAlbumNotifyUploadedV1.Request(photo_id=photo_id), ctx
        )

        album_get = await album_svc.GetV1(my_album_pb2.MyAlbumGetV1.Request(), ctx)
        assert len(album_get.photos) == 1
        assert album_get.photos[0].photo_id == photo_id

        pub_resp = await pub_svc.PublishV1(
            published_photo_pb2.PublishedPhotoPublishV1.Request(
                photo_ids=[photo_id],
                privacy_type=published_photo_v1_pb2.PrivacyType.PUBLIC,
            ),
            ctx,
        )
        assert len(pub_resp.photos) == 1
        assert pub_resp.photos[0].photo_id == photo_id
        assert pub_resp.photos[0].player_id == p1_id
    finally:
        reset_current_player(tokens_c)

    # 3. Visitor browses PublishedPhoto variety, likes the photo, and checks GetLikedV1
    tokens_v = set_current_player(p2_id)
    try:
        var_resp = await pub_svc.GetVarietyV1(
            published_photo_pb2.PublishedPhotoGetVarietyV1.Request(
                num_latest_photos=10,
                num_latest_first_photos=10,
            ),
            ctx,
        )
        assert any(p.photo_id == photo_id for p in var_resp.latest_photos)
        assert any(p.photo_id == photo_id for p in var_resp.latest_first_photos)

        await like_photo_svc.LikeV1(
            like_published_photo_pb2.LikePublishedPhotoLikeV1.Request(photo_id=photo_id),
            ctx,
        )

        liked_resp = await pub_svc.GetLikedV1(
            published_photo_pb2.PublishedPhotoGetLikedV1.Request(player_id=p2_id),
            ctx,
        )
        assert len(liked_resp.photos) == 1
        assert liked_resp.photos[0].photo_id == photo_id
        assert liked_resp.photos[0].like_count == 1

        await like_photo_svc.DeleteLikeV1(
            like_published_photo_pb2.LikePublishedPhotoDeleteLikeV1.Request(photo_id=photo_id),
            ctx,
        )
        by_player_resp = await pub_svc.GetByPlayerIDV1(
            published_photo_pb2.PublishedPhotoGetByPlayerIDV1.Request(player_id=p1_id),
            ctx,
        )
        assert len(by_player_resp.photos) == 1
        assert by_player_resp.photos[0].like_count == 0
    finally:
        reset_current_player(tokens_v)

    # 4. Creator unpublishes and deletes the photo
    tokens_c = set_current_player(p1_id)
    try:
        unpub_resp = await pub_svc.DeleteV1(
            published_photo_pb2.PublishedPhotoDeleteV1.Request(photo_ids=[photo_id]),
            ctx,
        )
        assert list(unpub_resp.deleted_photo_ids) == [photo_id]

        del_resp = await album_svc.DeleteV1(
            my_album_pb2.MyAlbumDeleteV1.Request(photo_ids=[photo_id]),
            ctx,
        )
        assert list(del_resp.deleted_photo_ids) == [photo_id]
    finally:
        reset_current_player(tokens_c)
