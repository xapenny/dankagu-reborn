"""Takasho PlayerStorage servicer for player state persistence and retrieval."""

import logging
import time
from typing import Any

import grpc
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.core.database import get_sessionmaker
from dankagu.core.ranking_rewards import (
    check_and_grant_event_ranking_milestones,
    check_and_grant_gpx_milestones_for_player,
    extract_event_info_status,
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
from dankagu.models.ranking import EventLeaderboard
from dankagu.models.storage import PlayerStorageEntry
from dankagu.services.box_loot_box import encode_raw_protobuf, parse_raw_protobuf

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
    "ShopProductPurchaseHistory--",
    "PKVSStatus--",
    "GachaTicketExchangeStatus--",
    "PlayerKeyValue:",
    "SelectTicket--",
    "UnlockEntityStatusCollection--",
    "DanmakuPassSeasonStatus-",
    "EventEmblemRankingStatusCollection--",
    "EventRaidAttackStatus--",
    "SaveHash--",
)


async def batch_upsert_storage_entries(session: AsyncSession, rows: list[dict[str, Any]]) -> None:
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


def add_unlock_ids_to_collection(
    current_bytes: bytes | None, category: int, unlock_ids: list[int], now: int
) -> bytes:
    """Add unlock IDs to an UnlockEntityStatusCollection--{category} protobuf blob."""
    existing_ids: set[int] = set()
    sub_messages = []

    if current_bytes:
        try:
            fields = parse_raw_protobuf(current_bytes)
            for _, sub_raw in fields.get(2, []):
                sub = parse_raw_protobuf(sub_raw)
                uid = sub.get(4, [(0, 0)])[0][1]
                existing_ids.add(uid)
                sub_messages.append((2, sub_raw))
        except Exception as e:
            logger.debug(
                "Failed parsing existing UnlockEntityStatusCollection--%d: %s", category, e
            )

    added = False
    for uid in unlock_ids:
        if uid not in existing_ids:
            new_sub = {
                1: [(0, now)],
                2: [(0, now)],
                3: [(0, category)],
                4: [(0, uid)],
                6: [(0, 1)],
            }
            sub_messages.append((2, encode_raw_protobuf(new_sub)))
            existing_ids.add(uid)
            added = True

    if not added and current_bytes:
        return current_bytes

    root = {
        1: [(0, category)],
        2: sub_messages,
    }
    return encode_raw_protobuf(root)


def add_emblems_to_collection(
    current_bytes: bytes | None, emblem_ids: list[int], now: int
) -> bytes:
    """Add emblem IDs to an UnlockEntityStatusCollection--8 protobuf blob."""
    return add_unlock_ids_to_collection(current_bytes, 8, emblem_ids, now)


def parse_event_emblem_rankings(blob: bytes | None) -> dict[int, int]:
    """Parse EventEmblemRankingStatusCollection--1 protobuf blob into {emblem_id: rank}."""
    if not blob:
        return {}
    rankings: dict[int, int] = {}
    try:
        fields = parse_raw_protobuf(blob)
        for _, sub_raw in fields.get(2, []):
            if isinstance(sub_raw, (bytes, bytearray)):
                sub = parse_raw_protobuf(sub_raw)
                eid_list = sub.get(1, [])
                rank_list = sub.get(2, [])
                if eid_list and rank_list:
                    rankings[eid_list[0][1]] = rank_list[0][1]
    except Exception:
        pass
    return rankings


def encode_event_emblem_rankings(rankings: dict[int, int], coll_id: int = 1) -> bytes:
    """Encode {emblem_id: rank} into EventEmblemRankingStatusCollection--1 protobuf blob."""
    sub_msgs = []
    for eid, rank in sorted(rankings.items()):
        sub_raw = encode_raw_protobuf(
            {
                1: [(0, eid)],
                2: [(0, rank)],
            }
        )
        sub_msgs.append((2, sub_raw))
    return encode_raw_protobuf(
        {
            1: [(0, coll_id)],
            2: sub_msgs,
        }
    )


