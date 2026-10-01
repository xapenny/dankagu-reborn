"""Takasho GameProduct servicer for shop products and item purchases."""

import logging
from pathlib import Path
import grpc

from dankagu.config import settings
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    game_product_pb2,
    game_product_pb2_grpc,
)

logger = logging.getLogger("dankagu.services.game_product")


class GameProductService(game_product_pb2_grpc.GameProductServicer):
    """Servicer for GameProduct listings and purchases."""

    def __init__(self) -> None:
        self.pages: dict[str, game_product_pb2.GameProductGetAvailableV1.Response] = {}
        self._load_products()

    def _load_products(self) -> None:
        products_dir = settings.hexdata_cf_dir
        if not products_dir.exists():
            return

        for hex_file in products_dir.glob("game_product*.hex"):
            try:
                hex_data = hex_file.read_text(encoding="utf-8").strip()
                resp = game_product_pb2.GameProductGetAvailableV1.Response.FromString(
                    bytes.fromhex(hex_data)
                )
                if hex_file.name == "game_product.hex":
                    token = ""
                else:
                    # Format: game_product.<page_token>.hex
                    token = hex_file.name.removeprefix("game_product.").removesuffix(".hex")
                self.pages[token] = resp
            except Exception as e:
                logger.error("Failed to parse %s: %s", hex_file.name, e)

        logger.info(
            "🛍️ Loaded %d pages of game products (root page has %d items)",
            len(self.pages),
            len(self.pages.get("", game_product_pb2.GameProductGetAvailableV1.Response()).game_products),
        )

    async def GetAvailableV1(
        self,
        request: game_product_pb2.GameProductGetAvailableV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> game_product_pb2.GameProductGetAvailableV1.Response:
        token = request.page_token or ""
        logger.info("🛍️ [GameProduct] GetAvailableV1 (page_token=%s)", token[:20] if token else "root")
        if token in self.pages:
            return self.pages[token]
        return self.pages.get("", game_product_pb2.GameProductGetAvailableV1.Response())

    async def PurchaseV1(
        self,
        request: game_product_pb2.GameProductPurchaseV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> game_product_pb2.GameProductPurchaseV1.Response:
        logger.info("🛍️ [GameProduct] PurchaseV1 (%s)", request.game_product_id)
        return game_product_pb2.GameProductPurchaseV1.Response()

    async def PurchaseV2(
        self,
        request: game_product_pb2.GameProductPurchaseV2.Request,
        context: grpc.aio.ServicerContext,
    ) -> game_product_pb2.GameProductPurchaseV2.Response:
        logger.info("🛍️ [GameProduct] PurchaseV2 (%s)", request.game_product_id)
        return game_product_pb2.GameProductPurchaseV2.Response()

    async def PurchaseAndSavePlayerStorageV1(
        self,
        request: game_product_pb2.GameProductPurchaseAndSavePlayerStorageV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> game_product_pb2.GameProductPurchaseAndSavePlayerStorageV1.Response:
        logger.info("🛍️ [GameProduct] PurchaseAndSavePlayerStorageV1 (%s)", request.game_product_id)
        return game_product_pb2.GameProductPurchaseAndSavePlayerStorageV1.Response()

    async def PurchaseAndSavePlayerStorageV2(
        self,
        request: game_product_pb2.GameProductPurchaseAndSavePlayerStorageV2.Request,
        context: grpc.aio.ServicerContext,
    ) -> game_product_pb2.GameProductPurchaseAndSavePlayerStorageV2.Response:
        logger.info("🛍️ [GameProduct] PurchaseAndSavePlayerStorageV2 (%s)", request.game_product_id)
        return game_product_pb2.GameProductPurchaseAndSavePlayerStorageV2.Response()


def register_game_product_servicer(server: grpc.aio.Server) -> None:
    servicer = GameProductService()
    method_handlers = {
        "GetAvailableV1": takasho_unary_handler(
            servicer.GetAvailableV1,
            game_product_pb2.GameProductGetAvailableV1.Request,
            game_product_pb2.GameProductGetAvailableV1.Response,
        ),
        "PurchaseV1": takasho_unary_handler(
            servicer.PurchaseV1,
            game_product_pb2.GameProductPurchaseV1.Request,
            game_product_pb2.GameProductPurchaseV1.Response,
        ),
        "PurchaseV2": takasho_unary_handler(
            servicer.PurchaseV2,
            game_product_pb2.GameProductPurchaseV2.Request,
            game_product_pb2.GameProductPurchaseV2.Response,
        ),
        "PurchaseAndSavePlayerStorageV1": takasho_unary_handler(
            servicer.PurchaseAndSavePlayerStorageV1,
            game_product_pb2.GameProductPurchaseAndSavePlayerStorageV1.Request,
            game_product_pb2.GameProductPurchaseAndSavePlayerStorageV1.Response,
        ),
        "PurchaseAndSavePlayerStorageV2": takasho_unary_handler(
            servicer.PurchaseAndSavePlayerStorageV2,
            game_product_pb2.GameProductPurchaseAndSavePlayerStorageV2.Request,
            game_product_pb2.GameProductPurchaseAndSavePlayerStorageV2.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.GameProduct", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
