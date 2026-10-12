"""Takasho PlayerStorage servicer for player state persistence and retrieval."""

import logging
import time

import grpc
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from dankagu.config import settings
from dankagu.core.database import get_sessionmaker
from dankagu.core.session import get_current_player_id
from dankagu.core.storage_state import StorageRevisionManager
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_storage_pb2,
    player_storage_pb2_grpc,
)
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_storage.v2_pb2 import (
    Criterion,
)
from dankagu.models.player import Player
from dankagu.models.storage import PlayerStorageEntry

logger = logging.getLogger("dankagu.services.player_storage")


PERSONAL_STORAGE_PREFIXES = (
    "CardStatus--",
    "CardEntityStatusCollection--",
    "PlayMusicRecord--",
    "SpotStatus--",
    "SpotObjectStatus--",
    "SpotRepairStatus--",
    "SpotRepairObjectStatus--",
    "SpotVisitStatus--",
    "SpotRepairTutorialStatus--",
    "HakoniwaStatus--",
    "HeroineStatus--",
    "CharacterStatus--",
    "Formation--",
    "DeckStatus--",
    "MySpotItemStatus--",
    "MySpotStatus--",
    "ItemStockStatus--",
    "GrowthItemStock--",
    "TweetStatus--",
    "NakayosiCloudPairtEntityStatusCollection--",
    "PremiumCardStatus--",
    "EpisodeEntityStatusCollection--",
    "PlaySkipStatus--",
    "PlayerStatus--",
    "PlayerBase--",
    "PlayerPublicStatus--",
    "PlayerSetting--",
    "ExpeditionManageStatus--",
    "ExpeditionMissionRecordEntityCollecttion--",
    "PlayerExpeditionChallengeStatusCollection--",
    "MissionStatusCollection-",
    "MissionCounterStatusCollection--",
    "MissionBeginnerStatus--",
    "MissionControlStatus--",
    "ShopPurchaseStatusCollection--",
    "ShopNewStatusCollection--",
    "UnlockEntityStatusCollection--",
    "DanmakuPassSeasonStatus-",
    "EventEmblemRankingStatusCollection--",
    "EventRaidAttackStatus--",
    "SaveHash--",
)


