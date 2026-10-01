"""Takasho Wallet servicer for currency balances and player items."""

import logging
from pathlib import Path
import grpc

from dankagu.config import settings
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
        self._load_template()

    def _load_template(self) -> None:
        wallet_hex_path = settings.hexdata_cf_dir / "wallet.hex"
        if wallet_hex_path.exists():
            try:
                hex_data = wallet_hex_path.read_text(encoding="utf-8").strip()
                self.cached_response = wallet_pb2.WalletGetBalancesV2.Response.FromString(
                    bytes.fromhex(hex_data)
                )
                logger.info(
                    "💰 Loaded wallet template with %d player key values",
                    len(self.cached_response.total.player_key_values),
                )
            except Exception as e:
                logger.error("Failed to parse wallet.hex: %s", e)

        if self.cached_response is None:
            self.cached_response = wallet_pb2.WalletGetBalancesV2.Response()
            vc1 = self.cached_response.total.virtual_currencies.add()
            vc1.virtual_currency_name = "FREE_STONE"
            vc1.amount = 50000

            vc2 = self.cached_response.total.virtual_currencies.add()
            vc2.virtual_currency_name = "PAID_STONE"
            vc2.amount = 10000

    async def GetBalancesV2(
        self,
        request: wallet_pb2.WalletGetBalancesV2.Request,
        context: grpc.aio.ServicerContext,
    ) -> wallet_pb2.WalletGetBalancesV2.Response:
        logger.info("💰 [Wallet] GetBalancesV2 (expired_at=%s)", request.expired_at)
        response = wallet_pb2.WalletGetBalancesV2.Response()
        if self.cached_response:
            response.total.CopyFrom(self.cached_response.total)
            response.expiration.CopyFrom(self.cached_response.expiration)
        response.expired_at = request.expired_at or 4102444800
        return response

    async def GetBalancesV1(
        self,
        request: wallet_pb2.WalletGetBalancesV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> wallet_pb2.WalletGetBalancesV1.Response:
        logger.info("💰 [Wallet] GetBalancesV1")
        response = wallet_pb2.WalletGetBalancesV1.Response()
        if self.cached_response:
            response.total.CopyFrom(self.cached_response.total)
            response.expiration.CopyFrom(self.cached_response.expiration)
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
