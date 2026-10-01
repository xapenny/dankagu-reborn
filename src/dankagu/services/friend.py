"""Takasho Friend servicer."""

import logging
import grpc

from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    friend_pb2,
    friend_pb2_grpc,
)

logger = logging.getLogger("dankagu.services.friend")


class FriendService(friend_pb2_grpc.FriendServicer):
    """Servicer for friends and followers."""

    async def GetMyFollowersV1(
        self,
        request: friend_pb2.FriendGetMyFollowersV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendGetMyFollowersV1.Response:
        logger.info("👥 [Friend] GetMyFollowersV1")
        return friend_pb2.FriendGetMyFollowersV1.Response()

    async def GetMyFollowingsV1(
        self,
        request: friend_pb2.FriendGetMyFollowingsV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendGetMyFollowingsV1.Response:
        logger.info("👥 [Friend] GetMyFollowingsV1")
        return friend_pb2.FriendGetMyFollowingsV1.Response()

    async def GetRecentlyLoggedInPlayersV1(
        self,
        request: friend_pb2.FriendGetRecentlyLoggedInPlayersV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendGetRecentlyLoggedInPlayersV1.Response:
        logger.info("👥 [Friend] GetRecentlyLoggedInPlayersV1")
        return friend_pb2.FriendGetRecentlyLoggedInPlayersV1.Response()

    async def GetFollowStatusesV1(
        self,
        request: friend_pb2.FriendGetFollowStatusesV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendGetFollowStatusesV1.Response:
        logger.info("👥 [Friend] GetFollowStatusesV1")
        return friend_pb2.FriendGetFollowStatusesV1.Response()

    async def SearchPlayerByPlayerShortIDV1(
        self,
        request: friend_pb2.FriendSearchPlayerByPlayerShortIDV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendSearchPlayerByPlayerShortIDV1.Response:
        logger.info("👥 [Friend] SearchPlayerByPlayerShortIDV1 (%s)", request.player_short_id)
        return friend_pb2.FriendSearchPlayerByPlayerShortIDV1.Response()

    async def FollowV1(
        self,
        request: friend_pb2.FriendFollowV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendFollowV1.Response:
        logger.info("👥 [Friend] FollowV1 (%s)", request.target_player_id)
        return friend_pb2.FriendFollowV1.Response()

    async def UnfollowV1(
        self,
        request: friend_pb2.FriendUnfollowV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendUnfollowV1.Response:
        logger.info("👥 [Friend] UnfollowV1 (%s)", request.target_player_id)
        return friend_pb2.FriendUnfollowV1.Response()

    async def RemoveMyFollowerV1(
        self,
        request: friend_pb2.FriendRemoveMyFollowerV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendRemoveMyFollowerV1.Response:
        logger.info("👥 [Friend] RemoveMyFollowerV1 (%s)", request.target_player_id)
        return friend_pb2.FriendRemoveMyFollowerV1.Response()


def register_friend_servicer(server: grpc.aio.Server) -> None:
    servicer = FriendService()
    method_handlers = {
        "GetMyFollowersV1": takasho_unary_handler(
            servicer.GetMyFollowersV1,
            friend_pb2.FriendGetMyFollowersV1.Request,
            friend_pb2.FriendGetMyFollowersV1.Response,
        ),
        "GetMyFollowingsV1": takasho_unary_handler(
            servicer.GetMyFollowingsV1,
            friend_pb2.FriendGetMyFollowingsV1.Request,
            friend_pb2.FriendGetMyFollowingsV1.Response,
        ),
        "GetRecentlyLoggedInPlayersV1": takasho_unary_handler(
            servicer.GetRecentlyLoggedInPlayersV1,
            friend_pb2.FriendGetRecentlyLoggedInPlayersV1.Request,
            friend_pb2.FriendGetRecentlyLoggedInPlayersV1.Response,
        ),
        "GetFollowStatusesV1": takasho_unary_handler(
            servicer.GetFollowStatusesV1,
            friend_pb2.FriendGetFollowStatusesV1.Request,
            friend_pb2.FriendGetFollowStatusesV1.Response,
        ),
        "SearchPlayerByPlayerShortIDV1": takasho_unary_handler(
            servicer.SearchPlayerByPlayerShortIDV1,
            friend_pb2.FriendSearchPlayerByPlayerShortIDV1.Request,
            friend_pb2.FriendSearchPlayerByPlayerShortIDV1.Response,
        ),
        "FollowV1": takasho_unary_handler(
            servicer.FollowV1,
            friend_pb2.FriendFollowV1.Request,
            friend_pb2.FriendFollowV1.Response,
        ),
        "UnfollowV1": takasho_unary_handler(
            servicer.UnfollowV1,
            friend_pb2.FriendUnfollowV1.Request,
            friend_pb2.FriendUnfollowV1.Response,
        ),
        "RemoveMyFollowerV1": takasho_unary_handler(
            servicer.RemoveMyFollowerV1,
            friend_pb2.FriendRemoveMyFollowerV1.Request,
            friend_pb2.FriendRemoveMyFollowerV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.fes.player_api.friend.Friend", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