class PlayerStorageService(player_storage_pb2_grpc.PlayerStorageServicer):
    """Handles GetEntriesV2 and SetEntriesV2 for game state persistence."""

    def __init__(self) -> None:
        self.template_entries: dict[str, bytes] = {}
        self._templates_loaded = False

    async def _ensure_templates_loaded(self) -> None:
        if self._templates_loaded:
            return

        from sqlalchemy import select

        from dankagu.models.master import MasterDefaultStorage

        try:
            session_factory = get_sessionmaker()
            async with session_factory() as session:
                result = await session.execute(select(MasterDefaultStorage))
                rows = result.scalars().all()
                for r in rows:
                    if r.key.startswith(PERSONAL_STORAGE_PREFIXES):
                        continue
                    self.template_entries[r.key] = r.value
                logger.info(
                    "💾 Loaded %d shared storage templates from PostgreSQL",
                    len(self.template_entries),
                )
        except Exception as e:
            logger.error("Failed to load default storage templates from database: %s", e)

        # Baseline entries used when no operator-supplied template covers them
        self.template_entries.setdefault(
            "PlayerBootStatus--1", b"\x08\x01\x18\x01 \x0e(\xff\xff\xff\xff\x07"
        )
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
        self.template_entries.setdefault(
            "ExpeditionMissionRecordEntityCollecttion--1",
            bytes.fromhex("0801121c08914e100018002000280030003a0200004205000000000048015000"),
        )
        self.template_entries.setdefault(
            "ExpeditionManageStatus--1",
            bytes.fromhex("080110914e18002000"),
        )
        self._templates_loaded = True

    async def GetEntriesV2(
        self,
        request: player_storage_pb2.PlayerStorageGetEntriesV2.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_storage_pb2.PlayerStorageGetEntriesV2.Response:
        await self._ensure_templates_loaded()
        current_pid = get_current_player_id()
        if not current_pid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return player_storage_pb2.PlayerStorageGetEntriesV2.Response()

        await StorageRevisionManager.initialize()
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            if current_pid not in StorageRevisionManager._player_revisions:
                player = await session.get(Player, current_pid)
                if player and player.storage_revision:
                    StorageRevisionManager.set_revision(
                        player.storage_revision, player_id=current_pid
                    )
            stmt = select(PlayerStorageEntry).where(PlayerStorageEntry.player_id == current_pid)
            result = await session.execute(stmt)
            db_entries = result.scalars().all()
            db_entry_map = {e.key: e for e in db_entries}

        response = player_storage_pb2.PlayerStorageGetEntriesV2.Response()
        response.revision = StorageRevisionManager.get_revision(current_pid)
        now = int(time.time())

        served_keys: set[str] = set()
        fallback_templates = self.template_entries

        for criterion in request.criteria:
            if criterion.matching_type == Criterion.MatchingType.EXACT:
                k = criterion.key
                if k in db_entry_map:
                    e = db_entry_map[k]
                    proto = response.entries.add()
                    proto.player_id = current_pid
                    proto.key = e.key
                    proto.value = e.value
                    proto.created_at = e.created_at
                    proto.updated_at = e.updated_at
                    served_keys.add(k)
                elif k in fallback_templates and k not in served_keys:
                    proto = response.entries.add()
                    proto.player_id = current_pid
                    proto.key = k
                    proto.value = fallback_templates[k]
                    proto.created_at = now
                    proto.updated_at = now
                    served_keys.add(k)
            else:  # FORWARD
                prefix = criterion.key
                # 1. DB entries first
                for k, e in db_entry_map.items():
                    if k.startswith(prefix) and k not in served_keys:
                        proto = response.entries.add()
                        proto.player_id = current_pid
                        proto.key = e.key
                        proto.value = e.value
                        proto.created_at = e.created_at
                        proto.updated_at = e.updated_at
                        served_keys.add(k)

                # 2. Template fallback for missing universal system keys
                for k, val in fallback_templates.items():
                    if k.startswith(prefix) and k not in served_keys:
                        proto = response.entries.add()
                        proto.player_id = current_pid
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
        current_pid = get_current_player_id()
        if not current_pid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return player_storage_pb2.PlayerStorageSetEntriesV2.Response()

        now = int(time.time())
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # Check for tutorial step 10 to ensure starter building materials exist
            has_tut_10 = any(
                proto_entry.key == "PlayerBase--1" and b"\x78\x0a" in proto_entry.value
                for proto_entry in request.entries
            )

            for proto_entry in request.entries:
                stmt = pg_insert(PlayerStorageEntry).values(
                    player_id=current_pid,
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

            # Auto-grant expedition dropped building materials if passing tutorial
            if has_tut_10:
                for mat_key, mat_val in [
                    ("ItemStockStatus--1000", bytes.fromhex("08e807108827")),  # 5000 wood
                    ("ItemStockStatus--2000", bytes.fromhex("08d00f108827")),  # 5000 stone
                ]:
                    mat_stmt = (
                        pg_insert(PlayerStorageEntry)
                        .values(
                            player_id=current_pid,
                            key=mat_key,
                            value=mat_val,
                            created_at=now,
                            updated_at=now,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[PlayerStorageEntry.player_id, PlayerStorageEntry.key]
                        )
                    )
                    await session.execute(mat_stmt)

            await session.commit()

        response = player_storage_pb2.PlayerStorageSetEntriesV2.Response()
        for proto_entry in request.entries:
            resp_entry = response.entries.add()
            resp_entry.player_id = current_pid
            resp_entry.key = proto_entry.key
            resp_entry.value = proto_entry.value
            resp_entry.created_at = proto_entry.created_at or now
            resp_entry.updated_at = now

        response.revision = await StorageRevisionManager.set_revision_and_save(
            request.next_revision, player_id=current_pid
        )
        return response

    async def GetOtherPlayerEntriesV2(
        self,
        request: player_storage_pb2.PlayerStorageGetOtherPlayerEntriesV2.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_storage_pb2.PlayerStorageGetOtherPlayerEntriesV2.Response:
        response = player_storage_pb2.PlayerStorageGetOtherPlayerEntriesV2.Response()
        target_pids = list(request.player_ids)
        if not target_pids:
            return response

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(PlayerStorageEntry).where(PlayerStorageEntry.player_id.in_(target_pids))
            result = await session.execute(stmt)
            db_entries = result.scalars().all()
            db_entry_map = {(e.player_id, e.key): e for e in db_entries}

        served_pairs: set[tuple[str, str]] = set()
        for pid in target_pids:
            for criterion in request.criteria:
                if criterion.matching_type == Criterion.MatchingType.EXACT:
                    k = criterion.key
                    pair = (pid, k)
                    if pair in db_entry_map and pair not in served_pairs:
                        e = db_entry_map[pair]
                        proto = response.entries.add()
                        proto.player_id = pid
                        proto.key = e.key
                        proto.value = e.value
                        proto.created_at = e.created_at
                        proto.updated_at = e.updated_at
                        served_pairs.add(pair)
                else:  # FORWARD
                    prefix = criterion.key
                    for (entry_pid, k), e in db_entry_map.items():
                        pair = (entry_pid, k)
                        if entry_pid == pid and k.startswith(prefix) and pair not in served_pairs:
                            proto = response.entries.add()
                            proto.player_id = pid
                            proto.key = e.key
                            proto.value = e.value
                            proto.created_at = e.created_at
                            proto.updated_at = e.updated_at
                            served_pairs.add(pair)
        return response


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
