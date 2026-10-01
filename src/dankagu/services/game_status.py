"""Takasho GameStatus servicer."""

import logging
import grpc

from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    game_status_pb2,
    game_status_pb2_grpc,
)

logger = logging.getLogger("dankagu.services.game_status")


class GameStatusService(game_status_pb2_grpc.GameStatusServicer):
    """Servicer for GameStatus verification."""

    async def GetV1(
        self,
        request: game_status_pb2.GameStatusGetV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> game_status_pb2.GameStatusGetV1.Response:
        logger.info("🎮 [GameStatus] GetV1 (%d values queried)", len(request.values))
        response = game_status_pb2.GameStatusGetV1.Response()
        for v in request.values:
            status = response.statuses.add()
            status.value = v.value
        return response


def register_game_status_servicer(server: grpc.aio.Server) -> None:
    servicer = GameStatusService()
    method_handlers = {
        "GetV1": takasho_unary_handler(
            servicer.GetV1,
            game_status_pb2.GameStatusGetV1.Request,
            game_status_pb2.GameStatusGetV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.GameStatus", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
