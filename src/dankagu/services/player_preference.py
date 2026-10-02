"""Takasho PlayerPreference servicer for user preferences and TOS consent."""

import logging
import time

import grpc
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from dankagu.core.database import get_sessionmaker
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_preference_pb2,
    player_preference_pb2_grpc,
)
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    player_preference_pb2 as fes_player_preference_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    player_preference_pb2_grpc as fes_player_preference_pb2_grpc,
)
from dankagu.models.storage import PlayerStorageEntry

logger = logging.getLogger("dankagu.services.player_preference")


class PlayerPreferenceService(player_preference_pb2_grpc.PlayerPreferenceServicer):
    """Servicer for PlayerPreference RPCs."""

    async def GetPreferenceV1(
        self,
        request: player_preference_pb2.PlayerPreferenceGetPreferenceV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_preference_pb2.PlayerPreferenceGetPreferenceV1.Response:
        logger.info("👤 [PlayerPreference] GetPreferenceV1")
        response = player_preference_pb2.PlayerPreferenceGetPreferenceV1.Response()
        pref = response.preference
        pref.birth_year = 2000
        pref.birth_month = 1
        pref.consented_tos_version = "v1"
        pref.consented_privacy_policy_version = "1"
        pref.created_at = int(time.time()) - 86400 * 30
        pref.updated_at = int(time.time())
        pref.nickname = "DanKagu"
        pref.nickname_updated_at = -62135596800
        pref.max_friend_number = 100
        pref.max_friend_request_queue_length = 50
        response.player_short_id = "12345678"
        return response

    async def SetPreferenceV1(
        self,
        request: player_preference_pb2.PlayerPreferenceSetPreferenceV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_preference_pb2.PlayerPreferenceSetPreferenceV1.Response:
        logger.info("👤 [PlayerPreference] SetPreferenceV1")
        response = player_preference_pb2.PlayerPreferenceSetPreferenceV1.Response()
        return response

    async def SetPreferenceAndSavePlayerStorageV1(
        self,
        request: player_preference_pb2.PlayerPreferenceSetPreferenceAndSavePlayerStorageV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_preference_pb2.PlayerPreferenceSetPreferenceAndSavePlayerStorageV1.Response:
        logger.info("👤 [PlayerPreference] SetPreferenceAndSavePlayerStorageV1")
        response = (
            player_preference_pb2.PlayerPreferenceSetPreferenceAndSavePlayerStorageV1.Response()
        )
        return response

    async def GetMonthlyBillingLimitV1(
        self,
        request: player_preference_pb2.PlayerPreferenceGetMonthlyBillingLimitV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_preference_pb2.PlayerPreferenceGetMonthlyBillingLimitV1.Response:
        return player_preference_pb2.PlayerPreferenceGetMonthlyBillingLimitV1.Response()

    async def GetMonthlyPurchaseSummaryV1(
        self,
        request: player_preference_pb2.PlayerPreferenceGetMonthlyPurchaseSummaryV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_preference_pb2.PlayerPreferenceGetMonthlyPurchaseSummaryV1.Response:
        return player_preference_pb2.PlayerPreferenceGetMonthlyPurchaseSummaryV1.Response()


def register_player_preference_servicer(server: grpc.aio.Server) -> None:
    servicer = PlayerPreferenceService()
    method_handlers = {
        "GetPreferenceV1": takasho_unary_handler(
            servicer.GetPreferenceV1,
            player_preference_pb2.PlayerPreferenceGetPreferenceV1.Request,
            player_preference_pb2.PlayerPreferenceGetPreferenceV1.Response,
        ),
        "SetPreferenceV1": takasho_unary_handler(
            servicer.SetPreferenceV1,
            player_preference_pb2.PlayerPreferenceSetPreferenceV1.Request,
            player_preference_pb2.PlayerPreferenceSetPreferenceV1.Response,
        ),
        "SetPreferenceAndSavePlayerStorageV1": takasho_unary_handler(
            servicer.SetPreferenceAndSavePlayerStorageV1,
            player_preference_pb2.PlayerPreferenceSetPreferenceAndSavePlayerStorageV1.Request,
            player_preference_pb2.PlayerPreferenceSetPreferenceAndSavePlayerStorageV1.Response,
        ),
        "GetMonthlyBillingLimitV1": takasho_unary_handler(
            servicer.GetMonthlyBillingLimitV1,
            player_preference_pb2.PlayerPreferenceGetMonthlyBillingLimitV1.Request,
            player_preference_pb2.PlayerPreferenceGetMonthlyBillingLimitV1.Response,
        ),
        "GetMonthlyPurchaseSummaryV1": takasho_unary_handler(
            servicer.GetMonthlyPurchaseSummaryV1,
            player_preference_pb2.PlayerPreferenceGetMonthlyPurchaseSummaryV1.Request,
            player_preference_pb2.PlayerPreferenceGetMonthlyPurchaseSummaryV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.PlayerPreference", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))

    fes_servicer = FesPlayerPreferenceService()
    fes_method_handlers = {
        "GetV1": takasho_unary_handler(
            fes_servicer.GetV1,
            fes_player_preference_pb2.FesPlayerPreferenceGetV1.Request,
            fes_player_preference_pb2.FesPlayerPreferenceGetV1.Response,
        ),
        "SetAndSavePlayerStorageV1": takasho_unary_handler(
            fes_servicer.SetAndSavePlayerStorageV1,
            fes_player_preference_pb2.FesPlayerPreferenceSetAndSavePlayerStorageV1.Request,
            fes_player_preference_pb2.FesPlayerPreferenceSetAndSavePlayerStorageV1.Response,
        ),
    }
    fes_generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.fes.player_api.player_preference.FesPlayerPreference", fes_method_handlers
    )
    server.add_generic_rpc_handlers((fes_generic_handler,))


class FesPlayerPreferenceService(fes_player_preference_pb2_grpc.FesPlayerPreferenceServicer):
    """Servicer for DanKagu game-specific FesPlayerPreference RPCs."""

    async def GetV1(
        self,
        request: fes_player_preference_pb2.FesPlayerPreferenceGetV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> fes_player_preference_pb2.FesPlayerPreferenceGetV1.Response:
        logger.info("🎮 [FesPlayerPreference] GetV1")
        response = fes_player_preference_pb2.FesPlayerPreferenceGetV1.Response()
        pref = response.player_preference
        pref.player_id = "dankagu-reborn-user"
        pref.player_level = 100
        pref.is_my_space_released = True
        return response

    async def SetAndSavePlayerStorageV1(
        self,
        request: fes_player_preference_pb2.FesPlayerPreferenceSetAndSavePlayerStorageV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> fes_player_preference_pb2.FesPlayerPreferenceSetAndSavePlayerStorageV1.Response:
        logger.info(
            "🎮 [FesPlayerPreference] SetAndSavePlayerStorageV1 (%d entries)", len(request.entries)
        )
        now = int(time.time())
        if request.entries:
            session_factory = get_sessionmaker()
            async with session_factory() as session:
                for proto_entry in request.entries:
                    p_id = proto_entry.player_id or "default-player"
                    stmt = sqlite_insert(PlayerStorageEntry).values(
                        player_id=p_id,
                        key=proto_entry.key,
                        value=proto_entry.value,
                        created_at=proto_entry.created_at or now,
                        updated_at=now,
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[PlayerStorageEntry.player_id, PlayerStorageEntry.key],
                        set_={
                            "value": stmt.excluded.value,
                            "updated_at": stmt.excluded.updated_at,
                        },
                    )
                    await session.execute(stmt)
                await session.commit()

        response = fes_player_preference_pb2.FesPlayerPreferenceSetAndSavePlayerStorageV1.Response()
        response.player_preference.CopyFrom(request.player_preference)
        response.entries.extend(request.entries)
        for entry in response.entries:
            entry.player_id = entry.player_id or "default-player"
            entry.created_at = entry.created_at or now
            entry.updated_at = now
        response.revision = request.next_revision or "1"
        return response
