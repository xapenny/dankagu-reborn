"""Takasho PlayerInventory servicer for user inventories and mailbox gifts."""

import logging
import time

import grpc

from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_inventory_pb2,
    player_inventory_pb2_grpc,
)

logger = logging.getLogger("dankagu.services.player_inventory")


class PlayerInventoryService(player_inventory_pb2_grpc.PlayerInventoryServicer):
    """Servicer for player inventory items and receiving rewards."""

    async def GetInventoriesV1(
        self,
        request: player_inventory_pb2.PlayerInventoryGetInventoriesV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_inventory_pb2.PlayerInventoryGetInventoriesV1.Response:
        logger.info("📦 [PlayerInventory] GetInventoriesV1")
        return player_inventory_pb2.PlayerInventoryGetInventoriesV1.Response()

    async def GetReceivedInventoriesV1(
        self,
        request: player_inventory_pb2.PlayerInventoryGetReceivedInventoriesV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_inventory_pb2.PlayerInventoryGetReceivedInventoriesV1.Response:
        logger.info("📦 [PlayerInventory] GetReceivedInventoriesV1")
        return player_inventory_pb2.PlayerInventoryGetReceivedInventoriesV1.Response()

    async def ReceiveV1(
        self,
        request: player_inventory_pb2.PlayerInventoryReceiveV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_inventory_pb2.PlayerInventoryReceiveV1.Response:
        logger.info("📦 [PlayerInventory] ReceiveV1 (%d items)", len(request.entries))
        response = player_inventory_pb2.PlayerInventoryReceiveV1.Response()
        now = int(time.time())
        response.entries.extend(request.entries)
        for entry in response.entries:
            entry.player_id = "dankagu-reborn-user"
            entry.created_at = now
            entry.updated_at = now
        response.revision = request.next_revision or "1"
        return response

    async def GetInventoriesAndCountV1(
        self,
        request: player_inventory_pb2.PlayerInventoryGetInventoriesAndCountV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_inventory_pb2.PlayerInventoryGetInventoriesAndCountV1.Response:
        logger.info("📦 [PlayerInventory] GetInventoriesAndCountV1")
        return player_inventory_pb2.PlayerInventoryGetInventoriesAndCountV1.Response()


def register_player_inventory_servicer(server: grpc.aio.Server) -> None:
    servicer = PlayerInventoryService()
    method_handlers = {
        "GetInventoriesV1": takasho_unary_handler(
            servicer.GetInventoriesV1,
            player_inventory_pb2.PlayerInventoryGetInventoriesV1.Request,
            player_inventory_pb2.PlayerInventoryGetInventoriesV1.Response,
        ),
        "GetReceivedInventoriesV1": takasho_unary_handler(
            servicer.GetReceivedInventoriesV1,
            player_inventory_pb2.PlayerInventoryGetReceivedInventoriesV1.Request,
            player_inventory_pb2.PlayerInventoryGetReceivedInventoriesV1.Response,
        ),
        "ReceiveV1": takasho_unary_handler(
            servicer.ReceiveV1,
            player_inventory_pb2.PlayerInventoryReceiveV1.Request,
            player_inventory_pb2.PlayerInventoryReceiveV1.Response,
        ),
        "GetInventoriesAndCountV1": takasho_unary_handler(
            servicer.GetInventoriesAndCountV1,
            player_inventory_pb2.PlayerInventoryGetInventoriesAndCountV1.Request,
            player_inventory_pb2.PlayerInventoryGetInventoriesAndCountV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.PlayerInventory", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
