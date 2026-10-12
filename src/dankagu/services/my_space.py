"""Takasho MySpace servicer for personal space visits and likes."""

import logging
import random
import time

import grpc
from sqlalchemy import select

from dankagu.core.database import get_sessionmaker
from dankagu.core.session import get_current_player_id
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    my_space_pb2,
    my_space_pb2_grpc,
)
from dankagu.models.player import Player
from dankagu.models.social import MySpaceLikeRecord
from dankagu.services.fes_player import query_player_storage_by_keys
from dankagu.services.system import get_jst_game_day

logger = logging.getLogger("dankagu.services.my_space")


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
            res = await session.execute(
                select(Player).where(Player.id != current_pid).limit(50)
            )
            candidates = list(res.scalars().all())
            if candidates:
                chosen = random.choice(candidates)
                response.player_id = chosen.id
                response.nickname = chosen.nickname or chosen.name or "DanKagu"
            else:
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
        logger.info("🏠 [MySpace] LikeV1 (from=%s, to=%s)", current_pid, target_pid)

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            record = MySpaceLikeRecord(
                from_player_id=current_pid,
                to_player_id=target_pid,
                liked_at=now,
            )
            session.add(record)
            await session.commit()

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
            "🏠 [MySpace] GetPlayerLikeStatusV1 (from=%s, to=%s)",
            current_pid,
            target_pid,
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
                .order_by(MySpaceLikeRecord.liked_at.desc())
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
        limit = int(request.max_results) if request.max_results > 0 else 50
        storage_keys = list(request.player_storage_keys)
        logger.info("🏠 [MySpace] GetSentLikesHistoryV1 (player=%s)", current_pid)

        response = my_space_pb2.MySpaceGetSentLikesHistoryV1.Response()
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = (
                select(MySpaceLikeRecord)
                .where(MySpaceLikeRecord.from_player_id == current_pid)
                .order_by(MySpaceLikeRecord.liked_at.desc())
                .limit(limit)
            )
            records = list((await session.execute(stmt)).scalars().all())
            for rec in records:
                like_item = response.likes.add()
                like_item.liked_at = rec.liked_at
                target = await session.get(Player, rec.to_player_id)
                like_item.player.player_id = rec.to_player_id
                like_item.player.nickname = (
                    (target.nickname or target.name or "DanKagu") if target else "DanKagu"
                )
                if storage_keys:
                    entries = await query_player_storage_by_keys(
                        session, rec.to_player_id, storage_keys
                    )
                    for row in entries:
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
        limit = int(request.max_results) if request.max_results > 0 else 50
        storage_keys = list(request.player_storage_keys)
        logger.info("🏠 [MySpace] GetReceivedLikesHistoryV1 (player=%s)", current_pid)

        response = my_space_pb2.MySpaceGetReceivedLikesHistoryV1.Response()
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = (
                select(MySpaceLikeRecord)
                .where(MySpaceLikeRecord.to_player_id == current_pid)
                .order_by(MySpaceLikeRecord.liked_at.desc())
                .limit(limit)
            )
            records = list((await session.execute(stmt)).scalars().all())
            for rec in records:
                like_item = response.likes.add()
                like_item.liked_at = rec.liked_at
                sender = await session.get(Player, rec.from_player_id)
                like_item.player.player_id = rec.from_player_id
                like_item.player.nickname = (
                    (sender.nickname or sender.name or "DanKagu") if sender else "DanKagu"
                )
                if storage_keys:
                    entries = await query_player_storage_by_keys(
                        session, rec.from_player_id, storage_keys
                    )
                    for row in entries:
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
