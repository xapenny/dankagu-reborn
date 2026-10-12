"""Cultivation, enhancement, limit break, and reconstruction materials utility."""

import json
import logging
import time
import uuid

from google.protobuf.internal.decoder import _DecodeVarint
from google.protobuf.internal.encoder import _EncodeVarint
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.config import settings
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_inventory.v1_pb2 import (
    ItemType,
    Route,
)
from dankagu.models.inventory import PlayerInventoryItem
from dankagu.models.storage import PlayerStorageEntry

logger = logging.getLogger("dankagu.core.growth")

# ---------------------------------------------------------------------------
# 1. Card Growth Materials (Prefix="Growth", GrowthItemStock--{id})
# ---------------------------------------------------------------------------
EXP_ITEM_IDS: list[int] = [30001, 30002, 30003, 30004, 30005]
BREAKTHROUGH_ITEM_IDS: list[int] = list(range(31001, 31016))
SKILL_ITEM_IDS: list[int] = [32003, 32004]
ALL_GROWTH_ITEM_IDS: list[int] = EXP_ITEM_IDS + BREAKTHROUGH_ITEM_IDS + SKILL_ITEM_IDS

# ---------------------------------------------------------------------------
# 2. Hakoniwa / Reconstruction Materials (Prefix="HakoniwaMaterial", prefix_id=14)
# ---------------------------------------------------------------------------
HAKONIWA_MATERIAL_IDS: list[int] = list(range(50001, 50014))

# ---------------------------------------------------------------------------
# 3. General Progression & Kaleidoscope Shards (Prefix="GenericItem", prefix_id=13)
# ---------------------------------------------------------------------------
KALEIDOSCOPE_SHARD_IDS: list[int] = [32005, 32006]
MUSIC_UNLOCK_TICKET_IDS: list[int] = [60021]

# ---------------------------------------------------------------------------
# 4. Stamina / Boost Recovery Items (Prefix="BoostHeal", prefix_id=12)
# ---------------------------------------------------------------------------
BOOST_HEAL_IDS: list[int] = [40002, 40003, 40004]

# ---------------------------------------------------------------------------
# 5. Heroine Cultivation Pieces (Prefix="HeroineFragment", prefix_id=11)
# ---------------------------------------------------------------------------
HEROINE_FRAGMENT_IDS: list[int] = [
    20000,
    20001,
    20002,
    20003,
    20005,
    20006,
    20008,
    20009,
    20010,
    20011,
    20013,
    20014,
    20015,
    20016,
    20017,
    20018,
    20019,
    20020,
    20021,
    20022,
    20023,
    20024,
    20025,
    20026,
    20027,
    20028,
    20029,
    20030,
    20031,
    20032,
    20033,
    20034,
    20037,
    20038,
    20039,
    20040,
    20041,
    20042,
    20043,
    20044,
    20045,
    20046,
    20049,
    20050,
    20051,
    20052,
    20053,
    20054,
    20055,
    20056,
    20058,
    20059,
    20060,
    20061,
    20062,
    20064,
    20065,
    20066,
    20067,
    20068,
    20069,
    20070,
    20072,
    20073,
    20074,
    20075,
    20076,
    20077,
    20079,
    20082,
    20083,
    20084,
    20085,
    20086,
    20087,
    20088,
    20089,
    20091,
    20093,
    20094,
    20095,
    20098,
    20106,
    20110,
    20111,
    20112,
    20113,
    20114,
    20155,
    20156,
    20157,
    20160,
    20161,
    20163,
    20803,
    20804,
    20805,
    29910,
]

_CULTIVATION_NAMES_CACHE: dict[str, dict[str, str]] | None = None


def _get_item_display_name(prefix: str, item_id: int) -> str:
    """Resolve localized item display name from data/metadata/cultivation_items.json if available."""
    global _CULTIVATION_NAMES_CACHE
    if _CULTIVATION_NAMES_CACHE is None:
        meta_path = settings.data_dir / "metadata" / "cultivation_items.json"
        if meta_path.exists():
            try:
                _CULTIVATION_NAMES_CACHE = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                _CULTIVATION_NAMES_CACHE = {}
        else:
            _CULTIVATION_NAMES_CACHE = {}

    cat_map = _CULTIVATION_NAMES_CACHE.get(prefix, {})
    return cat_map.get(str(item_id), f"{prefix} #{item_id}")


PREFIX_NAME_TO_ID: dict[str, int] = {
    "HeroineFragment": 11,
    "BoostHeal": 12,
    "GenericItem": 13,
    "HakoniwaMaterial": 14,
}


