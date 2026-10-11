"""Takasho Ranking servicers (RegularRanking, ScoreRanking)."""

import logging
import time

import grpc
from sqlalchemy import func, select

from dankagu.core.database import get_sessionmaker
from dankagu.core.session import get_current_player_id
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    regular_ranking_pb2,
    regular_ranking_pb2_grpc,
    score_ranking_pb2,
    score_ranking_pb2_grpc,
)
from dankagu.models.player import Player
from dankagu.models.ranking import SongLeaderboard

logger = logging.getLogger("dankagu.services.ranking")


class RegularRankingService(regular_ranking_pb2_grpc.RegularRankingServicer):
    """Servicer for regular score rankings."""

    async def RegisterV1(
        self,
        request: regular_ranking_pb2.RegularRankingRegisterV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> regular_ranking_pb2.RegularRankingRegisterV1.Response:
        player_id = get_current_player_id() or "dankagu-reborn-user"
        now = int(time.time())
        logger.info(
            "🏅 [RegularRanking] RegisterV1 (player=%s, key=%s, score=%d)",
            player_id,
            request.ranking_key,
            request.score,
        )

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # 1. Fetch player nickname
            player = await session.get(Player, player_id)
            nick_name = (player.nickname or player.name) if player else "DanKagu"

            # 2. Check existing score
            stmt = select(SongLeaderboard).where(
                SongLeaderboard.ranking_key == request.ranking_key,
                SongLeaderboard.player_id == player_id,
            )
            res = await session.execute(stmt)
            entry = res.scalars().first()

            if entry is None:
                entry = SongLeaderboard(
                    ranking_key=request.ranking_key,
                    player_id=player_id,
                    score=request.score,
                    updated_at=now,
                )
                session.add(entry)
                score_val = request.score
            else:
                if request.score > entry.score:
                    entry.score = request.score
                    entry.updated_at = now
                score_val = entry.score

            await session.commit()

            # 3. Calculate rank
            rank_stmt = (
                select(func.count())
                .select_from(SongLeaderboard)
                .where(
                    SongLeaderboard.ranking_key == request.ranking_key,
                    SongLeaderboard.score > score_val,
                )
            )
            higher_count = (await session.execute(rank_stmt)).scalar() or 0
            rank = higher_count + 1

        response = regular_ranking_pb2.RegularRankingRegisterV1.Response()
        player_ranking = response.player_ranking
        player_ranking.ranking_key = request.ranking_key
        player_ranking.player_id = player_id
        player_ranking.nick_name = nick_name
        player_ranking.rank = rank
        player_ranking.score = score_val
        return response

    async def GetTopRankingV1(
        self,
        request: regular_ranking_pb2.RegularRankingGetTopRankingV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> regular_ranking_pb2.RegularRankingGetTopRankingV1.Response:
        logger.info(
            "🏅 [RegularRanking] GetTopRankingV1 (key=%s, start=%d, count=%d)",
            request.ranking_key,
            request.start,
            request.count,
        )
        response = regular_ranking_pb2.RegularRankingGetTopRankingV1.Response()

        start = max(1, request.start)
        count = request.count if request.count > 0 else 100
        offset = start - 1

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = (
                select(SongLeaderboard, Player.nickname, Player.name)
                .outerjoin(Player, SongLeaderboard.player_id == Player.id)
                .where(SongLeaderboard.ranking_key == request.ranking_key)
                .order_by(SongLeaderboard.score.desc(), SongLeaderboard.updated_at.asc())
                .offset(offset)
                .limit(count)
            )
            res = await session.execute(stmt)
            rows = res.all()

        for i, (leaderboard, p_nickname, p_name) in enumerate(rows):
            item = response.player_rankings.add()
            item.ranking_key = leaderboard.ranking_key
            item.player_id = leaderboard.player_id
            item.nick_name = p_nickname or p_name or "DanKagu"
            item.rank = start + i
            item.score = leaderboard.score

        return response


class ScoreRankingService(score_ranking_pb2_grpc.ScoreRankingServicer):
    """Servicer for Danmaku Grand Prix and Class score rankings."""

    async def GetAvailableV1(
        self,
        request: score_ranking_pb2.ScoreRankingGetAvailableV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingGetAvailableV1.Response:
        logger.info("🏅 [ScoreRanking] GetAvailableV1")
        return score_ranking_pb2.ScoreRankingGetAvailableV1.Response()

    async def GetGrandPrixLeaderBoardV1(
        self,
        request: score_ranking_pb2.ScoreRankingGetGrandPrixLeaderBoardV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingGetGrandPrixLeaderBoardV1.Response:
        logger.info("🏅 [ScoreRanking] GetGrandPrixLeaderBoardV1")
        return score_ranking_pb2.ScoreRankingGetGrandPrixLeaderBoardV1.Response()

    async def GetClassGroupLeaderBoardV1(
        self,
        request: score_ranking_pb2.ScoreRankingGetClassGroupLeaderBoardV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingGetClassGroupLeaderBoardV1.Response:
        logger.info("🏅 [ScoreRanking] GetClassGroupLeaderBoardV1")
        return score_ranking_pb2.ScoreRankingGetClassGroupLeaderBoardV1.Response()

    async def GetGrandPrixRankV1(
        self,
        request: score_ranking_pb2.ScoreRankingGetGrandPrixRankV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingGetGrandPrixRankV1.Response:
        logger.info("🏅 [ScoreRanking] GetGrandPrixRankV1")
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        response = score_ranking_pb2.ScoreRankingGetGrandPrixRankV1.Response()
        target_pids = list(request.player_ids) or [current_pid]
        for pid in target_pids:
            info = response.player_grand_prix_rank_infos.add()
            info.player_id = pid
            info.rank = 1
            info.point = 100000
        return response

    async def GetClassGroupRankV1(
        self,
        request: score_ranking_pb2.ScoreRankingGetClassGroupRankV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingGetClassGroupRankV1.Response:
        logger.info("🏅 [ScoreRanking] GetClassGroupRankV1")
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        response = score_ranking_pb2.ScoreRankingGetClassGroupRankV1.Response()
        target_pids = list(request.player_ids) or [current_pid]
        for pid in target_pids:
            info = response.player_class_group_rank_infos.add()
            info.player_id = pid
            info.rank = 1
            info.score = 10000000
        return response

    async def GetClassV1(
        self,
        request: score_ranking_pb2.ScoreRankingGetClassV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingGetClassV1.Response:
        logger.info("🏅 [ScoreRanking] GetClassV1")
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        response = score_ranking_pb2.ScoreRankingGetClassV1.Response()
        target_pids = list(request.player_ids) or [current_pid]
        for pid in target_pids:
            pclass = response.player_classes.add()
            pclass.player_id = pid
            pclass.class_id = "1"
            pclass.score = 10000000
            pclass.group_id = "d5c0c1906e8e91ec6e027edfecb9eca9"
        return response

    async def ReceiveClassPrizeV1(
        self,
        request: score_ranking_pb2.ScoreRankingReceiveClassPrizeV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingReceiveClassPrizeV1.Response:
        logger.info("🏅 [ScoreRanking] ReceiveClassPrizeV1")
        return score_ranking_pb2.ScoreRankingReceiveClassPrizeV1.Response()


def register_ranking_servicers(server: grpc.aio.Server) -> None:
    regular_servicer = RegularRankingService()
    regular_handlers = {
        "RegisterV1": takasho_unary_handler(
            regular_servicer.RegisterV1,
            regular_ranking_pb2.RegularRankingRegisterV1.Request,
            regular_ranking_pb2.RegularRankingRegisterV1.Response,
        ),
        "GetTopRankingV1": takasho_unary_handler(
            regular_servicer.GetTopRankingV1,
            regular_ranking_pb2.RegularRankingGetTopRankingV1.Request,
            regular_ranking_pb2.RegularRankingGetTopRankingV1.Response,
        ),
    }
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "takasho.schema.fes.player_api.RegularRanking", regular_handlers
            ),
        )
    )

    score_servicer = ScoreRankingService()
    score_handlers = {
        "GetAvailableV1": takasho_unary_handler(
            score_servicer.GetAvailableV1,
            score_ranking_pb2.ScoreRankingGetAvailableV1.Request,
            score_ranking_pb2.ScoreRankingGetAvailableV1.Response,
        ),
        "GetGrandPrixLeaderBoardV1": takasho_unary_handler(
            score_servicer.GetGrandPrixLeaderBoardV1,
            score_ranking_pb2.ScoreRankingGetGrandPrixLeaderBoardV1.Request,
            score_ranking_pb2.ScoreRankingGetGrandPrixLeaderBoardV1.Response,
        ),
        "GetClassGroupLeaderBoardV1": takasho_unary_handler(
            score_servicer.GetClassGroupLeaderBoardV1,
            score_ranking_pb2.ScoreRankingGetClassGroupLeaderBoardV1.Request,
            score_ranking_pb2.ScoreRankingGetClassGroupLeaderBoardV1.Response,
        ),
        "GetGrandPrixRankV1": takasho_unary_handler(
            score_servicer.GetGrandPrixRankV1,
            score_ranking_pb2.ScoreRankingGetGrandPrixRankV1.Request,
            score_ranking_pb2.ScoreRankingGetGrandPrixRankV1.Response,
        ),
        "GetClassGroupRankV1": takasho_unary_handler(
            score_servicer.GetClassGroupRankV1,
            score_ranking_pb2.ScoreRankingGetClassGroupRankV1.Request,
            score_ranking_pb2.ScoreRankingGetClassGroupRankV1.Response,
        ),
        "GetClassV1": takasho_unary_handler(
            score_servicer.GetClassV1,
            score_ranking_pb2.ScoreRankingGetClassV1.Request,
            score_ranking_pb2.ScoreRankingGetClassV1.Response,
        ),
        "ReceiveClassPrizeV1": takasho_unary_handler(
            score_servicer.ReceiveClassPrizeV1,
            score_ranking_pb2.ScoreRankingReceiveClassPrizeV1.Request,
            score_ranking_pb2.ScoreRankingReceiveClassPrizeV1.Response,
        ),
    }
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "takasho.schema.fes.player_api.score_ranking.ScoreRanking", score_handlers
            ),
        )
    )
