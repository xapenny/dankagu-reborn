"""Takasho Friend servicer with PostgreSQL persistence."""

import logging
import time

import grpc
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.core.database import get_sessionmaker
from dankagu.core.session import get_current_player_id
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    friend_pb2,
    friend_pb2_grpc,
)
from dankagu.grpc.generated.takasho.schema.fes.resource.friend import (
    v1_pb2 as friend_v1_pb2,
)
from dankagu.models.player import Player
from dankagu.models.social import FriendRelation
from dankagu.services.fes_player import query_player_storage_by_keys

logger = logging.getLogger("dankagu.services.friend")


async def _populate_friend_player(
    session: AsyncSession,
    proto_fp: friend_v1_pb2.FriendPlayer,
    target_player: Player,
    is_following: bool,
    is_followed: bool,
    storage_keys: list[str],
) -> None:
    """Populate a FriendPlayer protobuf message from database records."""
    proto_fp.player_id = target_player.id
    proto_fp.nickname = target_player.nickname or target_player.name or "DanKagu"
    proto_fp.is_following = is_following
    proto_fp.is_followed = is_followed
    proto_fp.last_logged_in_at = target_player.last_login_at or int(time.time())

    if storage_keys:
        entries = await query_player_storage_by_keys(session, target_player.id, storage_keys)
        for row in entries:
            entry = proto_fp.player_storage_entries.add()
            entry.player_id = row.player_id
            entry.key = row.key
            entry.value = row.value
            entry.created_at = row.created_at
            entry.updated_at = row.updated_at


