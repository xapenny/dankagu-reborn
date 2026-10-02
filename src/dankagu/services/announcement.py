"""Takasho Announcement servicer."""

import logging

import grpc

from dankagu.config import settings
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    announcement_pb2,
    announcement_pb2_grpc,
)

logger = logging.getLogger("dankagu.services.announcement")


class AnnouncementService(announcement_pb2_grpc.AnnouncementServicer):
    """Servicer for game announcements and notices."""

    def __init__(self) -> None:
        self.cached_announcements: announcement_pb2.AnnouncementGetAvailableV1.Response | None = (
            None
        )
        self._load_announcements()

    def _load_announcements(self) -> None:
        hex_file = settings.hexdata_cf_dir / "announcement.100.hex"
        if not hex_file.exists():
            hex_file = settings.hexdata_cf_dir / "announcement.10.hex"

        if hex_file.exists():
            try:
                self.cached_announcements = (
                    announcement_pb2.AnnouncementGetAvailableV1.Response.FromString(
                        bytes.fromhex(hex_file.read_text(encoding="utf-8").strip())
                    )
                )
                logger.info(
                    "📢 Loaded %d announcements", len(self.cached_announcements.announcements)
                )
            except Exception as e:
                logger.error("Failed to parse announcement hex: %s", e)

        if self.cached_announcements is None:
            self.cached_announcements = announcement_pb2.AnnouncementGetAvailableV1.Response()

    async def GetAvailableV1(
        self,
        request: announcement_pb2.AnnouncementGetAvailableV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> announcement_pb2.AnnouncementGetAvailableV1.Response:
        logger.info("📢 [Announcement] GetAvailableV1 (max_results=%s)", request.max_results)
        response = announcement_pb2.AnnouncementGetAvailableV1.Response()
        if self.cached_announcements:
            response.CopyFrom(self.cached_announcements)
        response.base_image_url = f"{settings.public_base}/assets/announcements"
        return response


def register_announcement_servicer(server: grpc.aio.Server) -> None:
    servicer = AnnouncementService()
    method_handlers = {
        "GetAvailableV1": takasho_unary_handler(
            servicer.GetAvailableV1,
            announcement_pb2.AnnouncementGetAvailableV1.Request,
            announcement_pb2.AnnouncementGetAvailableV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.Announcement", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
