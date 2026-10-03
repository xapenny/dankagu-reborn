"""Restoration servicer for banned words validation."""

import logging

import grpc

from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    restoration_pb2,
    restoration_pb2_grpc,
)

logger = logging.getLogger("dankagu.services.restoration")


class RestorationService(restoration_pb2_grpc.RestorationServicer):
    """Servicer for Takasho Restoration banned words validation."""

    async def ValidateBannedWordsV1(
        self,
        request: restoration_pb2.RestorationValidateBannedWordsV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> restoration_pb2.RestorationValidateBannedWordsV1.Response:
        word = request.word.strip()
        logger.info(
            "🔍 [BannedWord Validation] Validating word=%r, languages=%s",
            request.word,
            list(request.language_codes),
        )

        # In DanKagu client disassembly (BannedWordProvider.<Validate>d__3):
        # contains_banned_word = True: Validation passed (allowed / not banned).
        # contains_banned_word = False: Triggers UISupport.ShowBannedWord modal ("使用禁止文字が含まれています").
        is_allowed = bool(word)

        return restoration_pb2.RestorationValidateBannedWordsV1.Response(
            contains_banned_word=is_allowed,
            word=request.word,
        )


def register_restoration_servicer(server: grpc.aio.Server) -> None:
    """Register the Restoration service handlers on the gRPC server."""
    servicer = RestorationService()
    method_handlers = {
        "ValidateBannedWordsV1": takasho_unary_handler(
            servicer.ValidateBannedWordsV1,
            restoration_pb2.RestorationValidateBannedWordsV1.Request,
            restoration_pb2.RestorationValidateBannedWordsV1.Response,
        )
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.Restoration", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