class FriendService(friend_pb2_grpc.FriendServicer):
    """Servicer for friends and followers."""

    async def GetMyFollowersV1(
        self,
        request: friend_pb2.FriendGetMyFollowersV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendGetMyFollowersV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        logger.info("👥 [Friend] GetMyFollowersV1 (player=%s)", current_pid)
        response = friend_pb2.FriendGetMyFollowersV1.Response()
        limit = int(request.max_results) if request.max_results > 0 else 50
        storage_keys = list(request.player_storage_keys)

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # Find all players who follow current_pid
            follower_res = await session.execute(
                select(FriendRelation.follower_id).where(FriendRelation.target_id == current_pid)
            )
            follower_ids = list(follower_res.scalars().all())
            if not follower_ids:
                return response

            # Check which of them current_pid follows back
            following_res = await session.execute(
                select(FriendRelation.target_id).where(
                    FriendRelation.follower_id == current_pid,
                    FriendRelation.target_id.in_(follower_ids),
                )
            )
            following_set = set(following_res.scalars().all())

            stmt = select(Player).where(Player.id.in_(follower_ids))
            if (
                request.HasField("criterion")
                and request.criterion.order_field
                == friend_v1_pb2.Criterion.OrderField.PLAYER_LEVEL
            ):
                if request.criterion.order_direction == friend_v1_pb2.Criterion.OrderDirection.ASC:
                    stmt = stmt.order_by(Player.level.asc())
                else:
                    stmt = stmt.order_by(Player.level.desc())
            else:
                if (
                    request.HasField("criterion")
                    and request.criterion.order_direction
                    == friend_v1_pb2.Criterion.OrderDirection.ASC
                ):
                    stmt = stmt.order_by(Player.last_login_at.asc())
                else:
                    stmt = stmt.order_by(Player.last_login_at.desc())

            players = list((await session.execute(stmt.limit(limit))).scalars().all())
            follow_filter = (
                request.criterion.filter.follow
                if request.HasField("criterion") and request.criterion.HasField("filter")
                else friend_v1_pb2.Criterion.Filter.Follow.ALL_FOLLOW
            )

            for p in players:
                is_following = p.id in following_set
                if (
                    follow_filter == friend_v1_pb2.Criterion.Filter.Follow.MUTUAL
                    and not is_following
                ):
                    continue
                if (
                    follow_filter == friend_v1_pb2.Criterion.Filter.Follow.NOT_MUTUAL
                    and is_following
                ):
                    continue
                fp = response.players.add()
                await _populate_friend_player(
                    session,
                    fp,
                    p,
                    is_following=is_following,
                    is_followed=True,
                    storage_keys=storage_keys,
                )

        return response

    async def GetMyFollowingsV1(
        self,
        request: friend_pb2.FriendGetMyFollowingsV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendGetMyFollowingsV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        logger.info("👥 [Friend] GetMyFollowingsV1 (player=%s)", current_pid)
        response = friend_pb2.FriendGetMyFollowingsV1.Response()
        limit = int(request.max_results) if request.max_results > 0 else 50
        storage_keys = list(request.player_storage_keys)

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            following_res = await session.execute(
                select(FriendRelation.target_id).where(FriendRelation.follower_id == current_pid)
            )
            following_ids = list(following_res.scalars().all())
            if not following_ids:
                return response

            followed_res = await session.execute(
                select(FriendRelation.follower_id).where(
                    FriendRelation.target_id == current_pid,
                    FriendRelation.follower_id.in_(following_ids),
                )
            )
            followed_set = set(followed_res.scalars().all())

            stmt = select(Player).where(Player.id.in_(following_ids))
            if (
                request.HasField("criterion")
                and request.criterion.order_field
                == friend_v1_pb2.Criterion.OrderField.PLAYER_LEVEL
            ):
                if request.criterion.order_direction == friend_v1_pb2.Criterion.OrderDirection.ASC:
                    stmt = stmt.order_by(Player.level.asc())
                else:
                    stmt = stmt.order_by(Player.level.desc())
            else:
                if (
                    request.HasField("criterion")
                    and request.criterion.order_direction
                    == friend_v1_pb2.Criterion.OrderDirection.ASC
                ):
                    stmt = stmt.order_by(Player.last_login_at.asc())
                else:
                    stmt = stmt.order_by(Player.last_login_at.desc())

            players = list((await session.execute(stmt.limit(limit))).scalars().all())
            follow_filter = (
                request.criterion.filter.follow
                if request.HasField("criterion") and request.criterion.HasField("filter")
                else friend_v1_pb2.Criterion.Filter.Follow.ALL_FOLLOW
            )

            for p in players:
                is_followed = p.id in followed_set
                if (
                    follow_filter == friend_v1_pb2.Criterion.Filter.Follow.MUTUAL
                    and not is_followed
                ):
                    continue
                if (
                    follow_filter == friend_v1_pb2.Criterion.Filter.Follow.NOT_MUTUAL
                    and is_followed
                ):
                    continue
                fp = response.players.add()
                await _populate_friend_player(
                    session,
                    fp,
                    p,
                    is_following=True,
                    is_followed=is_followed,
                    storage_keys=storage_keys,
                )

        return response

    async def GetRecentlyLoggedInPlayersV1(
        self,
        request: friend_pb2.FriendGetRecentlyLoggedInPlayersV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendGetRecentlyLoggedInPlayersV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        logger.info("👥 [Friend] GetRecentlyLoggedInPlayersV1 (player=%s)", current_pid)
        response = friend_pb2.FriendGetRecentlyLoggedInPlayersV1.Response()
        limit = int(request.max_results) if request.max_results > 0 else 20
        storage_keys = list(request.player_storage_keys)

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = (
                select(Player)
                .where(Player.id != current_pid)
                .order_by(Player.last_login_at.desc())
                .limit(limit)
            )
            players = list((await session.execute(stmt)).scalars().all())
            if not players:
                return response

            pids = [p.id for p in players]
            following_res = await session.execute(
                select(FriendRelation.target_id).where(
                    FriendRelation.follower_id == current_pid,
                    FriendRelation.target_id.in_(pids),
                )
            )
            following_set = set(following_res.scalars().all())

            followed_res = await session.execute(
                select(FriendRelation.follower_id).where(
                    FriendRelation.target_id == current_pid,
                    FriendRelation.follower_id.in_(pids),
                )
            )
            followed_set = set(followed_res.scalars().all())

            for p in players:
                fp = response.players.add()
                await _populate_friend_player(
                    session,
                    fp,
                    p,
                    is_following=(p.id in following_set),
                    is_followed=(p.id in followed_set),
                    storage_keys=storage_keys,
                )

        return response

    async def GetFollowStatusesV1(
        self,
        request: friend_pb2.FriendGetFollowStatusesV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendGetFollowStatusesV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        target_pids = list(request.player_ids)
        logger.info(
            "👥 [Friend] GetFollowStatusesV1 (player=%s, count=%d)",
            current_pid,
            len(target_pids),
        )
        response = friend_pb2.FriendGetFollowStatusesV1.Response()
        if not target_pids:
            return response

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            following_res = await session.execute(
                select(FriendRelation.target_id).where(
                    FriendRelation.follower_id == current_pid,
                    FriendRelation.target_id.in_(target_pids),
                )
            )
            following_set = set(following_res.scalars().all())

            followed_res = await session.execute(
                select(FriendRelation.follower_id).where(
                    FriendRelation.target_id == current_pid,
                    FriendRelation.follower_id.in_(target_pids),
                )
            )
            followed_set = set(followed_res.scalars().all())

            for pid in target_pids:
                st = response.follow_statuses.add()
                st.player_id = pid
                st.is_following = pid in following_set
                st.is_followed = pid in followed_set

        return response

    async def SearchPlayerByPlayerShortIDV1(
        self,
        request: friend_pb2.FriendSearchPlayerByPlayerShortIDV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendSearchPlayerByPlayerShortIDV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        short_id = request.player_short_id.strip()
        logger.info(
            "👥 [Friend] SearchPlayerByPlayerShortIDV1 (player=%s, short_id=%s)",
            current_pid,
            short_id,
        )
        response = friend_pb2.FriendSearchPlayerByPlayerShortIDV1.Response()
        if not short_id:
            return response

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            res = await session.execute(select(Player).where(Player.short_id == short_id))
            target = res.scalars().first()
            if not target:
                return response

            following_res = await session.execute(
                select(FriendRelation.id).where(
                    FriendRelation.follower_id == current_pid,
                    FriendRelation.target_id == target.id,
                )
            )
            followed_res = await session.execute(
                select(FriendRelation.id).where(
                    FriendRelation.follower_id == target.id,
                    FriendRelation.target_id == current_pid,
                )
            )
            await _populate_friend_player(
                session,
                response.player,
                target,
                is_following=(following_res.scalar() is not None),
                is_followed=(followed_res.scalar() is not None),
                storage_keys=list(request.player_storage_keys),
            )

        return response

    async def FollowV1(
        self,
        request: friend_pb2.FriendFollowV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendFollowV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        target_pid = request.player_id
        logger.info("👥 [Friend] FollowV1 (follower=%s, target=%s)", current_pid, target_pid)
        if target_pid and target_pid != current_pid:
            now = int(time.time())
            session_factory = get_sessionmaker()
            async with session_factory() as session:
                stmt = (
                    pg_insert(FriendRelation)
                    .values(
                        follower_id=current_pid,
                        target_id=target_pid,
                        created_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[FriendRelation.follower_id, FriendRelation.target_id]
                    )
                )
                await session.execute(stmt)
                await session.commit()

        return friend_pb2.FriendFollowV1.Response()

    async def UnfollowV1(
        self,
        request: friend_pb2.FriendUnfollowV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendUnfollowV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        target_pid = request.player_id
        logger.info("👥 [Friend] UnfollowV1 (follower=%s, target=%s)", current_pid, target_pid)
        if target_pid:
            session_factory = get_sessionmaker()
            async with session_factory() as session:
                await session.execute(
                    delete(FriendRelation).where(
                        FriendRelation.follower_id == current_pid,
                        FriendRelation.target_id == target_pid,
                    )
                )
                await session.commit()

        return friend_pb2.FriendUnfollowV1.Response()

    async def RemoveMyFollowerV1(
        self,
        request: friend_pb2.FriendRemoveMyFollowerV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendRemoveMyFollowerV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        follower_pid = request.player_id
        logger.info(
            "👥 [Friend] RemoveMyFollowerV1 (player=%s, follower=%s)",
            current_pid,
            follower_pid,
        )
        if follower_pid:
            session_factory = get_sessionmaker()
            async with session_factory() as session:
                await session.execute(
                    delete(FriendRelation).where(
                        FriendRelation.follower_id == follower_pid,
                        FriendRelation.target_id == current_pid,
                    )
                )
                await session.commit()

        return friend_pb2.FriendRemoveMyFollowerV1.Response()


def register_friend_servicer(server: grpc.aio.Server) -> None:
    servicer = FriendService()
    method_handlers = {
        "GetMyFollowersV1": takasho_unary_handler(
            servicer.GetMyFollowersV1,
            friend_pb2.FriendGetMyFollowersV1.Request,
            friend_pb2.FriendGetMyFollowersV1.Response,
        ),
        "GetMyFollowingsV1": takasho_unary_handler(
            servicer.GetMyFollowingsV1,
            friend_pb2.FriendGetMyFollowingsV1.Request,
            friend_pb2.FriendGetMyFollowingsV1.Response,
        ),
        "GetRecentlyLoggedInPlayersV1": takasho_unary_handler(
            servicer.GetRecentlyLoggedInPlayersV1,
            friend_pb2.FriendGetRecentlyLoggedInPlayersV1.Request,
            friend_pb2.FriendGetRecentlyLoggedInPlayersV1.Response,
        ),
        "GetFollowStatusesV1": takasho_unary_handler(
            servicer.GetFollowStatusesV1,
            friend_pb2.FriendGetFollowStatusesV1.Request,
            friend_pb2.FriendGetFollowStatusesV1.Response,
        ),
        "SearchPlayerByPlayerShortIDV1": takasho_unary_handler(
            servicer.SearchPlayerByPlayerShortIDV1,
            friend_pb2.FriendSearchPlayerByPlayerShortIDV1.Request,
            friend_pb2.FriendSearchPlayerByPlayerShortIDV1.Response,
        ),
        "FollowV1": takasho_unary_handler(
            servicer.FollowV1,
            friend_pb2.FriendFollowV1.Request,
            friend_pb2.FriendFollowV1.Response,
        ),
        "UnfollowV1": takasho_unary_handler(
            servicer.UnfollowV1,
            friend_pb2.FriendUnfollowV1.Request,
            friend_pb2.FriendUnfollowV1.Response,
        ),
        "RemoveMyFollowerV1": takasho_unary_handler(
            servicer.RemoveMyFollowerV1,
            friend_pb2.FriendRemoveMyFollowerV1.Request,
            friend_pb2.FriendRemoveMyFollowerV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.fes.player_api.friend.Friend", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
