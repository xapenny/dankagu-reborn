"""Takasho PlayerStorage servicer for player state persistence and retrieval."""

import logging
import time
from typing import Any

import grpc
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.core.database import get_sessionmaker
from dankagu.core.economy import (
    CARD_FIELD_CARD_ID,
    CARD_FIELD_DUPLICATE,
    CARD_STATUS_PREFIX,
    CardCanonicalisation,
    _parse_card_fields,
    canonicalise_card_entries,
    card_progress,
    merge_card_payload,
)
from dankagu.core.session import get_current_player_id
from dankagu.core.storage_state import StorageRevisionManager
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.errors import fail_internal_on_error
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


async def batch_upsert_storage_entries(
    session: AsyncSession, rows: list[dict[str, Any]]
) -> None:
    """Upsert multiple PlayerStorageEntry rows in a single PostgreSQL statement.

    Deduplicates by ``(player_id, key)`` (keeping the last occurrence) so PostgreSQL
    never raises ``CardinalityViolationError`` when a request repeats a key.
    """
    if not rows:
        return
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        deduped[(row["player_id"], row["key"])] = row
    values = list(deduped.values())
    stmt = pg_insert(PlayerStorageEntry).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[PlayerStorageEntry.player_id, PlayerStorageEntry.key],
        set_={
            "value": stmt.excluded.value,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await session.execute(stmt)


async def _upsert_entry(
    session: AsyncSession, player_id: str, key: str, value: bytes, now: int
) -> None:
    """Write one storage entry, replacing whatever was there."""
    await batch_upsert_storage_entries(
        session,
        [
            {
                "player_id": player_id,
                "key": key,
                "value": value,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )


async def _plan_card_merge(
    session: AsyncSession, player_id: str, request: Any, now: int
) -> CardCanonicalisation | None:
    """Work out how this request's card entries should be folded together."""
    incoming = [(entry.key, entry.value) for entry in request.entries if entry.value]
    if not any(key.startswith(CARD_STATUS_PREFIX) for key, _ in incoming):
        return None

    stored = await session.execute(
        select(PlayerStorageEntry).where(
            PlayerStorageEntry.player_id == player_id,
            PlayerStorageEntry.key.like(f"{CARD_STATUS_PREFIX}%"),
        )
    )
    existing: dict[int, tuple[str, bytes]] = {}
    duplicates: dict[int, list[str]] = {}
    for entry in sorted(stored.scalars().all(), key=lambda item: item.key):
        fields = _parse_card_fields(entry.value)
        card_id = fields.get(CARD_FIELD_CARD_ID)
        if not card_id:
            continue
        card_id = int(card_id)
        if card_id in existing:
            # The collection already holds this card more than once. Keep the entry the
            # player has developed furthest (a level or a limit break must never be lost
            # to tidying up), fold the copies this entry stands for into the surviving
            # counter, and remember the redundant key.
            keep_key, keep_value = existing[card_id]
            keep_fields = _parse_card_fields(keep_value)
            survivor_fields, survivor_key = (
                (fields, entry.key)
                if card_progress(fields) > card_progress(keep_fields)
                else (keep_fields, keep_key)
            )
            if survivor_key != keep_key:
                duplicates.setdefault(card_id, []).append(keep_key)
            duplicates.setdefault(card_id, []).append(entry.key)
            existing[card_id] = (
                survivor_key,
                merge_card_payload(
                    card_id=card_id,
                    survivor=survivor_fields,
                    others=[keep_fields if survivor_key != keep_key else fields],
                    duplicate=(keep_fields.get(CARD_FIELD_DUPLICATE, 1) or 1)
                    + (fields.get(CARD_FIELD_DUPLICATE, 1) or 1),
                    now=now,
                ),
            )
            continue
        existing[card_id] = (entry.key, entry.value)

    plan = canonicalise_card_entries(existing, incoming, now)
    if plan is not None and duplicates:
        # Only drop the redundant keys of cards this request actually rewrites: the
        # survivor's counter is carried over when it is written, so deleting a duplicate
        # of a card the request does not mention would silently lose the copies it stood
        # for. Those rows stay until that card is next written.
        doomed = [
            key for card_id, keys in duplicates.items() if card_id in plan.copies for key in keys
        ]
        plan.drop_keys.extend(doomed)
        logger.info(
            "🎴 [Storage] Repairing %d pre-existing duplicate card entr(ies) for player %s",
            len(doomed),
            player_id,
        )
    return plan


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
        self.template_entries.setdefault(
            "EventInfoStatus-V38-38",
            bytes.fromhex("08261000180032040101010138ca84d1d60658ca84d1d60670037803"),
        )
        self.template_entries.setdefault(
            "EventInfoStatus-V39-39",
            bytes.fromhex("08271000180032040101010138ca84d1d60658ca84d1d60670037803"),
        )
        self.template_entries.setdefault(
            "EventInfoStatus-V40-40",
            bytes.fromhex("08281000180032040101010138ca84d1d60658ca84d1d60670037803"),
        )
        self.template_entries.setdefault(
            "EventInfoStatus-V41-41",
            bytes.fromhex("08291000180032040101010138ca84d1d60658ca84d1d60670037803"),
        )
        self._templates_loaded = True

    @fail_internal_on_error("Failed to read player storage entries")
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
        criteria_list = list(request.criteria)
        db_entry_map: dict[str, PlayerStorageEntry] = {}

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            if current_pid not in StorageRevisionManager._player_revisions:
                player = await session.get(Player, current_pid)
                if player and player.storage_revision:
                    StorageRevisionManager.set_revision(
                        player.storage_revision, player_id=current_pid
                    )
            if criteria_list:
                stmt = select(PlayerStorageEntry).where(
                    PlayerStorageEntry.player_id == current_pid
                )
                # Push down key/prefix filters when the client asks for a bounded subset
                # of keys instead of the ~40-prefix full boot sync.
                has_empty_prefix = any(
                    c.matching_type != Criterion.MatchingType.EXACT and not c.key
                    for c in criteria_list
                )
                if not has_empty_prefix and len(criteria_list) <= 25:
                    exact_keys = [
                        c.key
                        for c in criteria_list
                        if c.matching_type == Criterion.MatchingType.EXACT and c.key
                    ]
                    prefix_keys = [
                        c.key
                        for c in criteria_list
                        if c.matching_type != Criterion.MatchingType.EXACT and c.key
                    ]
                    conditions = []
                    if exact_keys:
                        conditions.append(PlayerStorageEntry.key.in_(exact_keys))
                    for prefix in prefix_keys:
                        conditions.append(PlayerStorageEntry.key.startswith(prefix))
                    if conditions:
                        stmt = stmt.where(or_(*conditions))

                result = await session.execute(stmt)
                db_entries = result.scalars().all()
                db_entry_map = {e.key: e for e in db_entries}

        response = player_storage_pb2.PlayerStorageGetEntriesV2.Response()
        response.revision = StorageRevisionManager.get_revision(current_pid)
        now = int(time.time())

        served_keys: set[str] = set()
        fallback_templates = self.template_entries

        for criterion in criteria_list:
            if criterion.matching_type == Criterion.MatchingType.EXACT:
                k = criterion.key
                # An empty stored value means the client cleared this key. Never
                # serve it: the game parses every returned value as its own
                # message and aborts the boot on a zero-length payload.
                if k in db_entry_map and db_entry_map[k].value:
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
                    if k.startswith(prefix) and k not in served_keys and e.value:
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

    @fail_internal_on_error("Failed to persist player storage entries")
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

            # Fold the client's per-copy card entries into one entry per card before
            # anything is written, so a ten-pull holding two copies of a card lands as
            # one collection entry whose duplicate counter says two. This is a fix, not a
            # policy: without it a collection grows one entry per drawn copy.
            card_plan = await _plan_card_merge(session, current_pid, request, now)

            written_keys: set[str] = set()
            rows_to_upsert: list[dict[str, Any]] = []
            keys_to_delete: list[str] = []

            if card_plan is not None:
                for key, value in card_plan.writes:
                    rows_to_upsert.append(
                        {
                            "player_id": current_pid,
                            "key": key,
                            "value": value,
                            "created_at": now,
                            "updated_at": now,
                        }
                    )
                    written_keys.add(key)

                if card_plan.drop_keys:
                    deleted = await session.execute(
                        delete(PlayerStorageEntry).where(
                            PlayerStorageEntry.player_id == current_pid,
                            PlayerStorageEntry.key.in_(card_plan.drop_keys),
                        )
                    )
                    logger.info(
                        "🎴 [Storage] Collapsed %d duplicate card entr(ies) into a counter "
                        "for player %s (cards=%s)",
                        deleted.rowcount or 0,
                        current_pid,
                        card_plan.collapsed,
                    )

            for proto_entry in request.entries:
                if proto_entry.key in written_keys or (
                    card_plan is not None and proto_entry.key in card_plan.drop_keys
                ):
                    # Already written (or dropped) as part of the card merge.
                    continue

                # A SetEntriesV2 entry with no value is the client clearing that
                # key (construction sites, inventory history, ...). Storing the
                # empty blob poisons every later GetEntriesV2, so delete instead.
                if not proto_entry.value:
                    keys_to_delete.append(proto_entry.key)
                    continue

                rows_to_upsert.append(
                    {
                        "player_id": current_pid,
                        "key": proto_entry.key,
                        "value": proto_entry.value,
                        "created_at": proto_entry.created_at or now,
                        "updated_at": now,
                    }
                )

            if keys_to_delete:
                await session.execute(
                    delete(PlayerStorageEntry).where(
                        PlayerStorageEntry.player_id == current_pid,
                        PlayerStorageEntry.key.in_(keys_to_delete),
                    )
                )

            if rows_to_upsert:
                await batch_upsert_storage_entries(session, rows_to_upsert)

            # Auto-grant expedition dropped building materials if passing tutorial
            if has_tut_10:
                mat_stmt = (
                    pg_insert(PlayerStorageEntry)
                    .values(
                        [
                            {
                                "player_id": current_pid,
                                "key": "ItemStockStatus--1000",
                                "value": bytes.fromhex("08e807108827"),  # 5000 wood
                                "created_at": now,
                                "updated_at": now,
                            },
                            {
                                "player_id": current_pid,
                                "key": "ItemStockStatus--2000",
                                "value": bytes.fromhex("08d00f108827"),  # 5000 stone
                                "created_at": now,
                                "updated_at": now,
                            },
                        ]
                    )
                    .on_conflict_do_nothing(
                        index_elements=[PlayerStorageEntry.player_id, PlayerStorageEntry.key]
                    )
                )
                await session.execute(mat_stmt)

            revision = await StorageRevisionManager.set_revision_and_save(
                request.next_revision, player_id=current_pid, session=session
            )
            await session.commit()

        response = player_storage_pb2.PlayerStorageSetEntriesV2.Response()
        for proto_entry in request.entries:
            resp_entry = response.entries.add()
            resp_entry.player_id = current_pid
            resp_entry.key = proto_entry.key
            resp_entry.value = proto_entry.value
            resp_entry.created_at = proto_entry.created_at or now
            resp_entry.updated_at = now

        response.revision = revision
        return response

    async def GetOtherPlayerEntriesV2(
        self,
        request: player_storage_pb2.PlayerStorageGetOtherPlayerEntriesV2.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_storage_pb2.PlayerStorageGetOtherPlayerEntriesV2.Response:
        response = player_storage_pb2.PlayerStorageGetOtherPlayerEntriesV2.Response()
        target_pids = list(request.player_ids)
        criteria_list = list(request.criteria)
        if not target_pids or not criteria_list:
            return response

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(PlayerStorageEntry).where(PlayerStorageEntry.player_id.in_(target_pids))
            has_empty_prefix = any(
                c.matching_type != Criterion.MatchingType.EXACT and not c.key
                for c in criteria_list
            )
            if not has_empty_prefix and len(criteria_list) <= 25:
                exact_keys = [
                    c.key
                    for c in criteria_list
                    if c.matching_type == Criterion.MatchingType.EXACT and c.key
                ]
                prefix_keys = [
                    c.key
                    for c in criteria_list
                    if c.matching_type != Criterion.MatchingType.EXACT and c.key
                ]
                conditions = []
                if exact_keys:
                    conditions.append(PlayerStorageEntry.key.in_(exact_keys))
                for prefix in prefix_keys:
                    conditions.append(PlayerStorageEntry.key.startswith(prefix))
                if conditions:
                    stmt = stmt.where(or_(*conditions))

            result = await session.execute(stmt)
            db_entries = result.scalars().all()
            db_entry_map = {(e.player_id, e.key): e for e in db_entries}

        served_pairs: set[tuple[str, str]] = set()
        for pid in target_pids:
            for criterion in criteria_list:
                if criterion.matching_type == Criterion.MatchingType.EXACT:
                    k = criterion.key
                    pair = (pid, k)
                    if (
                        pair in db_entry_map
                        and pair not in served_pairs
                        and db_entry_map[pair].value
                    ):
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
                        if (
                            entry_pid == pid
                            and k.startswith(prefix)
                            and pair not in served_pairs
                            and e.value
                        ):
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