def encode_growth_item(item_id: int, count: int, now: int) -> bytes:
    """Encode GrowthItemStock protobuf binary payload.

    Field 1: item_id (varint)
    Field 2: count (varint)
    Field 4: updated_at (varint)
    """
    buf = bytearray()
    buf.append(0x08)
    _EncodeVarint(buf.extend, item_id)
    buf.append(0x10)
    _EncodeVarint(buf.extend, count)
    buf.append(0x20)
    _EncodeVarint(buf.extend, now)
    return bytes(buf)


def decode_growth_item(data: bytes) -> tuple[int, int]:
    """Decode GrowthItemStock or ItemStockStatus protobuf binary payload, returning (id, count)."""
    pos = 0
    item_id = 0
    count = 0
    while pos < len(data):
        tag, pos = _DecodeVarint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 7
        if wire_type == 0:  # varint
            val, pos = _DecodeVarint(data, pos)
            if field_num == 1:
                item_id = val
            elif field_num == 2:
                count = val
        else:
            break
    return item_id, count


def encode_item_stock_status(composite_id: int, count: int) -> bytes:
    """Encode ItemStockStatus protobuf binary payload.

    Field 1: ID = prefix_id * 1_000_000 + item_value (varint)
    Field 2: Count (varint)
    """
    buf = bytearray()
    buf.append(0x08)
    _EncodeVarint(buf.extend, composite_id)
    buf.append(0x10)
    _EncodeVarint(buf.extend, count)
    return bytes(buf)


async def add_growth_items(
    session: AsyncSession,
    player_id: str,
    item_ids: list[int],
    count: int = 500,
    max_cap: int | None = None,
) -> None:
    """Add growth materials directly to player's PlayerStorageEntry records."""
    now = int(time.time())
    keys = [f"GrowthItemStock--{iid}" for iid in item_ids]

    stmt = select(PlayerStorageEntry).where(
        PlayerStorageEntry.player_id == player_id,
        PlayerStorageEntry.key.in_(keys),
    )
    res = await session.execute(stmt)
    existing_map = {entry.key: entry for entry in res.scalars().all()}

    for iid in item_ids:
        key = f"GrowthItemStock--{iid}"
        existing = existing_map.get(key)
        new_count = count
        if existing and existing.value:
            try:
                _, cur_count = decode_growth_item(existing.value)
                new_count = cur_count + count
            except Exception:
                pass
        if max_cap is not None and new_count > max_cap:
            new_count = max_cap

        val_bytes = encode_growth_item(iid, new_count, now)
        upsert_stmt = pg_insert(PlayerStorageEntry).values(
            player_id=player_id,
            key=key,
            value=val_bytes,
            created_at=now,
            updated_at=now,
        )
        upsert_stmt = upsert_stmt.on_conflict_do_update(
            index_elements=[PlayerStorageEntry.player_id, PlayerStorageEntry.key],
            set_={
                "value": upsert_stmt.excluded.value,
                "updated_at": upsert_stmt.excluded.updated_at,
            },
        )
        await session.execute(upsert_stmt)


async def add_generic_item_stocks(
    session: AsyncSession,
    player_id: str,
    prefix_id: int,
    item_ids: list[int],
    count: int = 200,
) -> None:
    """Add GenericItemStockStatus entries (ItemStockStatus--{prefix_id * 1_000_000 + id})."""
    now = int(time.time())
    composite_ids = [prefix_id * 1_000_000 + iid for iid in item_ids]
    keys = [f"ItemStockStatus--{cid}" for cid in composite_ids]

    stmt = select(PlayerStorageEntry).where(
        PlayerStorageEntry.player_id == player_id,
        PlayerStorageEntry.key.in_(keys),
    )
    res = await session.execute(stmt)
    existing_map = {entry.key: entry for entry in res.scalars().all()}

    for cid in composite_ids:
        key = f"ItemStockStatus--{cid}"
        existing = existing_map.get(key)
        new_count = count
        if existing and existing.value:
            try:
                _, cur_count = decode_growth_item(existing.value)
                new_count = cur_count + count
            except Exception:
                pass

        val_bytes = encode_item_stock_status(cid, new_count)
        upsert_stmt = pg_insert(PlayerStorageEntry).values(
            player_id=player_id,
            key=key,
            value=val_bytes,
            created_at=now,
            updated_at=now,
        )
        upsert_stmt = upsert_stmt.on_conflict_do_update(
            index_elements=[PlayerStorageEntry.player_id, PlayerStorageEntry.key],
            set_={
                "value": upsert_stmt.excluded.value,
                "updated_at": upsert_stmt.excluded.updated_at,
            },
        )
        await session.execute(upsert_stmt)


