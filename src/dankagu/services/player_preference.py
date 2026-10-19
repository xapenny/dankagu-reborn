"""Takasho PlayerPreference servicer for user preferences and TOS consent."""

import logging
import time

import grpc

from dankagu.core.database import get_sessionmaker
from dankagu.core.session import get_current_player_id
from dankagu.core.storage_state import StorageRevisionManager
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
from dankagu.models.player import Player
from dankagu.services.player_storage import batch_upsert_storage_entries

logger = logging.getLogger("dankagu.services.player_preference")


class PlayerPreferenceService(player_preference_pb2_grpc.PlayerPreferenceServicer):
    """Servicer for PlayerPreference RPCs."""

    async def GetPreferenceV1(
        self,
        request: player_preference_pb2.PlayerPreferenceGetPreferenceV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_preference_pb2.PlayerPreferenceGetPreferenceV1.Response:
        current_pid = get_current_player_id()
        if not current_pid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return player_preference_pb2.PlayerPreferenceGetPreferenceV1.Response()

        logger.info("👤 [PlayerPreference] GetPreferenceV1 for player=%s", current_pid)
        response = player_preference_pb2.PlayerPreferenceGetPreferenceV1.Response()
        pref = response.preference
        now = int(time.time())

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            player = await session.get(Player, current_pid)

        if player is not None:
            pref.birth_year = player.birth_year
            pref.birth_month = player.birth_month
            pref.consented_tos_version = player.consented_tos_version or ""
            pref.consented_privacy_policy_version = "1" if player.consented_tos_version else ""
            pref.created_at = player.registered_at
            pref.updated_at = now
            pref.nickname = player.nickname or ""
            pref.nickname_updated_at = -62135596800 if not player.nickname else now
            pref.max_friend_number = 100
            pref.max_friend_request_queue_length = 50
            response.player_short_id = player.short_id or ""
        else:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player not found")
            return response

        return response

    async def SetPreferenceV1(
        self,
        request: player_preference_pb2.PlayerPreferenceSetPreferenceV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_preference_pb2.PlayerPreferenceSetPreferenceV1.Response:
        current_pid = get_current_player_id()
        if not current_pid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return player_preference_pb2.PlayerPreferenceSetPreferenceV1.Response()

        logger.info("👤 [PlayerPreference] SetPreferenceV1 for player=%s", current_pid)
        now = int(time.time())
        response = player_preference_pb2.PlayerPreferenceSetPreferenceV1.Response()
        pref = response.preference

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            player = await session.get(Player, current_pid)
            if player is not None:
                req_pref = request.preference
                if req_pref.nickname:
                    player.nickname = req_pref.nickname
                    player.name = req_pref.nickname
                if req_pref.birth_year:
                    player.birth_year = req_pref.birth_year
                if req_pref.birth_month:
                    player.birth_month = req_pref.birth_month
                if req_pref.consented_tos_version:
                    player.consented_tos_version = req_pref.consented_tos_version

                await session.commit()

                pref.birth_year = player.birth_year
                pref.birth_month = player.birth_month
                pref.consented_tos_version = player.consented_tos_version or ""
                pref.consented_privacy_policy_version = "1" if player.consented_tos_version else ""
                pref.created_at = player.registered_at
                pref.updated_at = now
                pref.nickname = player.nickname or ""
                pref.nickname_updated_at = now
                pref.max_friend_number = 100
                pref.max_friend_request_queue_length = 50
                response.player_short_id = player.short_id or ""
                return response
            else:
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player not found")
                return response

    async def SetPreferenceAndSavePlayerStorageV1(
        self,
        request: player_preference_pb2.PlayerPreferenceSetPreferenceAndSavePlayerStorageV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_preference_pb2.PlayerPreferenceSetPreferenceAndSavePlayerStorageV1.Response:
        current_pid = get_current_player_id()
        if not current_pid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return (
                player_preference_pb2.PlayerPreferenceSetPreferenceAndSavePlayerStorageV1.Response()
            )

        logger.info(
            "👤 [PlayerPreference] SetPreferenceAndSavePlayerStorageV1 for player=%s (%d entries)",
            current_pid,
            len(request.entries),
        )
        now = int(time.time())
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            player = await session.get(Player, current_pid)
            if player is None:
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player not found")
                return player_preference_pb2.PlayerPreferenceSetPreferenceAndSavePlayerStorageV1.Response()

            if request.preference:
                req_pref = request.preference
                if req_pref.nickname:
                    player.nickname = req_pref.nickname
                    player.name = req_pref.nickname
                if req_pref.birth_year:
                    player.birth_year = req_pref.birth_year
                if req_pref.birth_month:
                    player.birth_month = req_pref.birth_month
                if req_pref.consented_tos_version:
                    player.consented_tos_version = req_pref.consented_tos_version

            if request.entries:
                rows_to_upsert = [
                    {
                        "player_id": current_pid,
                        "key": proto_entry.key,
                        "value": proto_entry.value,
                        "created_at": proto_entry.created_at or now,
                        "updated_at": now,
                    }
                    for proto_entry in request.entries
                ]
                await batch_upsert_storage_entries(session, rows_to_upsert)

            revision = await StorageRevisionManager.set_revision_and_save(
                request.next_revision, player_id=current_pid, session=session
            )
            await session.commit()

        response = (
            player_preference_pb2.PlayerPreferenceSetPreferenceAndSavePlayerStorageV1.Response()
        )
        pref = response.preference
        pref.birth_year = player.birth_year
        pref.birth_month = player.birth_month
        pref.consented_tos_version = player.consented_tos_version or ""
        pref.consented_privacy_policy_version = "1" if player.consented_tos_version else ""
        pref.created_at = player.registered_at
        pref.updated_at = now
        pref.nickname = player.nickname or ""
        pref.nickname_updated_at = now
        pref.max_friend_number = 100
        pref.max_friend_request_queue_length = 50
        response.player_short_id = player.short_id or ""

        response.entries.extend(request.entries)
        for entry in response.entries:
            entry.player_id = current_pid
            entry.created_at = entry.created_at or now
            entry.updated_at = now

        response.revision = revision
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
        current_pid = get_current_player_id()
        if not current_pid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return fes_player_preference_pb2.FesPlayerPreferenceGetV1.Response()

        logger.info("🎮 [FesPlayerPreference] GetV1 for player=%s", current_pid)
        response = fes_player_preference_pb2.FesPlayerPreferenceGetV1.Response()
        pref = response.player_preference

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            player = await session.get(Player, current_pid)

        if player is not None:
            pref.player_id = player.id
            pref.player_level = max(1, player.level)
            pref.is_my_space_released = player.level >= 10
        else:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player not found")
            return response
        return response

    async def SetAndSavePlayerStorageV1(
        self,
        request: fes_player_preference_pb2.FesPlayerPreferenceSetAndSavePlayerStorageV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> fes_player_preference_pb2.FesPlayerPreferenceSetAndSavePlayerStorageV1.Response:
        current_pid = get_current_player_id()
        if not current_pid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return fes_player_preference_pb2.FesPlayerPreferenceSetAndSavePlayerStorageV1.Response()

        logger.info(
            "🎮 [FesPlayerPreference] SetAndSavePlayerStorageV1 for player=%s (%d entries)",
            current_pid,
            len(request.entries),
        )
        now = int(time.time())
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            player = await session.get(Player, current_pid)
            if player is None:
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player not found")
                return fes_player_preference_pb2.FesPlayerPreferenceSetAndSavePlayerStorageV1.Response()

            if request.player_preference and request.player_preference.player_level > 0:
                player.level = int(request.player_preference.player_level)

            if request.entries:
                rows_to_upsert = [
                    {
                        "player_id": current_pid,
                        "key": proto_entry.key,
                        "value": proto_entry.value,
                        "created_at": proto_entry.created_at or now,
                        "updated_at": now,
                    }
                    for proto_entry in request.entries
                ]
                await batch_upsert_storage_entries(session, rows_to_upsert)

            revision = await StorageRevisionManager.set_revision_and_save(
                request.next_revision, player_id=current_pid, session=session
            )
            await session.commit()

        response = fes_player_preference_pb2.FesPlayerPreferenceSetAndSavePlayerStorageV1.Response()
        pref = response.player_preference
        pref.player_id = player.id
        pref.player_level = player.level
        pref.is_my_space_released = player.level >= 10

        response.entries.extend(request.entries)
        for entry in response.entries:
            entry.player_id = current_pid
            entry.created_at = entry.created_at or now
            entry.updated_at = now

        response.revision = revision
        return response
