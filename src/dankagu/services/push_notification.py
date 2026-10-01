"""Takasho PushNotification servicer."""

import logging
import grpc

from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    push_notification_pb2,
    push_notification_pb2_grpc,
)

logger = logging.getLogger("dankagu.services.push_notification")


class PushNotificationService(push_notification_pb2_grpc.PushNotificationServicer):
    """Handles push notification configurations."""

    async def GetConfigV2(
        self,
        request: push_notification_pb2.PushNotificationGetConfigV2.Request,
        context: grpc.aio.ServicerContext,
    ) -> push_notification_pb2.PushNotificationGetConfigV2.Response:
        logger.info("🔔 [PushNotification] GetConfigV2")
        response = push_notification_pb2.PushNotificationGetConfigV2.Response()
        response.topic_ids.extend([
            "jp_release_i_remote_optin",
            "jp_release_night",
            "jp_release_i_night_optin",
        ])
        return response

    async def SetConfigV2(
        self,
        request: push_notification_pb2.PushNotificationSetConfigV2.Request,
        context: grpc.aio.ServicerContext,
    ) -> push_notification_pb2.PushNotificationSetConfigV2.Response:
        logger.info("🔔 [PushNotification] SetConfigV2")
        return push_notification_pb2.PushNotificationSetConfigV2.Response()

    async def GetConfigV1(
        self,
        request: push_notification_pb2.PushNotificationGetConfigV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> push_notification_pb2.PushNotificationGetConfigV1.Response:
        logger.info("🔔 [PushNotification] GetConfigV1")
        response = push_notification_pb2.PushNotificationGetConfigV1.Response()
        return response

    async def SetConfigV1(
        self,
        request: push_notification_pb2.PushNotificationSetConfigV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> push_notification_pb2.PushNotificationSetConfigV1.Response:
        logger.info("🔔 [PushNotification] SetConfigV1")
        return push_notification_pb2.PushNotificationSetConfigV1.Response()


def register_push_notification_servicer(server: grpc.aio.Server) -> None:
    servicer = PushNotificationService()
    method_handlers = {
        "GetConfigV2": takasho_unary_handler(
            servicer.GetConfigV2,
            push_notification_pb2.PushNotificationGetConfigV2.Request,
            push_notification_pb2.PushNotificationGetConfigV2.Response,
        ),
        "SetConfigV2": takasho_unary_handler(
            servicer.SetConfigV2,
            push_notification_pb2.PushNotificationSetConfigV2.Request,
            push_notification_pb2.PushNotificationSetConfigV2.Response,
        ),
        "GetConfigV1": takasho_unary_handler(
            servicer.GetConfigV1,
            push_notification_pb2.PushNotificationGetConfigV1.Request,
            push_notification_pb2.PushNotificationGetConfigV1.Response,
        ),
        "SetConfigV1": takasho_unary_handler(
            servicer.SetConfigV1,
            push_notification_pb2.PushNotificationSetConfigV1.Request,
            push_notification_pb2.PushNotificationSetConfigV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.PushNotification", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
