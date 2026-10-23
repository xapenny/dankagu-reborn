"""Takasho MySpace servicer for personal space visits, weighted drawing, and likes."""

import base64
import logging
import random
import time

import grpc
from sqlalchemy import and_, or_, select

from dankagu.core.database import get_sessionmaker
from dankagu.core.session import get_current_player_id
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    my_space_pb2,
    my_space_pb2_grpc,
)
from dankagu.models.player import Player
from dankagu.models.social import FriendRelation, MySpaceLikeRecord
from dankagu.models.storage import PlayerStorageEntry
from dankagu.services.fes_player import query_multi_player_storage_by_keys
from dankagu.services.system import get_jst_game_day

logger = logging.getLogger("dankagu.services.my_space")


def _encode_cursor(liked_at: int, record_id: int) -> str:
    """Encode Keyset cursor to base64 string."""
    return base64.b64encode(f"{liked_at}:{record_id}".encode("ascii")).decode("ascii")


def _decode_cursor(token: str | None) -> tuple[int, int] | None:
    """Decode base64 Keyset cursor to (liked_at, record_id)."""
    if not token:
        return None
    try:
        raw = base64.b64decode(token.encode("ascii")).decode("ascii")
        parts = raw.split(":", 1)
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return None


class MySpaceService(my_space_pb2_grpc.MySpaceServicer):
    """Servicer for MySpace player drawing, liking, and like histories."""

    async def DrawPlayerV1(
        self,
        request: my_space_pb2.MySpaceDrawPlayerV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> my_space_pb2.MySpaceDrawPlayerV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        logger.info("🏠 [MySpace] DrawPlayerV1 (player=%s)", current_pid)
        response = my_space_pb2.MySpaceDrawPlayerV1.Response()

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # Only consider players who have unlocked/initialized their space (MySpotStatus--1 exists)
            stmt = (
                select(Player)
                .join(PlayerStorageEntry, PlayerStorageEntry.player_id == Player.id)
                .where(
                    Player.id != current_pid,
                    PlayerStorageEntry.key == "MySpotStatus--1",
                )
                .distinct()
            )
            candidates = list((await session.execute(stmt)).scalars().all())

            if candidates:
                # Query social relationship for weighted sampling
                rel_stmt = select(FriendRelation).where(
                    or_(
                        FriendRelation.follower_id == current_pid,
                        FriendRelation.target_id == current_pid,
                    )
                )
                relations = list((await session.execute(rel_stmt)).scalars().all())
                following_ids = {r.target_id for r in relations if r.follower_id == current_pid}
                follower_ids = {r.follower_id for r in relations if r.target_id == current_pid}
                mutual_ids = following_ids & follower_ids

                weights: list[int] = []
                for c in candidates:
                    if c.id in mutual_ids:
                        weights.append(50)  # Mutual follow
                    elif c.id in following_ids:
                        weights.append(25)  # Current player follows candidate
                    elif c.id in follower_ids:
                        weights.append(15)  # Candidate follows current player
                    else:
                        weights.append(10)  # Stranger

                chosen = random.choices(candidates, weights=weights, k=1)[0]
                response.player_id = chosen.id
                response.nickname = chosen.nickname or chosen.name or "DanKagu"
            else:
                # Fallback to self (visit self's space in read-only visit mode)
                self_player = await session.get(Player, current_pid)
                response.player_id = current_pid
                response.nickname = (
                    (self_player.nickname or self_player.name or "DanKagu")
                    if self_player
                    else "DanKagu"
                )

        return response

    async def LikeV1(
        self,
        request: my_space_pb2.MySpaceLikeV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> my_space_pb2.MySpaceLikeV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        target_pid = request.player_id or current_pid
        now = int(time.time())
        today_jst = get_jst_game_day(now)
        logger.info(
            "🏠 [MySpace] LikeV1 (from=%s, to=%s, jst_day=%d)", current_pid, target_pid, today_jst
        )

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # Check if already liked on the current JST game day (04:00 reset)
            stmt = (
                select(MySpaceLikeRecord)
                .where(
                    MySpaceLikeRecord.from_player_id == current_pid,
                    MySpaceLikeRecord.to_player_id == target_pid,
                )
                .order_by(MySpaceLikeRecord.liked_at.desc(), MySpaceLikeRecord.id.desc())
                .limit(1)
            )
            latest = (await session.execute(stmt)).scalars().first()

            if latest is None or get_jst_game_day(latest.liked_at) < today_jst:
                record = MySpaceLikeRecord(
                    from_player_id=current_pid,
                    to_player_id=target_pid,
                    liked_at=now,
                )
                session.add(record)
                await session.commit()
            else:
                logger.info("🏠 [MySpace] LikeV1 idempotent hit (already liked today)")

        response = my_space_pb2.MySpaceLikeV1.Response()
        response.player_id = target_pid
        return response

    async def GetPlayerLikeStatusV1(
        self,
        request: my_space_pb2.MySpaceGetPlayerLikeStatusV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> my_space_pb2.MySpaceGetPlayerLikeStatusV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        target_pid = request.player_id
        now = int(time.time())
        today_jst = get_jst_game_day(now)
        logger.info(
            "🏠 [MySpace] GetPlayerLikeStatusV1 (from=%s, to=%s, jst_day=%d)",
            current_pid,
            target_pid,
            today_jst,
        )

        is_liked = False
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = (
                select(MySpaceLikeRecord)
                .where(
                    MySpaceLikeRecord.from_player_id == current_pid,
                    MySpaceLikeRecord.to_player_id == target_pid,
                )
                .order_by(MySpaceLikeRecord.liked_at.desc(), MySpaceLikeRecord.id.desc())
                .limit(1)
            )
            latest = (await session.execute(stmt)).scalars().first()
            if latest is not None and get_jst_game_day(latest.liked_at) >= today_jst:
                is_liked = True

        response = my_space_pb2.MySpaceGetPlayerLikeStatusV1.Response()
        response.player_id = target_pid
        response.is_liked = is_liked
        return response

    async def GetSentLikesHistoryV1(
        self,
        request: my_space_pb2.MySpaceGetSentLikesHistoryV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> my_space_pb2.MySpaceGetSentLikesHistoryV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        limit = min(max(int(request.max_results), 1), 50) if request.max_results > 0 else 50
        storage_keys = list(request.player_storage_keys)
        cursor = _decode_cursor(request.page_token)
        logger.info(
            "🏠 [MySpace] GetSentLikesHistoryV1 (player=%s, limit=%d, cursor=%s)",
            current_pid,
            limit,
            cursor,
        )

        response = my_space_pb2.MySpaceGetSentLikesHistoryV1.Response()
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            conditions = [MySpaceLikeRecord.from_player_id == current_pid]
            if cursor is not None:
                cursor_time, cursor_id = cursor
                conditions.append(
                    or_(
                        MySpaceLikeRecord.liked_at < cursor_time,
                        and_(
                            MySpaceLikeRecord.liked_at == cursor_time,
                            MySpaceLikeRecord.id < cursor_id,
                        ),
                    )
                )

            stmt = (
                select(MySpaceLikeRecord)
                .where(*conditions)
                .order_by(MySpaceLikeRecord.liked_at.desc(), MySpaceLikeRecord.id.desc())
                .limit(limit + 1)
            )
            records = list((await session.execute(stmt)).scalars().all())

            if len(records) > limit:
                last_included = records[limit - 1]
                response.next_page_token = _encode_cursor(last_included.liked_at, last_included.id)
                records = records[:limit]

            target_pids = list({rec.to_player_id for rec in records})
            players_by_id: dict[str, Player] = {}
            storage_by_pid: dict[str, list[PlayerStorageEntry]] = {}
            if target_pids:
                p_res = await session.execute(select(Player).where(Player.id.in_(target_pids)))
                players_by_id = {p.id: p for p in p_res.scalars().all()}
                storage_by_pid = await query_multi_player_storage_by_keys(
                    session, target_pids, storage_keys
                )

            for rec in records:
                like_item = response.likes.add()
                like_item.liked_at = rec.liked_at
                target = players_by_id.get(rec.to_player_id)
                like_item.player.player_id = rec.to_player_id
                like_item.player.nickname = (
                    (target.nickname or target.name or "DanKagu") if target else "DanKagu"
                )
                for row in storage_by_pid.get(rec.to_player_id, []):
                    e = like_item.player.player_storage_entries.add()
                    e.player_id = row.player_id
                    e.key = row.key
                    e.value = row.value
                    e.created_at = row.created_at
                    e.updated_at = row.updated_at

        return response

    async def GetReceivedLikesHistoryV1(
        self,
        request: my_space_pb2.MySpaceGetReceivedLikesHistoryV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> my_space_pb2.MySpaceGetReceivedLikesHistoryV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        limit = min(max(int(request.max_results), 1), 50) if request.max_results > 0 else 50
        storage_keys = list(request.player_storage_keys)
        cursor = _decode_cursor(request.page_token)
        logger.info(
            "🏠 [MySpace] GetReceivedLikesHistoryV1 (player=%s, limit=%d, cursor=%s)",
            current_pid,
            limit,
            cursor,
        )

        response = my_space_pb2.MySpaceGetReceivedLikesHistoryV1.Response()
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            conditions = [MySpaceLikeRecord.to_player_id == current_pid]
            if cursor is not None:
                cursor_time, cursor_id = cursor
                conditions.append(
                    or_(
                        MySpaceLikeRecord.liked_at < cursor_time,
                        and_(
                            MySpaceLikeRecord.liked_at == cursor_time,
                            MySpaceLikeRecord.id < cursor_id,
                        ),
                    )
                )

            stmt = (
                select(MySpaceLikeRecord)
                .where(*conditions)
                .order_by(MySpaceLikeRecord.liked_at.desc(), MySpaceLikeRecord.id.desc())
                .limit(limit + 1)
            )
            records = list((await session.execute(stmt)).scalars().all())

            if len(records) > limit:
                last_included = records[limit - 1]
                response.next_page_token = _encode_cursor(last_included.liked_at, last_included.id)
                records = records[:limit]

            sender_pids = list({rec.from_player_id for rec in records})
            players_by_id: dict[str, Player] = {}
            storage_by_pid: dict[str, list[PlayerStorageEntry]] = {}
            if sender_pids:
                p_res = await session.execute(select(Player).where(Player.id.in_(sender_pids)))
                players_by_id = {p.id: p for p in p_res.scalars().all()}
                storage_by_pid = await query_multi_player_storage_by_keys(
                    session, sender_pids, storage_keys
                )

            for rec in records:
                like_item = response.likes.add()
                like_item.liked_at = rec.liked_at
                sender = players_by_id.get(rec.from_player_id)
                like_item.player.player_id = rec.from_player_id
                like_item.player.nickname = (
                    (sender.nickname or sender.name or "DanKagu") if sender else "DanKagu"
                )
                for row in storage_by_pid.get(rec.from_player_id, []):
                    e = like_item.player.player_storage_entries.add()
                    e.player_id = row.player_id
                    e.key = row.key
                    e.value = row.value
                    e.created_at = row.created_at
                    e.updated_at = row.updated_at

        return response


def register_my_space_servicer(server: grpc.aio.Server) -> None:
    servicer = MySpaceService()
    method_handlers = {
        "DrawPlayerV1": takasho_unary_handler(
            servicer.DrawPlayerV1,
            my_space_pb2.MySpaceDrawPlayerV1.Request,
            my_space_pb2.MySpaceDrawPlayerV1.Response,
        ),
        "LikeV1": takasho_unary_handler(
            servicer.LikeV1,
            my_space_pb2.MySpaceLikeV1.Request,
            my_space_pb2.MySpaceLikeV1.Response,
        ),
        "GetSentLikesHistoryV1": takasho_unary_handler(
            servicer.GetSentLikesHistoryV1,
            my_space_pb2.MySpaceGetSentLikesHistoryV1.Request,
            my_space_pb2.MySpaceGetSentLikesHistoryV1.Response,
        ),
        "GetReceivedLikesHistoryV1": takasho_unary_handler(
            servicer.GetReceivedLikesHistoryV1,
            my_space_pb2.MySpaceGetReceivedLikesHistoryV1.Request,
            my_space_pb2.MySpaceGetReceivedLikesHistoryV1.Response,
        ),
        "GetPlayerLikeStatusV1": takasho_unary_handler(
            servicer.GetPlayerLikeStatusV1,
            my_space_pb2.MySpaceGetPlayerLikeStatusV1.Request,
            my_space_pb2.MySpaceGetPlayerLikeStatusV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.fes.player_api.MySpace", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
