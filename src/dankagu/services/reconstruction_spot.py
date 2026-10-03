"""Takasho ReconstructionSpot servicer for shrine reconstruction."""

import logging

import grpc

from dankagu.config import settings
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    reconstruction_spot_pb2,
    reconstruction_spot_pb2_grpc,
)

logger = logging.getLogger("dankagu.services.reconstruction_spot")


class ReconstructionSpotService(reconstruction_spot_pb2_grpc.ReconstructionSpotServicer):
    """Servicer for Hakurei Shrine reconstruction spots."""

    def __init__(self) -> None:
        self.cached_response: (
            reconstruction_spot_pb2.ReconstructionSpotGetAvailableV1.Response | None
        ) = None
        self._load_spots()

    def _load_spots(self) -> None:
        spot_hex = settings.hexdata_fes_dir / "reconstruction_spot.hex"
        if spot_hex.exists():
            try:
                hex_data = spot_hex.read_text(encoding="utf-8").strip()
                self.cached_response = (
                    reconstruction_spot_pb2.ReconstructionSpotGetAvailableV1.Response.FromString(
                        bytes.fromhex(hex_data)
                    )
                )
                # Ensure all spots and stages remain active through year 2100
                for spot in self.cached_response.spots:
                    spot.opened_at = 1590000000
                    spot.finished_at = 4102498799
                    spot.closed_at = 4102498799
                    for stage in spot.stages:
                        stage.opened_at = 1590000000
                        stage.closed_at = 4102498799
                logger.info(
                    "⛩️ Loaded %d active reconstruction spots", len(self.cached_response.spots)
                )
            except Exception as e:
                logger.error("Failed to parse reconstruction_spot.hex: %s", e)

        if self.cached_response is None:
            self.cached_response = (
                reconstruction_spot_pb2.ReconstructionSpotGetAvailableV1.Response()
            )

    async def GetAvailableV1(
        self,
        request: reconstruction_spot_pb2.ReconstructionSpotGetAvailableV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> reconstruction_spot_pb2.ReconstructionSpotGetAvailableV1.Response:
        logger.info("⛩️ [ReconstructionSpot] GetAvailableV1")
        if self.cached_response:
            return self.cached_response
        return reconstruction_spot_pb2.ReconstructionSpotGetAvailableV1.Response()

    async def ReceivePrizesV1(
        self,
        request: reconstruction_spot_pb2.ReconstructionSpotReceivePrizesV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> reconstruction_spot_pb2.ReconstructionSpotReceivePrizesV1.Response:
        logger.info("⛩️ [ReconstructionSpot] ReceivePrizesV1")
        return reconstruction_spot_pb2.ReconstructionSpotReceivePrizesV1.Response()


def register_reconstruction_spot_servicer(server: grpc.aio.Server) -> None:
    servicer = ReconstructionSpotService()
    method_handlers = {
        "GetAvailableV1": takasho_unary_handler(
            servicer.GetAvailableV1,
            reconstruction_spot_pb2.ReconstructionSpotGetAvailableV1.Request,
            reconstruction_spot_pb2.ReconstructionSpotGetAvailableV1.Response,
        ),
        "ReceivePrizesV1": takasho_unary_handler(
            servicer.ReceivePrizesV1,
            reconstruction_spot_pb2.ReconstructionSpotReceivePrizesV1.Request,
            reconstruction_spot_pb2.ReconstructionSpotReceivePrizesV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.fes.player_api.ReconstructionSpot", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