async def add_event_emblem_rankings_to_storage(
    session: AsyncSession,
    player_id: str,
    emblem_rankings: dict[int, int],
    now: int,
) -> PlayerStorageEntry | None:
    """Add or update event emblem rankings in EventEmblemRankingStatusCollection--1."""
    if not emblem_rankings:
        return None

    stmt = select(PlayerStorageEntry).where(
        PlayerStorageEntry.player_id == player_id,
        PlayerStorageEntry.key == "EventEmblemRankingStatusCollection--1",
    )
    entry = (await session.execute(stmt)).scalar_one_or_none()
    curr_blob = entry.value if entry else None
    existing = parse_event_emblem_rankings(curr_blob)

    updated = False
    for eid, r in emblem_rankings.items():
        if r > 0:
            best_rank = r
            if eid in existing and existing[eid] > 0:
                best_rank = min(existing[eid], r)
            if eid not in existing or existing[eid] != best_rank:
                existing[eid] = best_rank
                updated = True

    if not updated and entry:
        return entry

    new_blob = encode_event_emblem_rankings(existing, coll_id=1)
    if entry:
        entry.value = new_blob
        entry.updated_at = now
        return entry
    else:
        new_entry = PlayerStorageEntry(
            player_id=player_id,
            key="EventEmblemRankingStatusCollection--1",
            value=new_blob,
            created_at=now,
            updated_at=now,
        )
        session.add(new_entry)
        return new_entry


def update_player_base_emblem_intvals(
    blob: bytes | None, emblem_rankings: dict[int, int]
) -> tuple[bytes | None, bool]:
    """Update Emblem1IntVal (Tag 18) and Emblem2IntVal (Tag 19) in PlayerBase--1 if equipped."""
    if not blob or not emblem_rankings:
        return blob, False
    try:
        fields = parse_raw_protobuf(blob)
        e1_id = fields.get(5, [(0, 0)])[0][1] if 5 in fields else 0
        e2_id = fields.get(6, [(0, 0)])[0][1] if 6 in fields else 0
        changed = False
        if e1_id in emblem_rankings:
            target_val = emblem_rankings[e1_id]
            curr_val = fields.get(18, [(0, 0)])[0][1] if 18 in fields else 0
            if curr_val != target_val:
                fields[18] = [(0, target_val)]
                changed = True
        if e2_id in emblem_rankings:
            target_val = emblem_rankings[e2_id]
            curr_val = fields.get(19, [(0, 0)])[0][1] if 19 in fields else 0
            if curr_val != target_val:
                fields[19] = [(0, target_val)]
                changed = True
        if changed:
            return encode_raw_protobuf(fields), True
    except Exception:
        pass
    return blob, False


async def check_and_grant_heroine_milestones(
    session: AsyncSession, player_id: str, entries: Any, now: int
) -> PlayerStorageEntry | None:
    """Auto-grant heroine milestone titles (Lv 50: 1000000+cid, Lv 100: 1100000+cid).

    Ensures UnlockEntityStatusCollection--8 contains the unlocked titles.
    Titles are NOT queued to player_inventories because titles have MaxCount=1 and
    the client's EmblemProxy.CheckFully checks haveCount + addCount > 1, which blocks
    claiming if already unlocked.
    """
    emblems_to_grant: set[int] = set()
    has_unlock_8_in_entries = False

    for entry in entries:
        k = entry.key
        v = entry.value
        if not v:
            continue
        if k == "UnlockEntityStatusCollection--8":
            has_unlock_8_in_entries = True
        elif k.startswith("HeroineStatus--"):
            try:
                fields = parse_raw_protobuf(v)
                chara_id = fields.get(2, [(0, 0)])[0][1]
                exp = fields.get(8, [(0, 0)])[0][1]
                if chara_id > 0:
                    if exp >= 49:
                        emblems_to_grant.add(1000000 + chara_id)
                    if exp >= 99:
                        emblems_to_grant.add(1100000 + chara_id)
            except Exception as e:
                logger.debug("Failed parsing HeroineStatus: %s", e)
        elif k.startswith("CharacterStatus--"):
            try:
                fields = parse_raw_protobuf(v)
                chara_id = fields.get(1, [(0, 0)])[0][1]
                exp = fields.get(4, [(0, 0)])[0][1]
                if chara_id > 0:
                    if exp >= 49:
                        emblems_to_grant.add(1000000 + chara_id)
                    if exp >= 99:
                        emblems_to_grant.add(1100000 + chara_id)
            except Exception as e:
                logger.debug("Failed parsing CharacterStatus: %s", e)

    if not emblems_to_grant:
        return None

    # If client did not provide UnlockEntityStatusCollection--8 in this request, update it on server
    if not has_unlock_8_in_entries:
        stmt_u8 = select(PlayerStorageEntry).where(
            PlayerStorageEntry.player_id == player_id,
            PlayerStorageEntry.key == "UnlockEntityStatusCollection--8",
        )
        curr_u8 = (await session.execute(stmt_u8)).scalar_one_or_none()
        curr_val = curr_u8.value if curr_u8 else None
        updated_val = add_emblems_to_collection(curr_val, list(emblems_to_grant), now)
        if curr_u8:
            if curr_u8.value != updated_val:
                curr_u8.value = updated_val
                curr_u8.updated_at = now
            return curr_u8
        else:
            new_u8 = PlayerStorageEntry(
                player_id=player_id,
                key="UnlockEntityStatusCollection--8",
                value=updated_val,
                created_at=now,
                updated_at=now,
            )
            session.add(new_u8)
            return new_u8

    return None


