"""Takasho StepUpLootBoxV2 servicer."""

import logging

import grpc

from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    step_up_loot_box_pb2,
    step_up_loot_box_pb2_grpc,
)

logger = logging.getLogger("dankagu.services.step_up_loot_box")


class StepUpLootBoxService(step_up_loot_box_pb2_grpc.StepUpLootBoxV2Servicer):
    """Servicer for step-up gacha banners."""

    async def GetAvailableV1(
        self,
        request: step_up_loot_box_pb2.StepUpLootBoxV2GetAvailableV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> step_up_loot_box_pb2.StepUpLootBoxV2GetAvailableV1.Response:
        logger.info("🎁 [StepUpLootBox] GetAvailableV1")
        return step_up_loot_box_pb2.StepUpLootBoxV2GetAvailableV1.Response()

    async def PurchaseV1(
        self,
        request: step_up_loot_box_pb2.StepUpLootBoxV2PurchaseV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> step_up_loot_box_pb2.StepUpLootBoxV2PurchaseV1.Response:
        logger.info("🎁 [StepUpLootBox] PurchaseV1")
        return step_up_loot_box_pb2.StepUpLootBoxV2PurchaseV1.Response()

    async def GetProbabilityV1(
        self,
        request: step_up_loot_box_pb2.StepUpLootBoxV2GetProbabilityV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> step_up_loot_box_pb2.StepUpLootBoxV2GetProbabilityV1.Response:
        logger.info("🎁 [StepUpLootBox] GetProbabilityV1")
        return step_up_loot_box_pb2.StepUpLootBoxV2GetProbabilityV1.Response()

    async def GetDetailV1(
        self,
        request: step_up_loot_box_pb2.StepUpLootBoxV2GetDetailV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> step_up_loot_box_pb2.StepUpLootBoxV2GetDetailV1.Response:
        logger.info("🎁 [StepUpLootBox] GetDetailV1")
        return step_up_loot_box_pb2.StepUpLootBoxV2GetDetailV1.Response()


def register_step_up_loot_box_servicer(server: grpc.aio.Server) -> None:
    servicer = StepUpLootBoxService()
    method_handlers = {
        "GetAvailableV1": takasho_unary_handler(
            servicer.GetAvailableV1,
            step_up_loot_box_pb2.StepUpLootBoxV2GetAvailableV1.Request,
            step_up_loot_box_pb2.StepUpLootBoxV2GetAvailableV1.Response,
        ),
        "PurchaseV1": takasho_unary_handler(
            servicer.PurchaseV1,
            step_up_loot_box_pb2.StepUpLootBoxV2PurchaseV1.Request,
            step_up_loot_box_pb2.StepUpLootBoxV2PurchaseV1.Response,
        ),
        "GetProbabilityV1": takasho_unary_handler(
            servicer.GetProbabilityV1,
            step_up_loot_box_pb2.StepUpLootBoxV2GetProbabilityV1.Request,
            step_up_loot_box_pb2.StepUpLootBoxV2GetProbabilityV1.Response,
        ),
        "GetDetailV1": takasho_unary_handler(
            servicer.GetDetailV1,
            step_up_loot_box_pb2.StepUpLootBoxV2GetDetailV1.Request,
            step_up_loot_box_pb2.StepUpLootBoxV2GetDetailV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.StepUpLootBoxV2", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
