import logging
import time
import grpc
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from dankagu.core.database import get_sessionmaker
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_key_value_store_pb2,
    player_key_value_store_pb2_grpc,
)
from dankagu.models.storage import PlayerStorageEntry

logger = logging.getLogger("dankagu.services.player_key_value_store")


class PlayerKeyValueStoreService(player_key_value_store_pb2_grpc.PlayerKeyValueStoreServicer):
    """Servicer for player key-value store (PKVS)."""

    async def GetPlayerKeyValuesV1(
        self,
        request: player_key_value_store_pb2.PlayerKeyValueStoreGetPlayerKeyValuesV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_key_value_store_pb2.PlayerKeyValueStoreGetPlayerKeyValuesV1.Response:
        logger.info("🔑 [PKVS] GetPlayerKeyValuesV1 (%d keys)", len(request.keys))
        return player_key_value_store_pb2.PlayerKeyValueStoreGetPlayerKeyValuesV1.Response()

    async def IncrementPlayerKeyValuesV1(
        self,
        request: player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesV1.Response:
        logger.info("🔑 [PKVS] IncrementPlayerKeyValuesV1")
        response = player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesV1.Response()
        for item in request.player_key_values:
            pkv = response.player_key_values.add()
            pkv.key = item.key
            pkv.value = item.amount
            pkv.expired_at = 4102412400
        return response

    async def IncrementPlayerKeyValuesAndSavePlayerStorageV1(
        self,
        request: player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesAndSavePlayerStorageV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesAndSavePlayerStorageV1.Response:
        logger.info("🔑 [PKVS] IncrementPlayerKeyValuesAndSavePlayerStorageV1")
        response = player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesAndSavePlayerStorageV1.Response()
        for item in request.player_key_values:
            pkv = response.player_key_values.add()
            pkv.key = item.key
            pkv.value = item.amount
            pkv.expired_at = 4102412400

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

        response.entries.extend(request.entries)
        for entry in response.entries:
            entry.player_id = entry.player_id or "default-player"
            entry.created_at = entry.created_at or now
            entry.updated_at = now
        response.revision = request.next_revision or "1"
        return response

    async def UpdatePlayerKeyValuesAndSavePlayerStorageV1(
        self,
        request: player_key_value_store_pb2.PlayerKeyValueStoreUpdatePlayerKeyValuesAndSavePlayerStorageV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_key_value_store_pb2.PlayerKeyValueStoreUpdatePlayerKeyValuesAndSavePlayerStorageV1.Response:
        logger.info("🔑 [PKVS] UpdatePlayerKeyValuesAndSavePlayerStorageV1")
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

        response = player_key_value_store_pb2.PlayerKeyValueStoreUpdatePlayerKeyValuesAndSavePlayerStorageV1.Response()
        response.entries.extend(request.entries)
        for entry in response.entries:
            entry.player_id = entry.player_id or "default-player"
            entry.created_at = entry.created_at or now
            entry.updated_at = now
        response.revision = request.next_revision or "1"
        return response


def register_player_key_value_store_servicer(server: grpc.aio.Server) -> None:
    servicer = PlayerKeyValueStoreService()
    method_handlers = {
        "GetPlayerKeyValuesV1": takasho_unary_handler(
            servicer.GetPlayerKeyValuesV1,
            player_key_value_store_pb2.PlayerKeyValueStoreGetPlayerKeyValuesV1.Request,
            player_key_value_store_pb2.PlayerKeyValueStoreGetPlayerKeyValuesV1.Response,
        ),
        "IncrementPlayerKeyValuesV1": takasho_unary_handler(
            servicer.IncrementPlayerKeyValuesV1,
            player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesV1.Request,
            player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesV1.Response,
        ),
        "IncrementPlayerKeyValuesAndSavePlayerStorageV1": takasho_unary_handler(
            servicer.IncrementPlayerKeyValuesAndSavePlayerStorageV1,
            player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesAndSavePlayerStorageV1.Request,
            player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesAndSavePlayerStorageV1.Response,
        ),
        "UpdatePlayerKeyValuesAndSavePlayerStorageV1": takasho_unary_handler(
            servicer.UpdatePlayerKeyValuesAndSavePlayerStorageV1,
            player_key_value_store_pb2.PlayerKeyValueStoreUpdatePlayerKeyValuesAndSavePlayerStorageV1.Request,
            player_key_value_store_pb2.PlayerKeyValueStoreUpdatePlayerKeyValuesAndSavePlayerStorageV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.PlayerKeyValueStore", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
