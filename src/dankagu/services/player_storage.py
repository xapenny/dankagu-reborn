"""Takasho PlayerStorage servicer for player state persistence and retrieval."""

import logging
import time
from pathlib import Path

import grpc
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from dankagu.config import settings
from dankagu.core.database import get_sessionmaker
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_storage_pb2,
    player_storage_pb2_grpc,
)
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_storage.v2_pb2 import (
    Criterion,
)
from dankagu.models.storage import PlayerStorageEntry

logger = logging.getLogger("dankagu.services.player_storage")


class PlayerStorageService(player_storage_pb2_grpc.PlayerStorageServicer):
    """Handles GetEntriesV2 and SetEntriesV2 for game state persistence."""

    def __init__(self) -> None:
        self.template_entries: dict[str, bytes] = {}
        self._load_templates()

    def _load_templates(self) -> None:
        storage_dir = settings.hexdata_cf_dir
        if storage_dir.exists():
            for p in sorted(storage_dir.glob("player_storage*.hex")):
                try:
                    hex_data = p.read_text(encoding="utf-8").strip()
                    ps = player_storage_pb2.PlayerStorageGetEntriesV2.Response.FromString(
                        bytes.fromhex(hex_data)
                    )
                    for e in ps.entries:
                        self.template_entries[e.key] = e.value
                except Exception as e:
                    logger.error("Failed to parse %s: %s", p.name, e)

        # Baseline entries used when no operator-supplied template covers them
        self.template_entries.setdefault("PlayerBootStatus--1", b"\010\001 \016(\001")
        self.template_entries.setdefault(
            "JoinClubStatus--0", b"\020\363\234\252\210\006\030\363\234\252\210\006 \001"
        )
        self.template_entries.setdefault(
            "JoinClubStatus--1", b"\010\001\020\363\234\252\210\006\030\363\234\252\210\006"
        )
        self.template_entries.setdefault(
            "JoinClubStatus--2", b"\010\002\020\363\234\252\210\006\030\363\234\252\210\006"
        )
        self.template_entries.setdefault(
            "CoopEventInfoStatus-V3-3",
            b"\010\003\022\004ddd\000\030\205\016 \220\302\233\232\006(\233\360\225\232\0060\216\231\001",
        )
        self.template_entries.setdefault(
            "GpxInfoStatus-V1-1",
            b'\010\001\022*\010\250\214=\022\003\001\001\001\032\014\333\324\212\002\273\212\370\001\241\250\266\002"\014\333\324\212\002\320\376\361\001\241\250\266\002(\314\373\262\006\022*\010\220\224=\022\003\001\001\001\032\014\373\303\342\001\371\257\307\001\376\203\245\002"\014\373\303\342\001\371\257\307\001\376\203\245\002(\362\367\316\005\022$\010\370\233=\022\003\001\001\001\032\014\356\314\223\002\347\216\232\002\332\230\326\002"\006\000\000\240\320\303\002(\265\377\347\006\022*\010\340\243=\022\003\001\001\001\032\014\307\265\365\001\364\272\354\002\374\337\343\001"\014\262\374\362\001\364\272\354\002\374\337\343\001(\242\227\303\006\022\'\010\310\253=\022\003\001\001\001\032\014\212\376\240\002\261\276\307\002\210\227\315\002"\t\212\376\240\002\200\367\303\002\000(\273\357\252\007(\0050\0018\004@\310\253=H\264\272\223\232\006P\001X\001b$fa6306e0-3343-4016-bf2a-d24b6aa077bdh\357R',
        )

        logger.info("💾 Loaded %d baseline template PlayerStorage entries", len(self.template_entries))

    async def GetEntriesV2(
        self,
        request: player_storage_pb2.PlayerStorageGetEntriesV2.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_storage_pb2.PlayerStorageGetEntriesV2.Response:
        response = player_storage_pb2.PlayerStorageGetEntriesV2.Response()
        response.revision = "1"
        now = int(time.time())

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            result = await session.execute(select(PlayerStorageEntry))
            db_entries = result.scalars().all()
            db_entry_map = {e.key: e for e in db_entries}

        served_keys: set[str] = set()

        for criterion in request.criteria:
            if criterion.matching_type == Criterion.MatchingType.EXACT:
                k = criterion.key
                if k in db_entry_map:
                    e = db_entry_map[k]
                    proto = response.entries.add()
                    proto.player_id = e.player_id
                    proto.key = e.key
                    proto.value = e.value
                    proto.created_at = e.created_at
                    proto.updated_at = e.updated_at
                    served_keys.add(k)
                elif k in self.template_entries and k not in served_keys:
                    proto = response.entries.add()
                    proto.player_id = "default-player"
                    proto.key = k
                    proto.value = self.template_entries[k]
                    proto.created_at = now
                    proto.updated_at = now
                    served_keys.add(k)
            else:  # FORWARD
                prefix = criterion.key
                # 1. DB entries first
                for k, e in db_entry_map.items():
                    if k.startswith(prefix) and k not in served_keys:
                        proto = response.entries.add()
                        proto.player_id = e.player_id
                        proto.key = e.key
                        proto.value = e.value
                        proto.created_at = e.created_at
                        proto.updated_at = e.updated_at
                        served_keys.add(k)

                # 2. Template fallback for missing keys
                for k, val in self.template_entries.items():
                    if k.startswith(prefix) and k not in served_keys:
                        proto = response.entries.add()
                        proto.player_id = "default-player"
                        proto.key = k
                        proto.value = val
                        proto.created_at = now
                        proto.updated_at = now
                        served_keys.add(k)

        return response

    async def SetEntriesV2(
        self,
        request: player_storage_pb2.PlayerStorageSetEntriesV2.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_storage_pb2.PlayerStorageSetEntriesV2.Response:
        now = int(time.time())
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

        response = player_storage_pb2.PlayerStorageSetEntriesV2.Response()
        for proto_entry in request.entries:
            resp_entry = response.entries.add()
            resp_entry.player_id = proto_entry.player_id or "default-player"
            resp_entry.key = proto_entry.key
            resp_entry.value = proto_entry.value
            resp_entry.created_at = proto_entry.created_at or now
            resp_entry.updated_at = now
        response.revision = request.next_revision or "1"
        return response

    async def GetOtherPlayerEntriesV2(
        self,
        request: player_storage_pb2.PlayerStorageGetOtherPlayerEntriesV2.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_storage_pb2.PlayerStorageGetOtherPlayerEntriesV2.Response:
        return player_storage_pb2.PlayerStorageGetOtherPlayerEntriesV2.Response()


def register_player_storage_servicer(server: grpc.aio.Server) -> None:
    servicer = PlayerStorageService()
    method_handlers = {
        "GetEntriesV2": takasho_unary_handler(
            servicer.GetEntriesV2,
            player_storage_pb2.PlayerStorageGetEntriesV2.Request,
            player_storage_pb2.PlayerStorageGetEntriesV2.Response,
        ),
        "SetEntriesV2": takasho_unary_handler(
            servicer.SetEntriesV2,
            player_storage_pb2.PlayerStorageSetEntriesV2.Request,
            player_storage_pb2.PlayerStorageSetEntriesV2.Response,
        ),
        "GetOtherPlayerEntriesV2": takasho_unary_handler(
            servicer.GetOtherPlayerEntriesV2,
            player_storage_pb2.PlayerStorageGetOtherPlayerEntriesV2.Request,
            player_storage_pb2.PlayerStorageGetOtherPlayerEntriesV2.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.PlayerStorage", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
