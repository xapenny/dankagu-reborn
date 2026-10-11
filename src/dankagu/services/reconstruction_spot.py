"""Takasho ReconstructionSpot servicer for shrine reconstruction."""

import logging
import time
import uuid

import grpc

from dankagu.config import settings
from dankagu.core.database import get_sessionmaker
from dankagu.core.session import get_current_player_id
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_inventory.v1_pb2 import (
    ItemType,
    Route,
)
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    reconstruction_spot_pb2,
    reconstruction_spot_pb2_grpc,
)
from dankagu.models.inventory import PlayerInventoryItem

logger = logging.getLogger("dankagu.services.reconstruction_spot")


class ReconstructionSpotService(reconstruction_spot_pb2_grpc.ReconstructionSpotServicer):
    """Servicer for Hakurei Shrine reconstruction spots."""

    def __init__(self) -> None:
        self.cached_response: (
            reconstruction_spot_pb2.ReconstructionSpotGetAvailableV1.Response | None
        ) = None

    async def _ensure_loaded(self) -> None:
        if self.cached_response is not None:
            return

        from google.protobuf import json_format
        from sqlalchemy import select

        from dankagu.models.master import MasterReconstructionSpot

        try:
            session_factory = get_sessionmaker()
            async with session_factory() as session:
                result = await session.execute(select(MasterReconstructionSpot))
                rows = result.scalars().all()
                resp = reconstruction_spot_pb2.ReconstructionSpotGetAvailableV1.Response()
                for r in rows:
                    spot = resp.spots.add()
                    json_format.ParseDict(r.data, spot, ignore_unknown_fields=True)
                for spot in resp.spots:
                    spot.opened_at = 1590000000
                    spot.finished_at = 4102498799
                    spot.closed_at = 4102498799
                    for stage in spot.stages:
                        stage.opened_at = 1590000000
                        stage.closed_at = 4102498799
                self.cached_response = resp
                logger.info(
                    "⛩️ Loaded %d active reconstruction spots from PostgreSQL",
                    len(self.cached_response.spots),
                )
        except Exception as e:
            logger.error("Failed to load reconstruction spots from database: %s", e)

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
        await self._ensure_loaded()
        if self.cached_response:
            return self.cached_response
        return reconstruction_spot_pb2.ReconstructionSpotGetAvailableV1.Response()

    async def ReceivePrizesV1(
        self,
        request: reconstruction_spot_pb2.ReconstructionSpotReceivePrizesV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> reconstruction_spot_pb2.ReconstructionSpotReceivePrizesV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        event_count = len(request.player_event_logs)
        logger.info(
            "⛩️ [ReconstructionSpot] ReceivePrizesV1 player=%s (%d event logs)",
            current_pid,
            event_count,
        )
        response = reconstruction_spot_pb2.ReconstructionSpotReceivePrizesV1.Response()
        if not event_count:
            return response

        now = int(time.time())
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            for log in request.player_event_logs:
                logger.info(
                    "⛩️ Processing prize for reconstruction event (cat=%s, id=%s)",
                    log.event_category,
                    log.event_id,
                )
                item = PlayerInventoryItem(
                    id=str(uuid.uuid4()),
                    player_id=current_pid,
                    item_type=int(ItemType.VIRTUAL_CURRENCY),
                    schema_key="VIRTUAL_CURRENCY",
                    value=b'{"Count": 100, "CurrencyId": 1}',
                    route=int(Route.ACHIEVEMENT),
                    message="博麗神社復興達成報酬",
                    search_label="RECONSTRUCTION",
                    opened_at=now,
                    expired_at=now + 86400 * 30,
                    system_resource_name="FREE_STONE",
                    system_resource_num=100,
                    is_claimed=False,
                    created_at=now,
                )
                session.add(item)

                inv = response.inventories.add()
                inv.id = item.id
                inv.player_id = item.player_id
                inv.item_type = item.item_type
                inv.schema_key = item.schema_key
                inv.value = item.value
                inv.route = item.route
                inv.message = item.message
                inv.search_label = item.search_label
                inv.opened_at = item.opened_at
                inv.expired_at = item.expired_at
                inv.system_resource_name = item.system_resource_name
                inv.system_resource_num = item.system_resource_num
                inv.created_at = item.created_at

            await session.commit()

        return response


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
