"""Takasho PlayerEventLog servicer."""

import logging
import grpc

from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_event_log_pb2,
    player_event_log_pb2_grpc,
)

logger = logging.getLogger("dankagu.services.player_event_log")


class PlayerEventLogService(player_event_log_pb2_grpc.PlayerEventLogServicer):
    """Servicer for analytics and player event logging."""

    async def SendV1(
        self,
        request: player_event_log_pb2.PlayerEventLogSendV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_event_log_pb2.PlayerEventLogSendV1.Response:
        logger.info("📊 [PlayerEventLog] SendV1 (%s events)", len(request.player_event_logs))
        return player_event_log_pb2.PlayerEventLogSendV1.Response()


def register_player_event_log_servicer(server: grpc.aio.Server) -> None:
    servicer = PlayerEventLogService()
    method_handlers = {
        "SendV1": takasho_unary_handler(
            servicer.SendV1,
            player_event_log_pb2.PlayerEventLogSendV1.Request,
            player_event_log_pb2.PlayerEventLogSendV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.PlayerEventLog", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