def sanitize_gpx_info_status(raw_bytes: bytes) -> tuple[bytes, dict[int, int]]:
    """Sanitize GpxInfoStatusProto to eliminate poisoned legacy backup scores (15M+).

    Returns (sanitized_bytes, dict_of_phase_id_to_total_score).
    """
    if not raw_bytes:
        return raw_bytes, {}

    try:
        from google.protobuf.internal import decoder, encoder

        def parse_fields(data: bytes):
            pos = 0
            fields = []
            while pos < len(data):
                tag, pos = decoder._DecodeVarint(data, pos)
                fn = tag >> 3
                wt = tag & 7
                if wt == 0:
                    val, pos = decoder._DecodeVarint(data, pos)
                    fields.append((fn, wt, val))
                elif wt == 2:
                    length, pos = decoder._DecodeVarint(data, pos)
                    val = data[pos : pos + length]
                    pos += length
                    fields.append((fn, wt, val))
                else:
                    break
            return fields

        root_fields = parse_fields(raw_bytes)
        out = bytearray()
        phase_scores = {}

        for fn, wt, val in root_fields:
            if fn == 2 and wt == 2:  # Phase sub-message
                p_fields = parse_fields(val)
                phase_id = 0
                is_clear = b"\x00\x00\x00"
                max_scores = [0, 0, 0]
                scores = [0, 0, 0]
                max_total_score = 0
                receive_reward = 0

                for pfn, _pwt, pval in p_fields:
                    if pfn == 1:
                        phase_id = pval
                    elif pfn == 2:
                        is_clear = pval
                    elif pfn == 3:
                        pos = 0
                        ms = []
                        while pos < len(pval):
                            v, pos = decoder._DecodeVarint(pval, pos)
                            ms.append(v)
                        if ms:
                            max_scores = ms
                    elif pfn == 4:
                        pos = 0
                        sc = []
                        while pos < len(pval):
                            v, pos = decoder._DecodeVarint(pval, pos)
                            sc.append(v)
                        if sc:
                            scores = sc
                    elif pfn == 5:
                        max_total_score = pval
                    elif pfn == 6:
                        receive_reward = pval

                total_score = sum(scores)
                if max_total_score >= 10_000_000:
                    max_total_score = total_score
                    max_scores = list(scores)
                elif total_score > max_total_score:
                    max_total_score = total_score
                    max_scores = list(scores)

                phase_scores[phase_id] = total_score

                # Re-encode phase
                p_out = bytearray()
                p_out += encoder._VarintBytes((1 << 3) | 0) + encoder._VarintBytes(phase_id)
                p_out += (
                    encoder._VarintBytes((2 << 3) | 2)
                    + encoder._VarintBytes(len(is_clear))
                    + is_clear
                )
                ms_buf = bytearray()
                for s in max_scores:
                    ms_buf += encoder._VarintBytes(s)
                p_out += (
                    encoder._VarintBytes((3 << 3) | 2) + encoder._VarintBytes(len(ms_buf)) + ms_buf
                )
                sc_buf = bytearray()
                for s in scores:
                    sc_buf += encoder._VarintBytes(s)
                p_out += (
                    encoder._VarintBytes((4 << 3) | 2) + encoder._VarintBytes(len(sc_buf)) + sc_buf
                )
                if max_total_score > 0:
                    p_out += encoder._VarintBytes((5 << 3) | 0) + encoder._VarintBytes(
                        max_total_score
                    )
                if receive_reward > 0:
                    p_out += encoder._VarintBytes((6 << 3) | 0) + encoder._VarintBytes(
                        receive_reward
                    )

                out += (
                    encoder._VarintBytes((2 << 3) | 2)
                    + encoder._VarintBytes(len(p_out))
                    + bytes(p_out)
                )
            elif wt == 0:
                out += encoder._VarintBytes((fn << 3) | 0) + encoder._VarintBytes(val)
            elif wt == 2:
                out += encoder._VarintBytes((fn << 3) | 2) + encoder._VarintBytes(len(val)) + val

        return bytes(out), phase_scores
    except Exception as e:
        logger.debug("Failed parsing GpxInfoStatusProto in sanitizer: %s", e)
        return raw_bytes, {}