def _make_mail_item(
    player_id: str,
    prefix: str,
    item_id: int,
    count: int,
    message: str,
    now: int,
    *,
    schema_key: str = "ITEM",
    item_type: int = int(ItemType.GAME_RUNTIME),
    search_label: str = "Item",
    system_resource_name: str = "",
) -> PlayerInventoryItem:
    """Build a client-compliant PlayerInventoryItem compatible with AppItemInfoBuilder.Build."""
    return PlayerInventoryItem(
        id=str(uuid.uuid4()),
        player_id=player_id,
        item_type=item_type,
        schema_key=schema_key,
        value=json.dumps({"Prefix": prefix, "Value": item_id, "Count": count}).encode("utf-8"),
        route=int(Route.LOGIN_BONUS),
        message=message,
        search_label=search_label,
        opened_at=now,
        expired_at=now + 86400 * 90,
        system_resource_name=system_resource_name or f"{prefix}_{item_id}",
        system_resource_num=count,
        is_claimed=False,
        created_at=now,
    )


def create_daily_reward_mail_items(
    player_id: str,
    total_login_days: int,
    *,
    growth_count: int = 500,
    music_ticket_count: int = 300,
    hakoniwa_count: int = 200,
    boost_heal_count: int = 10,
    universal_fragment_count: int = 10000,
    coin_amount: int = 1000000,
) -> list[PlayerInventoryItem]:
    """Generate daily login reward mail items:
    - Mitama Card Growth Materials: 500 each (5 EXP orbs + 15 Blossom materials + 2 Kaleidoscope shards)
    - Music Unlock Ticket: 300
    - Hakoniwa / Reconstruction Materials: 200 each (13 types)
    - Boost Recovery Items: 10 each (3 types)
    - Universal Heroine Fragment: 10000
    - Coins: 1,000,000
    """
    now = int(time.time())
    items: list[PlayerInventoryItem] = []
    day_prefix = f"【Reborn 育成支援・{total_login_days}日目】"

    # 1. Card Growth Materials (500 each)
    for iid in EXP_ITEM_IDS + BREAKTHROUGH_ITEM_IDS:
        name = _get_item_display_name("Growth", iid)
        items.append(
            _make_mail_item(
                player_id=player_id,
                prefix="Growth",
                item_id=iid,
                count=growth_count,
                message=f"{day_prefix}{name} x{growth_count}",
                now=now,
            )
        )

    for iid in KALEIDOSCOPE_SHARD_IDS:
        name = _get_item_display_name("GenericItem", iid)
        items.append(
            _make_mail_item(
                player_id=player_id,
                prefix="GenericItem",
                item_id=iid,
                count=growth_count,
                message=f"{day_prefix}{name} x{growth_count}",
                now=now,
            )
        )

    # 2. Music Unlock Tickets (300)
    for iid in MUSIC_UNLOCK_TICKET_IDS:
        name = _get_item_display_name("GenericItem", iid)
        items.append(
            _make_mail_item(
                player_id=player_id,
                prefix="GenericItem",
                item_id=iid,
                count=music_ticket_count,
                message=f"{day_prefix}{name} x{music_ticket_count}",
                now=now,
            )
        )

    # 3. Hakoniwa / Reconstruction Materials (200 each)
    for iid in HAKONIWA_MATERIAL_IDS:
        name = _get_item_display_name("HakoniwaMaterial", iid)
        items.append(
            _make_mail_item(
                player_id=player_id,
                prefix="HakoniwaMaterial",
                item_id=iid,
                count=hakoniwa_count,
                message=f"{day_prefix}{name} x{hakoniwa_count}",
                now=now,
            )
        )

    # 4. Boost Recovery Items (10 each)
    for iid in BOOST_HEAL_IDS:
        name = _get_item_display_name("BoostHeal", iid)
        items.append(
            _make_mail_item(
                player_id=player_id,
                prefix="BoostHeal",
                item_id=iid,
                count=boost_heal_count,
                message=f"{day_prefix}{name} x{boost_heal_count}",
                now=now,
            )
        )

    # 5. Universal Heroine Fragment (200)
    frag_name = _get_item_display_name("HeroineFragment", 20000)
    items.append(
        _make_mail_item(
            player_id=player_id,
            prefix="HeroineFragment",
            item_id=20000,
            count=universal_fragment_count,
            message=f"{day_prefix}{frag_name} x{universal_fragment_count}",
            now=now,
        )
    )

    # 6. Coins (1,000,000)
    coin_name = _get_item_display_name("Coin", 10001)
    items.append(
        _make_mail_item(
            player_id=player_id,
            prefix="Coin",
            item_id=10001,
            count=coin_amount,
            message=f"{day_prefix}{coin_name} x{coin_amount:,}",
            now=now,
            search_label="Coin",
        )
    )

    return items
