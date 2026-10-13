"""Takasho Wallet servicer for currency balances and player items."""

import logging
from typing import Any

import grpc

from dankagu.core.database import get_sessionmaker
from dankagu.core.economy import get_or_create_wallet, get_player_key_value
from dankagu.core.session import get_current_player_id
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    wallet_pb2,
    wallet_pb2_grpc,
)

logger = logging.getLogger("dankagu.services.wallet")


class WalletService(wallet_pb2_grpc.WalletServicer):
    """Servicer for player currency and wallet balances."""

    def __init__(self) -> None:
        self.cached_response: wallet_pb2.WalletGetBalancesV2.Response | None = None

    async def _ensure_loaded(self) -> None:
        if self.cached_response is not None:
            return

        from google.protobuf import json_format
        from sqlalchemy import select

        from dankagu.models.master import MasterSystemTemplate

        try:
            session_factory = get_sessionmaker()
            async with session_factory() as session:
                result = await session.execute(
                    select(MasterSystemTemplate).where(MasterSystemTemplate.key == "wallet")
                )
                tmpl = result.scalar_one_or_none()
                if tmpl and tmpl.data:
                    resp = wallet_pb2.WalletGetBalancesV2.Response()
                    json_format.ParseDict(tmpl.data, resp, ignore_unknown_fields=True)
                    self.cached_response = resp
                    logger.info(
                        "💰 Loaded wallet template with %d player key values from PostgreSQL",
                        len(self.cached_response.total.player_key_values),
                    )
        except Exception as e:
            logger.error("Failed to load wallet template from database: %s", e)

        if self.cached_response is None:
            self.cached_response = wallet_pb2.WalletGetBalancesV2.Response()
            vc1 = self.cached_response.total.virtual_currencies.add()
            vc1.virtual_currency_name = "FREE_STONE"
            vc1.amount = 60000

            vc2 = self.cached_response.total.virtual_currencies.add()
            vc2.virtual_currency_name = "PAID_STONE"
            vc2.amount = 30000

    async def _populate_wallet_balances(self, response_total: Any, player_id: str | None) -> None:
        if not player_id:
            return

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            wallet = await get_or_create_wallet(session, player_id)
            point_amount = await get_player_key_value(session, player_id, "point", default=8612)
            await session.commit()
            free_amount = wallet.free_stones
            paid_amount = wallet.paid_stones

        found_free = False
        found_paid = False
        for vc in response_total.virtual_currencies:
            if vc.virtual_currency_name == "FREE_STONE":
                vc.amount = free_amount
                found_free = True
            elif vc.virtual_currency_name == "PAID_STONE":
                vc.amount = paid_amount
                found_paid = True

        if not found_free:
            vc1 = response_total.virtual_currencies.add()
            vc1.virtual_currency_name = "FREE_STONE"
            vc1.amount = free_amount

        if not found_paid:
            vc2 = response_total.virtual_currencies.add()
            vc2.virtual_currency_name = "PAID_STONE"
            vc2.amount = paid_amount

        found_point = False
        for pkv in response_total.player_key_values:
            if pkv.key == "point":
                pkv.amount = point_amount
                found_point = True
                break
        if not found_point:
            pkv = response_total.player_key_values.add()
            pkv.key = "point"
            pkv.amount = point_amount

    async def GetBalancesV2(
        self,
        request: wallet_pb2.WalletGetBalancesV2.Request,
        context: grpc.aio.ServicerContext,
    ) -> wallet_pb2.WalletGetBalancesV2.Response:
        await self._ensure_loaded()
        current_pid = get_current_player_id()
        logger.info(
            "💰 [Wallet] GetBalancesV2 for player=%s (expired_at=%s)",
            current_pid,
            request.expired_at,
        )
        response = wallet_pb2.WalletGetBalancesV2.Response()
        if self.cached_response:
            response.total.CopyFrom(self.cached_response.total)
            response.expiration.CopyFrom(self.cached_response.expiration)
        await self._populate_wallet_balances(response.total, current_pid)
        response.expired_at = request.expired_at or 4102444800
        return response

    async def GetBalancesV1(
        self,
        request: wallet_pb2.WalletGetBalancesV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> wallet_pb2.WalletGetBalancesV1.Response:
        await self._ensure_loaded()
        current_pid = get_current_player_id()
        logger.info("💰 [Wallet] GetBalancesV1 for player=%s", current_pid)
        response = wallet_pb2.WalletGetBalancesV1.Response()
        bal = response.balance.add()
        if self.cached_response:
            bal.CopyFrom(self.cached_response.total)
        await self._populate_wallet_balances(bal, current_pid)
        return response


def register_wallet_servicer(server: grpc.aio.Server) -> None:
    servicer = WalletService()
    method_handlers = {
        "GetBalancesV1": takasho_unary_handler(
            servicer.GetBalancesV1,
            wallet_pb2.WalletGetBalancesV1.Request,
            wallet_pb2.WalletGetBalancesV1.Response,
        ),
        "GetBalancesV2": takasho_unary_handler(
            servicer.GetBalancesV2,
            wallet_pb2.WalletGetBalancesV2.Request,
            wallet_pb2.WalletGetBalancesV2.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.Wallet", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
