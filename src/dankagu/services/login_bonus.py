"""Takasho LoginBonus servicer."""

import logging

import grpc

from dankagu.config import settings
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    login_bonus_pb2,
    login_bonus_pb2_grpc,
)

logger = logging.getLogger("dankagu.services.login_bonus")


class LoginBonusService(login_bonus_pb2_grpc.LoginBonusServicer):
    """Servicer for daily login bonuses."""

    def __init__(self) -> None:
        self.available_response: login_bonus_pb2.LoginBonusGetAvailableV1.Response | None = None
        self.countup_response: login_bonus_pb2.LoginBonusCountUpProgressV1.Response | None = None

    async def _ensure_loaded(self) -> None:
        if self.available_response is not None:
            return

        from google.protobuf import json_format
        from sqlalchemy import select

        from dankagu.core.database import get_sessionmaker
        from dankagu.models.master import MasterSystemTemplate

        try:
            session_factory = get_sessionmaker()
            async with session_factory() as session:
                result = await session.execute(
                    select(MasterSystemTemplate).where(MasterSystemTemplate.key == "login_bonus")
                )
                tmpl = result.scalar_one_or_none()
                if tmpl and tmpl.data:
                    if "available" in tmpl.data:
                        self.available_response = (
                            login_bonus_pb2.LoginBonusGetAvailableV1.Response()
                        )
                        json_format.ParseDict(
                            tmpl.data["available"],
                            self.available_response,
                            ignore_unknown_fields=True,
                        )
                    if "countup" in tmpl.data:
                        self.countup_response = (
                            login_bonus_pb2.LoginBonusCountUpProgressV1.Response()
                        )
                        json_format.ParseDict(
                            tmpl.data["countup"],
                            self.countup_response,
                            ignore_unknown_fields=True,
                        )
        except Exception as e:
            logger.error("Failed to load login bonus template from database: %s", e)

    async def GetAvailableLoginBonusV1(
        self,
        request: login_bonus_pb2.LoginBonusGetAvailableV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> login_bonus_pb2.LoginBonusGetAvailableV1.Response:
        logger.info(
            "🎁 [LoginBonus] GetAvailableLoginBonusV1 (returning empty to prevent claim freeze)"
        )
        return login_bonus_pb2.LoginBonusGetAvailableV1.Response()

    async def CountUpProgressV1(
        self,
        request: login_bonus_pb2.LoginBonusCountUpProgressV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> login_bonus_pb2.LoginBonusCountUpProgressV1.Response:
        logger.info("🎁 [LoginBonus] CountUpProgressV1 (returning empty to prevent claim freeze)")
        return login_bonus_pb2.LoginBonusCountUpProgressV1.Response()


def register_login_bonus_servicer(server: grpc.aio.Server) -> None:
    servicer = LoginBonusService()
    method_handlers = {
        "GetAvailableLoginBonusV1": takasho_unary_handler(
            servicer.GetAvailableLoginBonusV1,
            login_bonus_pb2.LoginBonusGetAvailableV1.Request,
            login_bonus_pb2.LoginBonusGetAvailableV1.Response,
        ),
        "CountUpProgressV1": takasho_unary_handler(
            servicer.CountUpProgressV1,
            login_bonus_pb2.LoginBonusCountUpProgressV1.Request,
            login_bonus_pb2.LoginBonusCountUpProgressV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.fes.player_api.LoginBonus", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
