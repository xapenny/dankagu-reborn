"""Starter player data seeding for cold-start onboarding."""

import logging
import time

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.core.storage_state import StorageRevisionManager
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
        "088880908398d9838b0510a48d0618b9e2859906208fc0df990640015801"
    ),
    # Marisa Kirisame starter card
    "CardStatus--366497051944681478": bytes.fromhex(
        "088680908388e4838b0510a88d061890e385990620bae585990640015801"
    ),
    # Remilia Scarlet starter card
    "CardStatus--366496678282526729": bytes.fromhex(
        "088980908398d9838b0510aa8d0618b9e2859906208fc0df990640015801"
    ),
    # HakoniwaStatus--1: Hakurei Shrine initial stage (ID=1, PartyPt=0)
    "HakoniwaStatus--1": bytes.fromhex("08011000"),
    # DailyBonusStatus--1: Seeded login bonus status (ID=1, Count=5) to prevent client crash on missing EOL banner
    "DailyBonusStatus--1": bytes.fromhex("0801100518ef9caa880620c0f3989a06"),
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
            free_stones=50000,
            paid_stones=10000,
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

    StorageRevisionManager.set_revision("1", player_id=player.id)
    logger.info("🌱 Seeded starter data for player %s (%s)", player.id, player.user_id)
