"""Takasho Club servicers (ClubPlayer, ClubChat, ClubAchievement)."""

import logging
import grpc

from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    club_player_pb2,
    club_player_pb2_grpc,
    club_chat_pb2,
    club_chat_pb2_grpc,
    club_achievement_pb2,
    club_achievement_pb2_grpc,
)

logger = logging.getLogger("dankagu.services.club")


class ClubPlayerService(club_player_pb2_grpc.ClubPlayerServicer):
    """Servicer for club membership and roster."""

    async def GetClubs(
        self,
        request: club_player_pb2.ClubPlayerGetClubs.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_pb2.ClubPlayerGetClubs.Response:
        logger.info("🏰 [ClubPlayer] GetClubs")
        return club_player_pb2.ClubPlayerGetClubs.Response()

    async def GetPlayers(
        self,
        request: club_player_pb2.ClubPlayerGetPlayers.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_pb2.ClubPlayerGetPlayers.Response:
        logger.info("🏰 [ClubPlayer] GetPlayers")
        return club_player_pb2.ClubPlayerGetPlayers.Response()

    async def GetPlayersCount(
        self,
        request: club_player_pb2.ClubPlayerGetPlayersCount.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_pb2.ClubPlayerGetPlayersCount.Response:
        logger.info("🏰 [ClubPlayer] GetPlayersCount")
        return club_player_pb2.ClubPlayerGetPlayersCount.Response()

    async def KickPlayer(
        self,
        request: club_player_pb2.ClubPlayerKickPlayer.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_pb2.ClubPlayerKickPlayer.Response:
        logger.info("🏰 [ClubPlayer] KickPlayer")
        return club_player_pb2.ClubPlayerKickPlayer.Response()

    async def Leave(
        self,
        request: club_player_pb2.ClubPlayerLeave.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_pb2.ClubPlayerLeave.Response:
        logger.info("🏰 [ClubPlayer] Leave")
        return club_player_pb2.ClubPlayerLeave.Response()

    async def UpdateRole(
        self,
        request: club_player_pb2.ClubPlayerUpdateRole.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_pb2.ClubPlayerUpdateRole.Response:
        logger.info("🏰 [ClubPlayer] UpdateRole")
        return club_player_pb2.ClubPlayerUpdateRole.Response()


class ClubChatService(club_chat_pb2_grpc.ClubChatServicer):
    """Servicer for club chat."""

    async def GetMessagesCount(
        self,
        request: club_chat_pb2.ClubChatGetMessagesCount.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_chat_pb2.ClubChatGetMessagesCount.Response:
        logger.info("💬 [ClubChat] GetMessagesCount")
        return club_chat_pb2.ClubChatGetMessagesCount.Response()

    async def Get(
        self,
        request: club_chat_pb2.ClubChatGet.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_chat_pb2.ClubChatGet.Response:
        logger.info("💬 [ClubChat] Get")
        return club_chat_pb2.ClubChatGet.Response()

    async def CreateMessage(
        self,
        request: club_chat_pb2.ClubChatCreateMessage.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_chat_pb2.ClubChatCreateMessage.Response:
        logger.info("💬 [ClubChat] CreateMessage")
        return club_chat_pb2.ClubChatCreateMessage.Response()


class ClubAchievementService(club_achievement_pb2_grpc.ClubAchievementServicer):
    """Servicer for club achievements."""

    async def GetAvailable(
        self,
        request: club_achievement_pb2.ClubAchievementGetAvailable.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_achievement_pb2.ClubAchievementGetAvailable.Response:
        logger.info("🏆 [ClubAchievement] GetAvailable")
        return club_achievement_pb2.ClubAchievementGetAvailable.Response()

    async def UnlockAndIncrementClubScalar(
        self,
        request: club_achievement_pb2.ClubAchievementUnlockAndIncrementClubScalar.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_achievement_pb2.ClubAchievementUnlockAndIncrementClubScalar.Response:
        logger.info("🏆 [ClubAchievement] UnlockAndIncrementClubScalar")
        return club_achievement_pb2.ClubAchievementUnlockAndIncrementClubScalar.Response()


def register_club_servicers(server: grpc.aio.Server) -> None:
    player_servicer = ClubPlayerService()
    player_handlers = {
        "GetClubs": takasho_unary_handler(
            player_servicer.GetClubs,
            club_player_pb2.ClubPlayerGetClubs.Request,
            club_player_pb2.ClubPlayerGetClubs.Response,
        ),
        "GetPlayers": takasho_unary_handler(
            player_servicer.GetPlayers,
            club_player_pb2.ClubPlayerGetPlayers.Request,
            club_player_pb2.ClubPlayerGetPlayers.Response,
        ),
        "GetPlayersCount": takasho_unary_handler(
            player_servicer.GetPlayersCount,
            club_player_pb2.ClubPlayerGetPlayersCount.Request,
            club_player_pb2.ClubPlayerGetPlayersCount.Response,
        ),
        "KickPlayer": takasho_unary_handler(
            player_servicer.KickPlayer,
            club_player_pb2.ClubPlayerKickPlayer.Request,
            club_player_pb2.ClubPlayerKickPlayer.Response,
        ),
        "Leave": takasho_unary_handler(
            player_servicer.Leave,
            club_player_pb2.ClubPlayerLeave.Request,
            club_player_pb2.ClubPlayerLeave.Response,
        ),
        "UpdateRole": takasho_unary_handler(
            player_servicer.UpdateRole,
            club_player_pb2.ClubPlayerUpdateRole.Request,
            club_player_pb2.ClubPlayerUpdateRole.Response,
        ),
    }
    server.add_generic_rpc_handlers((
        grpc.method_handlers_generic_handler(
            "takasho.schema.fes.player_api.club_player.ClubPlayer", player_handlers
        ),
    ))

    chat_servicer = ClubChatService()
    chat_handlers = {
        "GetMessagesCount": takasho_unary_handler(
            chat_servicer.GetMessagesCount,
            club_chat_pb2.ClubChatGetMessagesCount.Request,
            club_chat_pb2.ClubChatGetMessagesCount.Response,
        ),
        "Get": takasho_unary_handler(
            chat_servicer.Get,
            club_chat_pb2.ClubChatGet.Request,
            club_chat_pb2.ClubChatGet.Response,
        ),
        "CreateMessage": takasho_unary_handler(
            chat_servicer.CreateMessage,
            club_chat_pb2.ClubChatCreateMessage.Request,
            club_chat_pb2.ClubChatCreateMessage.Response,
        ),
    }
    server.add_generic_rpc_handlers((
        grpc.method_handlers_generic_handler(
            "takasho.schema.fes.player_api.club_chat.ClubChat", chat_handlers
        ),
    ))

    ach_servicer = ClubAchievementService()
    ach_handlers = {
        "GetAvailable": takasho_unary_handler(
            ach_servicer.GetAvailable,
            club_achievement_pb2.ClubAchievementGetAvailable.Request,
            club_achievement_pb2.ClubAchievementGetAvailable.Response,
        ),
        "UnlockAndIncrementClubScalar": takasho_unary_handler(
            ach_servicer.UnlockAndIncrementClubScalar,
            club_achievement_pb2.ClubAchievementUnlockAndIncrementClubScalar.Request,
            club_achievement_pb2.ClubAchievementUnlockAndIncrementClubScalar.Response,
        ),
    }
    server.add_generic_rpc_handlers((
        grpc.method_handlers_generic_handler(
            "takasho.schema.fes.player_api.club_achievement.ClubAchievement", ach_handlers
        ),
    ))