class PlayerStorageService(player_storage_pb2_grpc.PlayerStorageServicer):
    """Handles GetEntriesV2 and SetEntriesV2 for game state persistence."""

    def __init__(self) -> None:
        self.template_entries: dict[str, bytes] = {}
        self._templates_loaded = False

    async def _ensure_templates_loaded(self) -> None:
        if self._templates_loaded:
            return

        from dankagu.core.metadata import metadata

        try:
            default_map = metadata.get_default_storage()
            for k, val_bytes in default_map.items():
                if k.startswith(PERSONAL_STORAGE_PREFIXES):
                    continue
                self.template_entries[k] = val_bytes
            logger.info(
                "💾 Loaded %d shared storage templates from metadata",
                len(self.template_entries),
            )
        except Exception as e:
            logger.error("Failed to load default storage templates from metadata: %s", e)

        # Baseline entries used when no operator-supplied template covers them
        self.template_entries.setdefault(
            "PlayerBootStatus--1", b"\x08\x01\x18\x01 \x0e(\xff\xff\xff\xff\x07"
        )
        # Seed all 16 UnlockEntityStatusCollection categories (Card, Heroine, Character,
        # Map, MapGrid, Lunatic, CardIllust, Emblem, DanmakuPass, HomeCard, ClubStamp,
        # HeroineGrowth, ExpeditionCount, AnotherMusic, FirstBurstLink, SdSkin)
        for cat_id in range(1, 17):
            self.template_entries.setdefault(
                f"UnlockEntityStatusCollection--{cat_id}", bytes([0x08, cat_id])
            )
        # Seed all EpisodeEntityStatusCollection categories (1, 2, 3: Chara, 4, 5, 6, 10)
        for ep_cat in (1, 2, 3, 4, 5, 6, 10):
            self.template_entries.setdefault(
                f"EpisodeEntityStatusCollection--{ep_cat}", bytes([0x08, ep_cat])
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
        # Clean GpxInfoStatusProto: ID=1, 5 phases (1001000..1005000), maxTotalScore=0, scores=[0,0,0]
        # This ensures real gameplay scores trigger GpxService.RegistRanking -> RegisterScore on client.
        gpx_info_val = bytes.fromhex(
            "0801121308a88c3d12030000001a030000002203000000"
            "12130890943d12030000001a030000002203000000"
            "121308f89b3d12030000001a030000002203000000"
            "121308e0a33d12030000001a030000002203000000"
            "121308c8ab3d12030000001a030000002203000000"
            "28013001380440c8ab3d48b4ba939a065001580162006800"
        )
        self.template_entries.setdefault("GpxInfoStatus-V1-1", gpx_info_val)
        self.template_entries.setdefault("GpxInfoStatus--1", gpx_info_val)
        self.template_entries.setdefault("GpxEntryStatus-V1-1", b"\x08\x01\x10\x01")
        self.template_entries.setdefault("GpxEntryStatus--1", b"\x08\x01\x10\x01")
        self.template_entries.setdefault(
            "ExpeditionMissionRecordEntityCollecttion--1",
            bytes.fromhex("0801121c08914e100018002000280030003a0200004205000000000048015000"),
        )
        self.template_entries.setdefault(
            "ExpeditionManageStatus--1",
            bytes.fromhex("080110914e18002000"),
        )
        self.template_entries.setdefault(
            "EventInfoStatus-V32-32",
            bytes.fromhex("08201000180032040101010138ca84d1d60658ca84d1d60670037803"),
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
                stmt = select(PlayerStorageEntry).where(PlayerStorageEntry.player_id == current_pid)
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

                now = int(time.time())
                needs_emblem_rankings = any(
                    c.key.startswith("EventEmblemRankingStatusCollection--")
                    or (
                        c.matching_type == Criterion.MatchingType.EXACT
                        and c.key == "EventEmblemRankingStatusCollection--1"
                    )
                    for c in criteria_list
                )
                needs_player_base = any(
                    c.key.startswith("PlayerBase--")
                    or (
                        c.matching_type == Criterion.MatchingType.EXACT and c.key == "PlayerBase--1"
                    )
                    for c in criteria_list
                )

                if needs_emblem_rankings or needs_player_base:
                    u_key = "UnlockEntityStatusCollection--8"
                    u_entry = db_entry_map.get(u_key)
                    if not u_entry:
                        u_res = await session.execute(
                            select(PlayerStorageEntry).where(
                                PlayerStorageEntry.player_id == current_pid,
                                PlayerStorageEntry.key == u_key,
                            )
                        )
                        u_entry = u_res.scalar_one_or_none()

                    from dankagu.core.ranking_rewards import (
                        SCORE38_EMBLEM_MAP,
                        extract_unlocked_emblem_ids,
                        resolve_event_emblem_rank,
                    )

                    unlocked_eids = extract_unlocked_emblem_ids(u_entry.value if u_entry else None)
                    event_eids = [
                        eid
                        for eid in unlocked_eids
                        if eid in SCORE38_EMBLEM_MAP or (3_000_000 <= eid < 4_000_000)
                    ]

                    existing_coll = db_entry_map.get("EventEmblemRankingStatusCollection--1")
                    if not existing_coll:
                        c_res = await session.execute(
                            select(PlayerStorageEntry).where(
                                PlayerStorageEntry.player_id == current_pid,
                                PlayerStorageEntry.key == "EventEmblemRankingStatusCollection--1",
                            )
                        )
                        existing_coll = c_res.scalar_one_or_none()
                        if existing_coll:
                            db_entry_map[existing_coll.key] = existing_coll

                    rankings = parse_event_emblem_rankings(
                        existing_coll.value if existing_coll else None
                    )
                    missing_eids = [eid for eid in event_eids if eid not in rankings]

                    if missing_eids:
                        new_rankings = {}
                        for eid in missing_eids:
                            ev_id = (
                                (eid - 3_000_000) // 100 if (3_000_000 <= eid < 4_000_000) else 38
                            )
                            sc_key = f"score{ev_id}"
                            lb_stmt = select(EventLeaderboard).where(
                                EventLeaderboard.ranking_event_id == sc_key,
                                EventLeaderboard.player_id == current_pid,
                            )
                            lb_entry = (await session.execute(lb_stmt)).scalars().first()
                            actual_rank = 0
                            if lb_entry:
                                r_stmt = (
                                    select(func.count())
                                    .select_from(EventLeaderboard)
                                    .where(
                                        EventLeaderboard.ranking_event_id == sc_key,
                                        EventLeaderboard.score > lb_entry.score,
                                    )
                                )
                                actual_rank = ((await session.execute(r_stmt)).scalar() or 0) + 1

                            new_rankings[eid] = resolve_event_emblem_rank(eid, actual_rank)

                        saved_coll = await add_event_emblem_rankings_to_storage(
                            session, current_pid, new_rankings, now
                        )
                        if saved_coll:
                            await session.commit()
                            db_entry_map[saved_coll.key] = saved_coll
                            rankings.update(new_rankings)

                    pb_entry = db_entry_map.get("PlayerBase--1")
                    if not pb_entry and needs_player_base:
                        pb_res = await session.execute(
                            select(PlayerStorageEntry).where(
                                PlayerStorageEntry.player_id == current_pid,
                                PlayerStorageEntry.key == "PlayerBase--1",
                            )
                        )
                        pb_entry = pb_res.scalar_one_or_none()
                        if pb_entry:
                            db_entry_map[pb_entry.key] = pb_entry

                    if pb_entry and pb_entry.value and rankings:
                        new_pb, pb_changed = update_player_base_emblem_intvals(
                            pb_entry.value, rankings
                        )
                        if pb_changed:
                            pb_entry.value = new_pb
                            pb_entry.updated_at = now
                            await session.commit()

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
                    if k.startswith("GpxInfoStatus"):
                        proto.value, _ = sanitize_gpx_info_status(proto.value)
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
                matching_entries = [
                    (k, e)
                    for k, e in db_entry_map.items()
                    if k.startswith(prefix) and k not in served_keys and e.value
                ]

                for k, e in matching_entries:
                    proto = response.entries.add()
                    proto.player_id = current_pid
                    proto.key = e.key
                    proto.value = e.value
                    if k.startswith("GpxInfoStatus"):
                        proto.value, _ = sanitize_gpx_info_status(proto.value)
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

            rows_to_upsert: list[dict[str, Any]] = []
            keys_to_delete: list[str] = []

            for proto_entry in request.entries:
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

            # Sanitize GpxInfoStatus and auto-sync medley ranking scores
            clean_gpx_val = None
            gpx_phase_scores: dict[int, int] = {}
            for row in rows_to_upsert:
                if row["key"].startswith("GpxInfoStatus"):
                    clean_val, p_scores = sanitize_gpx_info_status(row["value"])
                    row["value"] = clean_val
                    clean_gpx_val = clean_val
                    gpx_phase_scores.update(p_scores)

            p5_score = gpx_phase_scores.get(1005000, 0)
            if p5_score > 0:
                ranking_key = "gpx_s1_p1005"
                lb_stmt = select(EventLeaderboard).where(
                    EventLeaderboard.ranking_event_id == ranking_key,
                    EventLeaderboard.player_id == current_pid,
                )
                lb_entry = (await session.execute(lb_stmt)).scalars().first()
                if lb_entry is None:
                    session.add(
                        EventLeaderboard(
                            ranking_event_id=ranking_key,
                            player_id=current_pid,
                            score=p5_score,
                            updated_at=now,
                        )
                    )
                elif p5_score > lb_entry.score:
                    lb_entry.score = p5_score
                    lb_entry.updated_at = now

                # Grant GPX milestones immediately upon achieving new peak score/rank
                await check_and_grant_gpx_milestones_for_player(
                    session,
                    player_id=current_pid,
                    score=p5_score,
                    gpx_info_bytes=clean_gpx_val,
                    ranking_key=ranking_key,
                )

            # Auto-sync event point and event score rankings from EventInfoStatus
            for row in rows_to_upsert:
                if row["key"].startswith("EventInfoStatus"):
                    ev_id, ev_pt, ev_max_score = extract_event_info_status(row["value"])
                    if ev_pt > 0:
                        pt_key = f"point{ev_id}"
                        pt_stmt = select(EventLeaderboard).where(
                            EventLeaderboard.ranking_event_id == pt_key,
                            EventLeaderboard.player_id == current_pid,
                        )
                        pt_entry = (await session.execute(pt_stmt)).scalars().first()
                        if pt_entry is None:
                            session.add(
                                EventLeaderboard(
                                    ranking_event_id=pt_key,
                                    player_id=current_pid,
                                    score=ev_pt,
                                    updated_at=now,
                                )
                            )
                        elif ev_pt > pt_entry.score:
                            pt_entry.score = ev_pt
                            pt_entry.updated_at = now

                        rank_stmt = (
                            select(func.count())
                            .select_from(EventLeaderboard)
                            .where(
                                EventLeaderboard.ranking_event_id == pt_key,
                                EventLeaderboard.score > ev_pt,
                            )
                        )
                        higher_count = (await session.execute(rank_stmt)).scalar() or 0
                        rank = higher_count + 1
                        await check_and_grant_event_ranking_milestones(
                            session, current_pid, pt_key, rank
                        )

                    if ev_max_score > 0:
                        sc_key = f"score{ev_id}"
                        sc_stmt = select(EventLeaderboard).where(
                            EventLeaderboard.ranking_event_id == sc_key,
                            EventLeaderboard.player_id == current_pid,
                        )
                        sc_entry = (await session.execute(sc_stmt)).scalars().first()
                        if sc_entry is None:
                            session.add(
                                EventLeaderboard(
                                    ranking_event_id=sc_key,
                                    player_id=current_pid,
                                    score=ev_max_score,
                                    updated_at=now,
                                )
                            )
                        elif ev_max_score > sc_entry.score:
                            sc_entry.score = ev_max_score
                            sc_entry.updated_at = now

                        rank_stmt = (
                            select(func.count())
                            .select_from(EventLeaderboard)
                            .where(
                                EventLeaderboard.ranking_event_id == sc_key,
                                EventLeaderboard.score > ev_max_score,
                            )
                        )
                        higher_count = (await session.execute(rank_stmt)).scalar() or 0
                        rank = higher_count + 1
                        await check_and_grant_event_ranking_milestones(
                            session, current_pid, sc_key, rank
                        )

            # Ensure equipped event emblem ranking numbers are preserved in PlayerBase--1
            coll_row = next(
                (r for r in rows_to_upsert if r["key"] == "EventEmblemRankingStatusCollection--1"),
                None,
            )
            pb_row = next((r for r in rows_to_upsert if r["key"] == "PlayerBase--1"), None)

            rankings_for_pb: dict[int, int] = {}
            if coll_row and coll_row["value"]:
                rankings_for_pb.update(parse_event_emblem_rankings(coll_row["value"]))

            if pb_row and pb_row["value"]:
                if not rankings_for_pb:
                    r_blob = (
                        await session.execute(
                            select(PlayerStorageEntry.value).where(
                                PlayerStorageEntry.player_id == current_pid,
                                PlayerStorageEntry.key == "EventEmblemRankingStatusCollection--1",
                            )
                        )
                    ).scalar_one_or_none()
                    rankings_for_pb.update(parse_event_emblem_rankings(r_blob))

                if rankings_for_pb:
                    new_pb, pb_changed = update_player_base_emblem_intvals(
                        pb_row["value"], rankings_for_pb
                    )
                    if pb_changed:
                        pb_row["value"] = new_pb
            elif rankings_for_pb:
                pb_db = (
                    await session.execute(
                        select(PlayerStorageEntry).where(
                            PlayerStorageEntry.player_id == current_pid,
                            PlayerStorageEntry.key == "PlayerBase--1",
                        )
                    )
                ).scalar_one_or_none()
                if pb_db and pb_db.value:
                    new_pb, pb_changed = update_player_base_emblem_intvals(
                        pb_db.value, rankings_for_pb
                    )
                    if pb_changed:
                        pb_db.value = new_pb
                        pb_db.updated_at = now

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

            # Auto-grant heroine & character milestone titles (Lv 50 / Lv 100)
            u8_entry = await check_and_grant_heroine_milestones(
                session, current_pid, request.entries, now
            )

            revision = await StorageRevisionManager.set_revision_and_save(
                request.next_revision, player_id=current_pid, session=session
            )
            await session.commit()

        response = player_storage_pb2.PlayerStorageSetEntriesV2.Response()
        written_map = {row["key"]: row for row in rows_to_upsert}
        if u8_entry and u8_entry.key not in written_map:
            written_map[u8_entry.key] = {
                "key": u8_entry.key,
                "value": u8_entry.value,
                "created_at": u8_entry.created_at,
                "updated_at": u8_entry.updated_at,
            }

        for key, row in written_map.items():
            resp_entry = response.entries.add()
            resp_entry.player_id = current_pid
            resp_entry.key = key

            resp_entry.value = row["value"]

            resp_entry.created_at = row["created_at"]

            resp_entry.updated_at = row["updated_at"]

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
                c.matching_type != Criterion.MatchingType.EXACT and not c.key for c in criteria_list
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
