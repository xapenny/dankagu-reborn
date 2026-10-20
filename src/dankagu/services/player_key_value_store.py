import logging
import time

import grpc
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from dankagu.core.database import get_sessionmaker
from dankagu.core.session import get_current_player_id
from dankagu.core.storage_state import StorageRevisionManager
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_key_value_store_pb2,
    player_key_value_store_pb2_grpc,
)
from dankagu.models.storage import PlayerKeyValue
from dankagu.services.player_storage import batch_upsert_storage_entries

logger = logging.getLogger("dankagu.services.player_key_value_store")


class PlayerKeyValueStoreService(player_key_value_store_pb2_grpc.PlayerKeyValueStoreServicer):
    """Servicer for player key-value store (PKVS)."""

    async def GetPlayerKeyValuesV1(
        self,
        request: player_key_value_store_pb2.PlayerKeyValueStoreGetPlayerKeyValuesV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_key_value_store_pb2.PlayerKeyValueStoreGetPlayerKeyValuesV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        logger.info(
            "🔑 [PKVS] GetPlayerKeyValuesV1 (%d keys, player=%s)", len(request.key), current_pid
        )
        response = player_key_value_store_pb2.PlayerKeyValueStoreGetPlayerKeyValuesV1.Response()

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(PlayerKeyValue).where(PlayerKeyValue.player_id == current_pid)
            if request.key:
                stmt = stmt.where(PlayerKeyValue.key.in_(request.key))
            res = await session.execute(stmt)
            for row in res.scalars().all():
                pkv = response.player_key_values.add()
                pkv.key = row.key
                pkv.value = row.value
                pkv.expired_at = row.expired_at
        return response

    async def IncrementPlayerKeyValuesV1(
        self,
        request: player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        now = int(time.time())
        logger.info(
            "🔑 [PKVS] IncrementPlayerKeyValuesV1 (%d items, player=%s)",
            len(request.player_key_values),
            current_pid,
        )
        response = (
            player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesV1.Response()
        )

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            for item in request.player_key_values:
                stmt = pg_insert(PlayerKeyValue).values(
                    player_id=current_pid,
                    key=item.key,
                    value=item.delta,
                    expired_at=4102412400,
                    updated_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[PlayerKeyValue.player_id, PlayerKeyValue.key],
                    set_={
                        "value": PlayerKeyValue.value + stmt.excluded.value,
                        "updated_at": stmt.excluded.updated_at,
                    },
                ).returning(PlayerKeyValue.value, PlayerKeyValue.expired_at)
                res = await session.execute(stmt)
                row = res.first()

                pkv = response.player_key_values.add()
                pkv.key = item.key
                pkv.value = row[0] if row else item.delta
                pkv.expired_at = row[1] if row else 4102412400
            await session.commit()

        return response

    async def IncrementPlayerKeyValuesAndSavePlayerStorageV1(
        self,
        request: player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesAndSavePlayerStorageV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesAndSavePlayerStorageV1.Response:
        current_pid = get_current_player_id()
        if not current_pid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesAndSavePlayerStorageV1.Response()

        now = int(time.time())
        logger.info(
            "🔑 [PKVS] IncrementPlayerKeyValuesAndSavePlayerStorageV1 (pkvs=%d, entries=%d, player=%s)",
            len(request.player_key_values),
            len(request.entries),
            current_pid,
        )
        response = player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesAndSavePlayerStorageV1.Response()

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            for item in request.player_key_values:
                stmt = pg_insert(PlayerKeyValue).values(
                    player_id=current_pid,
                    key=item.key,
                    value=item.delta,
                    expired_at=4102412400,
                    updated_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[PlayerKeyValue.player_id, PlayerKeyValue.key],
                    set_={
                        "value": PlayerKeyValue.value + stmt.excluded.value,
                        "updated_at": stmt.excluded.updated_at,
                    },
                ).returning(PlayerKeyValue.value, PlayerKeyValue.expired_at)
                res = await session.execute(stmt)
                row = res.first()

                pkv = response.player_key_values.add()
                pkv.key = item.key
                pkv.value = row[0] if row else item.delta
                pkv.expired_at = row[1] if row else 4102412400

            if request.entries:
                rows = [
                    {
                        "player_id": current_pid,
                        "key": proto_entry.key,
                        "value": proto_entry.value,
                        "created_at": proto_entry.created_at or now,
                        "updated_at": now,
                    }
                    for proto_entry in request.entries
                ]
                await batch_upsert_storage_entries(session, rows)

            response.revision = await StorageRevisionManager.set_revision_and_save(
                request.next_revision, player_id=current_pid, session=session
            )
            await session.commit()

        response.entries.extend(request.entries)
        for entry in response.entries:
            entry.player_id = current_pid
            entry.created_at = entry.created_at or now
            entry.updated_at = now

        return response

    async def UpdatePlayerKeyValuesAndSavePlayerStorageV1(
        self,
        request: player_key_value_store_pb2.PlayerKeyValueStoreUpdatePlayerKeyValuesAndSavePlayerStorageV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_key_value_store_pb2.PlayerKeyValueStoreUpdatePlayerKeyValuesAndSavePlayerStorageV1.Response:
        current_pid = get_current_player_id()
        if not current_pid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return player_key_value_store_pb2.PlayerKeyValueStoreUpdatePlayerKeyValuesAndSavePlayerStorageV1.Response()

        now = int(time.time())
        logger.info(
            "🔑 [PKVS] UpdatePlayerKeyValuesAndSavePlayerStorageV1 (pkvs=%d, entries=%d, player=%s)",
            len(request.player_key_values),
            len(request.entries),
            current_pid,
        )
        response = player_key_value_store_pb2.PlayerKeyValueStoreUpdatePlayerKeyValuesAndSavePlayerStorageV1.Response()

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            for item in request.player_key_values:
                stmt = pg_insert(PlayerKeyValue).values(
                    player_id=current_pid,
                    key=item.key,
                    value=item.value,
                    expired_at=4102412400,
                    updated_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[PlayerKeyValue.player_id, PlayerKeyValue.key],
                    set_={
                        "value": stmt.excluded.value,
                        "updated_at": stmt.excluded.updated_at,
                    },
                ).returning(PlayerKeyValue.value, PlayerKeyValue.expired_at)
                res = await session.execute(stmt)
                row = res.first()

                pkv = response.player_key_values.add()
                pkv.key = item.key
                pkv.value = row[0] if row else item.value
                pkv.expired_at = row[1] if row else 4102412400

            if request.entries:
                rows = [
                    {
                        "player_id": current_pid,
                        "key": proto_entry.key,
                        "value": proto_entry.value,
                        "created_at": proto_entry.created_at or now,
                        "updated_at": now,
                    }
                    for proto_entry in request.entries
                ]
                await batch_upsert_storage_entries(session, rows)

            response.revision = await StorageRevisionManager.set_revision_and_save(
                request.next_revision, player_id=current_pid, session=session
            )
            await session.commit()

        response.entries.extend(request.entries)
        for entry in response.entries:
            entry.player_id = current_pid
            entry.created_at = entry.created_at or now
            entry.updated_at = now

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
