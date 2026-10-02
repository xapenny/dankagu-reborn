"""Takasho BaasProduct servicer for subscription and pass products."""

import logging

import grpc

from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    baas_product_pb2,
    baas_product_pb2_grpc,
)

logger = logging.getLogger("dankagu.services.baas_product")


class BaasProductService(baas_product_pb2_grpc.BaasProductServicer):
    """Servicer for Danmaku Kagura Pass products."""

    async def GetAvailableByIDsV1(
        self,
        request: baas_product_pb2.BaasProductGetAvailableByIDsV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> baas_product_pb2.BaasProductGetAvailableByIDsV1.Response:
        logger.info("🎫 [BaasProduct] GetAvailableByIDsV1 (%s)", request.product_ids)
        response = baas_product_pb2.BaasProductGetAvailableByIDsV1.Response()
        if "com.dena.game.12026801.kagurapass1_tier8" in request.product_ids:
            baas_product = response.baas_products.add()
            baas_product.baas_product_id = "com.dena.game.12026801.kagurapass1_tier8"
            baas_product.inventory_message = "ゴールドカグラパス"
            extra = baas_product.extras.add()
            extra.baas_product_extra_id = "com.dena.game.12026801.kagurapass1_tier8"
            extra.schema_key = "ITEM"
            extra.value = b'{"ItemId":2900000002,"Prefix":"DanmakuPassActivationKey","Value":2,"Count":1,"BassPrdouctId":"kagurapass1_tier8","Extra":"\xe3\x81\x8a\xe3\x81\xbe\xe3\x81\x91"}'
            extra.system_resource_num = 1
            extra.search_label = "ActivationKey"
        if "com.dena.game.12026801.kagurapass2_tier24" in request.product_ids:
            baas_product = response.baas_products.add()
            baas_product.baas_product_id = "com.dena.game.12026801.kagurapass2_tier24"
            baas_product.inventory_message = "プラチナカグラパス"
            extra = baas_product.extras.add()
            extra.baas_product_extra_id = "com.dena.game.12026801.kagurapass2_tier24"
            extra.schema_key = "ITEM"
            extra.value = b'{"ItemId":2900000003,"Prefix":"DanmakuPassActivationKey","Value":3,"Count":1,"BassPrdouctId":"kagurapass2_tier24","Extra":"\xe3\x81\x8a\xe3\x81\xbe\xe3\x81\x91"}'
            extra.system_resource_num = 1
            extra.search_label = "ActivationKey"
        return response


def register_baas_product_servicer(server: grpc.aio.Server) -> None:
    servicer = BaasProductService()
    method_handlers = {
        "GetAvailableByIDsV1": takasho_unary_handler(
            servicer.GetAvailableByIDsV1,
            baas_product_pb2.BaasProductGetAvailableByIDsV1.Request,
            baas_product_pb2.BaasProductGetAvailableByIDsV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.BaasProduct", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
