"""Takasho LootBoxV3 servicer for gacha banners and pulls."""

import logging

import grpc

from dankagu.config import settings
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    loot_box_pb2,
    loot_box_pb2_grpc,
)

logger = logging.getLogger("dankagu.services.loot_box")


class LootBoxService(loot_box_pb2_grpc.LootBoxV3Servicer):
    """Servicer for LootBox V3 gacha banners."""

    def __init__(self) -> None:
        self.pages: dict[str, loot_box_pb2.LootBoxV3GetAvailableV1.Response] = {}
        self._load_loot_boxes()

    def _load_loot_boxes(self) -> None:
        loot_box_dir = settings.hexdata_cf_dir
        if not loot_box_dir.exists():
            return

        for hex_file in loot_box_dir.glob("loot_box*.hex"):
            try:
                hex_data = hex_file.read_text(encoding="utf-8").strip()
                resp = loot_box_pb2.LootBoxV3GetAvailableV1.Response.FromString(
                    bytes.fromhex(hex_data)
                )
                if hex_file.name == "loot_box.hex":
                    token = ""
                else:
                    token = hex_file.name.removeprefix("loot_box.").removesuffix(".hex")
                self.pages[token] = resp
            except Exception as e:
                logger.error("Failed to parse %s: %s", hex_file.name, e)

        logger.info(
            "🎁 Loaded %d pages of loot boxes (root page has %d items)",
            len(self.pages),
            len(
                self.pages.get(
                    "", loot_box_pb2.LootBoxV3GetAvailableV1.Response()
                ).loot_box_products
            ),
        )

    async def GetAvailableV1(
        self,
        request: loot_box_pb2.LootBoxV3GetAvailableV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> loot_box_pb2.LootBoxV3GetAvailableV1.Response:
        token = request.page_token or ""
        logger.info("🎁 [LootBox] GetAvailableV1 (page_token=%s)", token[:20] if token else "root")
        if token in self.pages:
            return self.pages[token]
        return self.pages.get("", loot_box_pb2.LootBoxV3GetAvailableV1.Response())

    async def PurchaseV1(
        self,
        request: loot_box_pb2.LootBoxV3PurchaseV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> loot_box_pb2.LootBoxV3PurchaseV1.Response:
        logger.info("🎁 [LootBox] PurchaseV1 (%s)", request.loot_box_product_id)
        return loot_box_pb2.LootBoxV3PurchaseV1.Response()

    async def GetProbabilityV1(
        self,
        request: loot_box_pb2.LootBoxV3GetProbabilityV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> loot_box_pb2.LootBoxV3GetProbabilityV1.Response:
        logger.info("🎁 [LootBox] GetProbabilityV1 (%s)", request.loot_box_product_id)
        return loot_box_pb2.LootBoxV3GetProbabilityV1.Response()

    async def GetDetailV1(
        self,
        request: loot_box_pb2.LootBoxV3GetDetailV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> loot_box_pb2.LootBoxV3GetDetailV1.Response:
        logger.info("🎁 [LootBox] GetDetailV1 (%s)", request.loot_box_product_id)
        return loot_box_pb2.LootBoxV3GetDetailV1.Response()


def register_loot_box_servicer(server: grpc.aio.Server) -> None:
    servicer = LootBoxService()
    method_handlers = {
        "GetAvailableV1": takasho_unary_handler(
            servicer.GetAvailableV1,
            loot_box_pb2.LootBoxV3GetAvailableV1.Request,
            loot_box_pb2.LootBoxV3GetAvailableV1.Response,
        ),
        "PurchaseV1": takasho_unary_handler(
            servicer.PurchaseV1,
            loot_box_pb2.LootBoxV3PurchaseV1.Request,
            loot_box_pb2.LootBoxV3PurchaseV1.Response,
        ),
        "GetProbabilityV1": takasho_unary_handler(
            servicer.GetProbabilityV1,
            loot_box_pb2.LootBoxV3GetProbabilityV1.Request,
            loot_box_pb2.LootBoxV3GetProbabilityV1.Response,
        ),
        "GetDetailV1": takasho_unary_handler(
            servicer.GetDetailV1,
            loot_box_pb2.LootBoxV3GetDetailV1.Request,
            loot_box_pb2.LootBoxV3GetDetailV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.LootBoxV3", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
