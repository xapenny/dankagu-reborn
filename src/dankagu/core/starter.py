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

# Protobuf binaries for starter player state
STARTER_STORAGE_ENTRIES = {
    # PlayerBootStatus--1: Initial tutorial/onboarding state with future appeal timestamp
    "PlayerBootStatus--1": b"\x08\x01\x18\x01 \x0e(\xff\xff\xff\xff\x07",
    # Formation--1: Initial starter deck
    "Formation--1": bytes.fromhex(
        "0801121289809083f3b0fdf30481809083f4d8d7be041a2d8080908397ecf3a4038280908385eff89c0480809083a5f1efbe0481809083defcd4e004808090838bc3f78b032202456d38f59dea9906"
    ),
    # Reimu Hakurei starter card
    "CardStatus--366496678282526728": bytes.fromhex(
        "088880908398d9838b0510a48d0618b9e2859906208fc0df9906400150015801"
    ),
    # Marisa Kirisame starter card
    "CardStatus--366497051944681478": bytes.fromhex(
        "088680908388e4838b0510a88d061890e385990620bae5859906400150015801"
    ),
    # Remilia Scarlet starter card
    "CardStatus--366496678282526729": bytes.fromhex(
        "088980908398d9838b0510aa8d0618b9e2859906208fc0df9906400150015801"
    ),
    # HakoniwaStatus--1: Hakurei Shrine initial stage (ID=1, PartyPt=0)
    "HakoniwaStatus--1": bytes.fromhex("08011000"),
    # DailyBonusStatus--1: Seeded login bonus status (ID=1, Count=5) to prevent client crash on missing EOL banner
    "DailyBonusStatus--1": bytes.fromhex("0801100518ef9caa880620c0f3989a06"),
    # PlayerStatus--1: Seeded player status with Exp=100 (Level 2+) to unlock Feature 11 (Travel / おでかけ)
    "PlayerStatus--1": bytes.fromhex(
        "080118bba5a7d6062064280a30f3a0a7d60648c84c50c874600168880170048001f3a0a7d606"
    ),
    # ExpeditionMissionRecordEntityCollecttion--1: Initial expedition mission record with Mission 10001 (tutorial mission)
    "ExpeditionMissionRecordEntityCollecttion--1": bytes.fromhex(
        "0801122408914e10001800200028f3a0a7d60630f3a0a7d6063a0200004205000000000048015000"
    ),
    # ExpeditionManageStatus--1: Expedition manager status initialized with tutorial Mission 10001
    "ExpeditionManageStatus--1": bytes.fromhex(
        "080110914e18f3a0a7d60620f3a0a7d606"
    ),
    # ItemStockStatus--1000: Starter wood (count=5000) for Gensokyo Hakurei Shrine reconstruction
    "ItemStockStatus--1000": bytes.fromhex("08e807108827"),
    # ItemStockStatus--2000: Starter stone (count=5000) for Gensokyo Hakurei Shrine reconstruction
    "ItemStockStatus--2000": bytes.fromhex("08d00f108827"),
    # PlayerSetting--1: Seeded player settings with lastActiveEventViewTime and lastNewMusicAnnouncementViewTime
    # set to future timestamps to prevent boot announcement spam of blank event banners on fresh accounts.
    "PlayerSetting--1": bytes.fromhex(
        "080110ef9caa880618f49ce8d5062001420ffa8e06d28e068c8e06ca8e06c88e06520501010101015a050100000001600a68e0c6899a0670e080ff99067a0601efd39ca40f8001efd39ca40f8801efd39ca40f90010e9801c5a2aa8806a001d199f98806a80101c00101"
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

    # 2. Seed Starter Storage Entries
    for key, val in STARTER_STORAGE_ENTRIES.items():
        stmt = pg_insert(PlayerStorageEntry).values(
            player_id=player.id,
            key=key,
            value=val,
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[PlayerStorageEntry.player_id, PlayerStorageEntry.key]
        )
        await session.execute(stmt)

    # 3. Seed Starter Welcome Gift in Mailbox
    inv_res = await session.execute(
        select(PlayerInventoryItem).where(PlayerInventoryItem.player_id == player.id)
    )
    if inv_res.scalars().first() is None:
        welcome_gift = PlayerInventoryItem(
            id=str(uuid.uuid4()),
            player_id=player.id,
            item_type=0,
            schema_key="ITEM",
            value=json.dumps(
                {"Prefix": "VirtualCurrency", "Value": "FREE_STONE", "Count": 2500}
            ).encode("utf-8"),
            route=1,
            message="ダンカグ Reborn へようこそ！事前登録＆リリース記念報酬",
            search_label="Stone",
            opened_at=now,
            expired_at=4102498799,
            system_resource_name="FREE_STONE",
            system_resource_num=2500,
            is_claimed=False,
            created_at=now,
        )
        session.add(welcome_gift)

    StorageRevisionManager.set_revision("1", player_id=player.id)
    logger.info("🌱 Seeded starter data for player %s (%s)", player.id, player.user_id)
