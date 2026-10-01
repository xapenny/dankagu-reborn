"""Takasho Ranking servicers (RegularRanking, ScoreRanking)."""

import logging
import grpc

from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    regular_ranking_pb2,
    regular_ranking_pb2_grpc,
    score_ranking_pb2,
    score_ranking_pb2_grpc,
)

logger = logging.getLogger("dankagu.services.ranking")


class RegularRankingService(regular_ranking_pb2_grpc.RegularRankingServicer):
    """Servicer for regular score rankings."""

    async def RegisterV1(
        self,
        request: regular_ranking_pb2.RegularRankingRegisterV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> regular_ranking_pb2.RegularRankingRegisterV1.Response:
        logger.info("🏅 [RegularRanking] RegisterV1 (key=%s, score=%d)", request.ranking_key, request.score)
        response = regular_ranking_pb2.RegularRankingRegisterV1.Response()
        player_ranking = response.player_ranking
        player_ranking.ranking_key = request.ranking_key
        player_ranking.player_id = "dankagu-reborn-user"
        player_ranking.nick_name = "DanKagu"
        player_ranking.score = request.score
        return response

    async def GetTopRankingV1(
        self,
        request: regular_ranking_pb2.RegularRankingGetTopRankingV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> regular_ranking_pb2.RegularRankingGetTopRankingV1.Response:
        logger.info("🏅 [RegularRanking] GetTopRankingV1 (key=%s)", request.ranking_key)
        return regular_ranking_pb2.RegularRankingGetTopRankingV1.Response()


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
        response = score_ranking_pb2.ScoreRankingGetGrandPrixRankV1.Response()
        info = response.player_grand_prix_rank_infos.add()
        info.player_id = "dankagu-reborn-user"
        info.rank = 1
        info.point = 100000
        return response

    async def GetClassGroupRankV1(
        self,
        request: score_ranking_pb2.ScoreRankingGetClassGroupRankV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingGetClassGroupRankV1.Response:
        logger.info("🏅 [ScoreRanking] GetClassGroupRankV1")
        response = score_ranking_pb2.ScoreRankingGetClassGroupRankV1.Response()
        info = response.player_class_group_rank_infos.add()
        info.player_id = "dankagu-reborn-user"
        info.rank = 1
        info.score = 10000000
        return response

    async def GetClassV1(
        self,
        request: score_ranking_pb2.ScoreRankingGetClassV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingGetClassV1.Response:
        logger.info("🏅 [ScoreRanking] GetClassV1")
        response = score_ranking_pb2.ScoreRankingGetClassV1.Response()
        pclass = response.player_classes.add()
        pclass.player_id = "dankagu-reborn-user"
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
    server.add_generic_rpc_handlers((
        grpc.method_handlers_generic_handler(
            "takasho.schema.fes.player_api.RegularRanking", regular_handlers
        ),
    ))

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
    server.add_generic_rpc_handlers((
        grpc.method_handlers_generic_handler(
            "takasho.schema.fes.player_api.score_ranking.ScoreRanking", score_handlers
        ),
    ))
