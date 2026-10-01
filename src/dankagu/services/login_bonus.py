"""Takasho LoginBonus servicer."""

import logging
from pathlib import Path

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
        self._load_hexes()

    def _load_hexes(self) -> None:
        f_avail = settings.hexdata_fes_dir / "login_bonus.GetAvailableV1.hex"
        if f_avail.exists():
            try:
                self.available_response = (
                    login_bonus_pb2.LoginBonusGetAvailableV1.Response.FromString(
                        bytes.fromhex(f_avail.read_text(encoding="utf-8").strip())
                    )
                )
            except Exception as e:
                logger.error("Failed to parse login_bonus.GetAvailableV1.hex: %s", e)

        f_countup = settings.hexdata_fes_dir / "login_bonus.CountUpProgressV1.hex"
        if f_countup.exists():
            try:
                self.countup_response = (
                    login_bonus_pb2.LoginBonusCountUpProgressV1.Response.FromString(
                        bytes.fromhex(f_countup.read_text(encoding="utf-8").strip())
                    )
                )
            except Exception as e:
                logger.error("Failed to parse login_bonus.CountUpProgressV1.hex: %s", e)

    async def GetAvailableLoginBonusV1(
        self,
        request: login_bonus_pb2.LoginBonusGetAvailableV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> login_bonus_pb2.LoginBonusGetAvailableV1.Response:
        logger.info("🎁 [LoginBonus] GetAvailableLoginBonusV1")
        return login_bonus_pb2.LoginBonusGetAvailableV1.Response()

    async def CountUpProgressV1(
        self,
        request: login_bonus_pb2.LoginBonusCountUpProgressV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> login_bonus_pb2.LoginBonusCountUpProgressV1.Response:
        logger.info("🎁 [LoginBonus] CountUpProgressV1")
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
