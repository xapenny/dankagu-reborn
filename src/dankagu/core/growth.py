"""Character growth, enhancement, and limit break materials utility."""

import json
import logging
import time
import uuid

from google.protobuf.internal.decoder import _DecodeVarint
from google.protobuf.internal.encoder import _EncodeVarint
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_inventory.v1_pb2 import (
    ItemType,
    Route,
)
from dankagu.models.inventory import PlayerInventoryItem
from dankagu.models.storage import PlayerStorageEntry

logger = logging.getLogger("dankagu.core.growth")

# ---------------------------------------------------------------------------
# Growth Material Item IDs
# ---------------------------------------------------------------------------
# Character EXP drops (ユメのしずく: 小、中、大、特大、極)
EXP_ITEM_IDS: list[int] = [30001, 30002, 30003, 30004, 30005]

# Character Limit Break / Blossom drops (心・技・体・万能開花の雫 & 深度開花結晶)
BREAKTHROUGH_ITEM_IDS: list[int] = [
    31001, 31002, 31003, 31004,  # 小
    31005, 31006, 31007, 31008,  # 大
    31009, 31010, 31011, 31012,  # 特大
    31013, 31014, 31015,         # 深度開花雫
]

# Skill enhancement books (スキルの書: 上、極)
SKILL_ITEM_IDS: list[int] = [32003, 32004]

# All growth materials combined
ALL_GROWTH_ITEM_IDS: list[int] = EXP_ITEM_IDS + BREAKTHROUGH_ITEM_IDS + SKILL_ITEM_IDS


def encode_growth_item(item_id: int, count: int, now: int) -> bytes:
    """Encode GrowthItemStock protobuf binary payload.

    Field 1: item_id (varint)
    Field 2: count (varint)
    Field 3: updated_at (varint)
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
    """Decode GrowthItemStock protobuf binary payload, returning (item_id, count)."""
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


async def add_growth_items(
    session: AsyncSession,
    player_id: str,
    item_ids: list[int],
    count: int = 500,
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
            set_={"value": upsert_stmt.excluded.value, "updated_at": upsert_stmt.excluded.updated_at},
        )
        await session.execute(upsert_stmt)

    logger.info(
        "✨ Added %d count of %d growth materials for player %s",
        count,
        len(item_ids),
        player_id,
    )


def create_daily_reward_mail_items(
    player_id: str,
    total_login_days: int,
    stone_amount: int = 20000,
    material_count: int = 500,
) -> list[PlayerInventoryItem]:
    """Generate daily login reward mail items for stones and growth materials."""
    now = int(time.time())
    items: list[PlayerInventoryItem] = []

    # 1. 御霊石 20,000
    mail_stones = PlayerInventoryItem(
        id=str(uuid.uuid4()),
        player_id=player_id,
        item_type=int(ItemType.VIRTUAL_CURRENCY),
        schema_key="VIRTUAL_CURRENCY",
        value=json.dumps(
            {"Prefix": "VirtualCurrency", "Value": "FREE_STONE", "Count": stone_amount}
        ).encode("utf-8"),
        route=int(Route.LOGIN_BONUS),
        message=f"【通算ログイン{total_login_days}日目】御霊石 x{stone_amount:,}",
        search_label="Stone",
        opened_at=now,
        expired_at=now + 86400 * 90,
        system_resource_name="FREE_STONE",
        system_resource_num=stone_amount,
        is_claimed=False,
        created_at=now,
    )
    items.append(mail_stones)

    # 2. 角色升级素材（ユメのしずく各500个）
    mail_exp = PlayerInventoryItem(
        id=str(uuid.uuid4()),
        player_id=player_id,
        item_type=int(ItemType.GAME_RUNTIME),
        schema_key="ITEM",
        value=json.dumps(
            {
                "Prefix": "GrowthItem",
                "Items": EXP_ITEM_IDS,
                "Count": material_count,
            }
        ).encode("utf-8"),
        route=int(Route.LOGIN_BONUS),
        message=f"【通算ログイン{total_login_days}日目】キャラクター育成素材セット（ユメのしずく各{material_count}個）",
        search_label="Growth",
        opened_at=now,
        expired_at=now + 86400 * 90,
        system_resource_name="GROWTH_EXP",
        system_resource_num=material_count,
        is_claimed=False,
        created_at=now,
    )
    items.append(mail_exp)

    # 3. 角色开花与突破素材（心・技・体・万能各500个）
    mail_limit = PlayerInventoryItem(
        id=str(uuid.uuid4()),
        player_id=player_id,
        item_type=int(ItemType.GAME_RUNTIME),
        schema_key="ITEM",
        value=json.dumps(
            {
                "Prefix": "GrowthItem",
                "Items": BREAKTHROUGH_ITEM_IDS,
                "Count": material_count,
            }
        ).encode("utf-8"),
        route=int(Route.LOGIN_BONUS),
        message=f"【通算ログイン{total_login_days}日目】キャラクター開花・突破素材セット（心・技・体・万能各{material_count}個）",
        search_label="Growth",
        opened_at=now,
        expired_at=now + 86400 * 90,
        system_resource_name="GROWTH_LIMIT_BREAK",
        system_resource_num=material_count,
        is_claimed=False,
        created_at=now,
    )
    items.append(mail_limit)

    # 4. 技能强化素材（スキルの書各500个）
    mail_skill = PlayerInventoryItem(
        id=str(uuid.uuid4()),
        player_id=player_id,
        item_type=int(ItemType.GAME_RUNTIME),
        schema_key="ITEM",
        value=json.dumps(
            {
                "Prefix": "GrowthItem",
                "Items": SKILL_ITEM_IDS,
                "Count": material_count,
            }
        ).encode("utf-8"),
        route=int(Route.LOGIN_BONUS),
        message=f"【通算ログイン{total_login_days}日目】スキルの書・強化素材セット（各{material_count}個）",
        search_label="Growth",
        opened_at=now,
        expired_at=now + 86400 * 90,
        system_resource_name="GROWTH_SKILL",
        system_resource_num=material_count,
        is_claimed=False,
        created_at=now,
    )
    items.append(mail_skill)

    return items
