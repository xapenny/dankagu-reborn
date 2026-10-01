"""Zendesk servicer for in-game customer support / unread badge count."""

import grpc

from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    zendesk_pb2,
    zendesk_pb2_grpc,
)


class ZendeskService(zendesk_pb2_grpc.ZendeskServicer):
    """Servicer for Zendesk in-game customer support badge."""

    async def GetUnreadCountV1(
        self,
        request: zendesk_pb2.ZendeskGetUnreadCountV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> zendesk_pb2.ZendeskGetUnreadCountV1.Response:
        return zendesk_pb2.ZendeskGetUnreadCountV1.Response(unread_count=0)


def register_zendesk_servicer(server: grpc.aio.Server) -> None:
    servicer = ZendeskService()
    method_handlers = {
        "GetUnreadCountV1": takasho_unary_handler(
            servicer.GetUnreadCountV1,
            zendesk_pb2.ZendeskGetUnreadCountV1.Request,
            zendesk_pb2.ZendeskGetUnreadCountV1.Response,
        )
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.fes.player_api.Zendesk", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
