"""Takasho Subscription RenewalReward servicer."""

import logging

import grpc

from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.fes.player_api.subscription import (
    renewal_reward_pb2,
    renewal_reward_pb2_grpc,
)

logger = logging.getLogger("dankagu.services.subscription")


class RenewalRewardService(renewal_reward_pb2_grpc.RenewalRewardServicer):
    """Servicer for monthly pass renewal rewards."""

    async def GetAvailableV1(
        self,
        request: renewal_reward_pb2.RenewalRewardGetAvailableV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> renewal_reward_pb2.RenewalRewardGetAvailableV1.Response:
        logger.info("💎 [RenewalReward] GetAvailableV1")
        return renewal_reward_pb2.RenewalRewardGetAvailableV1.Response()

    async def ReceiveV1(
        self,
        request: renewal_reward_pb2.RenewalRewardReceiveV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> renewal_reward_pb2.RenewalRewardReceiveV1.Response:
        logger.info("💎 [RenewalReward] ReceiveV1")
        response = renewal_reward_pb2.RenewalRewardReceiveV1.Response()
        rc = response.renewal_counts.add()
        rc.subscription_product_id = "subs.currency.com.dena.game.12026801.mitamaishipass370"
        rc.count = 12
        return response


def register_subscription_servicers(server: grpc.aio.Server) -> None:
    servicer = RenewalRewardService()
    method_handlers = {
        "GetAvailableV1": takasho_unary_handler(
            servicer.GetAvailableV1,
            renewal_reward_pb2.RenewalRewardGetAvailableV1.Request,
            renewal_reward_pb2.RenewalRewardGetAvailableV1.Response,
        ),
        "ReceiveV1": takasho_unary_handler(
            servicer.ReceiveV1,
            renewal_reward_pb2.RenewalRewardReceiveV1.Request,
            renewal_reward_pb2.RenewalRewardReceiveV1.Response,
        ),
    }
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "takasho.schema.fes.player_api.subscription.renewal_reward.RenewalReward",
                method_handlers,
            ),
        )
    )
