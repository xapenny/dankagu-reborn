"""Starter player data seeding for cold-start onboarding."""

import json
import logging
import time
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.core.storage_state import StorageRevisionManager
from dankagu.models.inventory import PlayerInventoryItem
from dankagu.models.player import Player
from dankagu.models.storage import PlayerStorageEntry
from dankagu.models.wallet import WalletBalance

logger = logging.getLogger("dankagu.core.starter")

# Protobuf binaries for pure fresh-account starter state
STARTER_STORAGE_ENTRIES = {
    # PlayerBootStatus--1: Initial onboarding state with future account-linkage appeal timestamp
    "PlayerBootStatus--1": b"\x08\x01\x18\x01 \x0e(\xff\xff\xff\xff\x07",
    # HakoniwaStatus--1: Hakurei Shrine initial stage (ID=1, PartyPt=0)
    "HakoniwaStatus--1": bytes.fromhex("08011000"),
    # DailyBonusStatus--1: Clean login bonus status (ID=1, Count=1, CreatedAt=0, LastUpdate=0)
    "DailyBonusStatus--1": bytes.fromhex("0801100118002000"),
    # PlayerStatus--1: Pure initial status (ID=1, Exp=0, Stamina=10, LevelTemp=1)
    "PlayerStatus--1": bytes.fromhex("08012000280a6001"),
    # ExpeditionMissionRecordEntityCollecttion--1: Initial expedition record with tutorial Mission 10001 (CreatedAt=0, LastUpdate=0)
    "ExpeditionMissionRecordEntityCollecttion--1": bytes.fromhex(
        "0801121c08914e100018002000280030003a0200004205000000000048015000"
    ),
    # ExpeditionManageStatus--1: Expedition manager status initialized with tutorial Mission 10001
    "ExpeditionManageStatus--1": bytes.fromhex("080110914e18002000"),
    # PlayerSetting--1: Pure initial player settings with starter N card [100082, 0, 0, 0, 0]
    # and future announcement timestamps (4102498799) to suppress blank EOL event popups.
    "PlayerSetting--1": bytes.fromhex(
        "08014207b28d0600000000520500000000005a050000000000600a7a0601efd39ca40f8001efd39ca40f8801efd39ca40f90010e"
    ),
    # Active Event Initial Statuses (Events 38, 39, 40, 41)
    "EventInfoStatus-V38-38": bytes.fromhex(
        "08261000180032040101010138ca84d1d60658ca84d1d60670037803"
    ),
    "EventInfoStatus-V39-39": bytes.fromhex(
        "08271000180032040101010138ca84d1d60658ca84d1d60670037803"
    ),
    "EventInfoStatus-V40-40": bytes.fromhex(
        "08281000180032040101010138ca84d1d60658ca84d1d60670037803"
    ),
    "EventInfoStatus-V41-41": bytes.fromhex(
        "08291000180032040101010138ca84d1d60658ca84d1d60670037803"
    ),
}


async def seed_starter_player(session: AsyncSession, player: Player) -> None:
    """Seed initial wallet balances and starter storage entries for a new player."""
    now = int(time.time())

    # 1. Seed Wallet
    wallet_res = await session.execute(
        select(WalletBalance).where(WalletBalance.player_id == player.id)
    )
    if wallet_res.scalars().first() is None:
        wallet = WalletBalance(
            player_id=player.id,
            free_stones=60000,
            paid_stones=30000,
            updated_at=now,
        )
        session.add(wallet)

    # 2. Seed Starter Storage Entries in a single batch statement
    storage_rows = [
        {
            "player_id": player.id,
            "key": key,
            "value": val,
            "created_at": now,
            "updated_at": now,
        }
        for key, val in STARTER_STORAGE_ENTRIES.items()
    ]
    stmt = (
        pg_insert(PlayerStorageEntry)
        .values(storage_rows)
        .on_conflict_do_nothing(
            index_elements=[PlayerStorageEntry.player_id, PlayerStorageEntry.key]
        )
    )
    await session.execute(stmt)

    # 3. Seed Starter Welcome Gift and Day 1 Daily Cultivation Materials in Mailbox
    inv_res = await session.execute(
        select(PlayerInventoryItem.id).where(PlayerInventoryItem.player_id == player.id).limit(1)
    )
    if inv_res.scalar() is None:
        from dankagu.core.growth import create_daily_reward_mail_items

        welcome_gift = PlayerInventoryItem(
            id=str(uuid.uuid4()),
            player_id=player.id,
            item_type=1,
            schema_key="VC",
            value=json.dumps({"Prefix": "VC", "Value": 90002, "Count": 2500}).encode("utf-8"),
            route=1,
            message="ダンカグ Reborn へようこそ！事前登録＆リリース記念報酬",
            search_label="VC",
            opened_at=now,
            expired_at=4102498799,
            system_resource_name="FREE_STONE",
            system_resource_num=2500,
            is_claimed=False,
            created_at=now,
        )
        daily_items = create_daily_reward_mail_items(
            player_id=player.id,
            total_login_days=1,
        )
        session.add_all([welcome_gift, *daily_items])

    StorageRevisionManager.set_revision("1", player_id=player.id)
    logger.info("🌱 Seeded starter data for player %s (%s)", player.id, player.user_id)
