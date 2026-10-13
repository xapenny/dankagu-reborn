"""Gacha card pool and banner database seeder."""

import json
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.config import settings
from dankagu.models.gacha import GachaBanner, GachaCard

logger = logging.getLogger("dankagu.core.gacha_seed")


DEFAULT_BANNERS = [
    {
        "id": 1,
        "name": "Tutorial",
        "element": 0,
        "guaranteed_ssr": True,
        "pickups": [],
        "enabled": True,
    },
    {
        "id": 3,
        "name": "Start Dash",
        "element": 0,
        "guaranteed_ssr": True,
        "pickups": [],
        "enabled": True,
    },
    {
        "id": 159,
        "name": "Standard 1",
        "element": 0,
        "guaranteed_ssr": False,
        "pickups": [],
        "enabled": True,
    },
    {
        "id": 160,
        "name": "Standard 2",
        "element": 0,
        "guaranteed_ssr": False,
        "pickups": [],
        "enabled": True,
    },
    {
        "id": 161,
        "name": "Standard 3",
        "element": 0,
        "guaranteed_ssr": False,
        "pickups": [],
        "enabled": True,
    },
    {
        "id": 163,
        "name": "Finale Ro",
        "element": 3,
        "guaranteed_ssr": True,
        "pickups": [],
        "enabled": True,
    },
    {
        "id": 167,
        "name": "Finale Em",
        "element": 1,
        "guaranteed_ssr": True,
        "pickups": [],
        "enabled": True,
    },
    {
        "id": 171,
        "name": "Finale Co",
        "element": 2,
        "guaranteed_ssr": True,
        "pickups": [],
        "enabled": True,
    },
    {
        "id": 4087,
        "name": "Point Gacha",
        "element": 0,
        "guaranteed_ssr": False,
        "pickups": [],
        "enabled": True,
    },
    {
        "id": 80001,
        "name": "Ad Gacha",
        "element": 0,
        "guaranteed_ssr": False,
        "pickups": [],
        "enabled": True,
    },
]


async def seed_gacha_data_if_empty(session: AsyncSession) -> tuple[int, int]:
    """Seed gacha_cards and gacha_banners tables if they are empty.

    Loads game metadata from data/metadata/card_pool.json if available.
    Returns:
        tuple[int, int]: (seeded_cards_count, seeded_banners_count)
    """
    card_count = (await session.execute(select(func.count(GachaCard.id)))).scalar() or 0
    banner_count = (await session.execute(select(func.count(GachaBanner.id)))).scalar() or 0

    seeded_cards = 0
    seeded_banners = 0

    # Look for metadata file in ignored data/metadata/ directory
    metadata_path = settings.data_dir / "metadata" / "card_pool.json"
    metadata_data: dict[str, object] | None = None
    if metadata_path.exists():
        try:
            with open(metadata_path, encoding="utf-8") as f:
                metadata_data = json.load(f)
            logger.info("📦 Found metadata file at %s", metadata_path)
        except Exception as exc:
            logger.warning("⚠️ Failed to parse metadata file %s: %s", metadata_path, exc)

    # 1. Seed Cards from metadata if table is empty
    if card_count == 0:
        raw_cards = metadata_data.get("cards") if metadata_data else None
        card_items: list[dict[str, object]] = []
        if isinstance(raw_cards, dict):
            card_items = list(raw_cards.values())
        elif isinstance(raw_cards, list):
            card_items = raw_cards

        if card_items:
            logger.info("🎴 Seeding gacha_cards table from metadata (%d cards)...", len(card_items))
            for cdata in card_items:
                card_obj = GachaCard(
                    id=int(cdata["id"]),
                    rarity=int(cdata["rarity"]),
                    element=int(cdata.get("element", 0)),
                    chara_id=int(cdata.get("chara_id", 0)),
                    name=str(cdata.get("name", "")),
                    second_name=str(cdata.get("second_name", "")),
                    enabled=bool(cdata.get("enabled", True)),
                )
                session.add(card_obj)
                seeded_cards += 1
            await session.flush()
            logger.info("✅ Seeded %d cards into gacha_cards table", seeded_cards)
        else:
            logger.info(
                "ℹ️ No card metadata found at %s. Place metadata json into data/metadata/card_pool.json to seed card pool.",
                metadata_path,
            )

    # 2. Seed Banners from metadata or fallback
    if banner_count == 0:
        raw_banners = metadata_data.get("banners") if metadata_data else None
        banner_items: list[dict[str, object]] = []
        if isinstance(raw_banners, dict):
            banner_items = list(raw_banners.values())
        elif isinstance(raw_banners, list):
            banner_items = raw_banners
        else:
            banner_items = DEFAULT_BANNERS

        logger.info("🎁 Seeding gacha_banners table (%d banners)...", len(banner_items))
        for bdata in banner_items:
            banner_obj = GachaBanner(
                id=int(bdata["id"]),
                name=str(bdata["name"]),
                element=int(bdata["element"]),
                guaranteed_ssr=bool(bdata["guaranteed_ssr"]),
                pickups=list(bdata.get("pickups", [])),
                enabled=bool(bdata.get("enabled", True)),
            )
            session.add(banner_obj)
            seeded_banners += 1
        await session.flush()
        logger.info("✅ Seeded %d banners into gacha_banners table", seeded_banners)

    # 3. Ensure guaranteed_ssr flag on critical banners
    guaranteed_ids = (3, 163, 167, 171)
    for gid in guaranteed_ids:
        b = await session.get(GachaBanner, gid)
        if b and not b.guaranteed_ssr:
            b.guaranteed_ssr = True
            logger.info("🔒 Set guaranteed_ssr=True on GachaBanner %d", gid)

    await session.commit()
    return seeded_cards, seeded_banners
