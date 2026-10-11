"""Master database seeder loading catalogs from data/metadata/*.json into PostgreSQL."""

import json
import logging
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.config import settings
from dankagu.models.master import (
    MasterAnnouncement,
    MasterDefaultStorage,
    MasterExpedition,
    MasterGameProduct,
    MasterItem,
    MasterLootBoxProduct,
    MasterMusic,
    MasterOndemandEntry,
    MasterReconstructionSpot,
    MasterScore,
    MasterSystemTemplate,
)

logger = logging.getLogger("dankagu.core.master_seed")


async def seed_master_data_if_empty(session: AsyncSession) -> dict[str, int]:
    """Seed all master catalog tables from JSON files in data/metadata/ if empty."""
    meta_dir = settings.data_dir / "metadata"
    if not meta_dir.exists():
        logger.info("ℹ️ No metadata directory found at %s. Skipping master seed.", meta_dir)
        return {}

    counts: dict[str, int] = {}

    # 1. Game Products
    p_file = meta_dir / "game_products.json"
    p_count = (await session.execute(select(func.count(MasterGameProduct.id)))).scalar() or 0
    if p_count == 0 and p_file.exists():
        try:
            p_data = json.loads(p_file.read_text(encoding="utf-8"))
            for p in p_data:
                pid = str(p.get("game_product_id") or p.get("id", ""))
                stmt = pg_insert(MasterGameProduct).values(
                    id=pid,
                    name=str(p.get("name", "")),
                    category=int(p.get("category", 0)),
                    data=p,
                ).on_conflict_do_nothing(index_elements=[MasterGameProduct.id])
                await session.execute(stmt)
            counts["game_products"] = len(p_data)
            logger.info("🛍️ Seeded %d master game products", len(p_data))
        except Exception as exc:
            logger.error("Failed to seed game products: %s", exc)

    # 2. Announcements
    a_file = meta_dir / "announcements.json"
    a_count = (await session.execute(select(func.count(MasterAnnouncement.id)))).scalar() or 0
    if a_count == 0 and a_file.exists():
        try:
            a_data = json.loads(a_file.read_text(encoding="utf-8"))
            for a in a_data:
                aid = str(a.get("announcement_id") or a.get("id", ""))
                stmt = pg_insert(MasterAnnouncement).values(
                    id=aid,
                    title=str(a.get("title", "")),
                    category=int(a.get("category", 0)),
                    enabled=bool(a.get("enabled", False)),
                    data=a,
                ).on_conflict_do_nothing(index_elements=[MasterAnnouncement.id])
                await session.execute(stmt)
            counts["announcements"] = len(a_data)
            logger.info("📢 Seeded %d master announcements", len(a_data))
        except Exception as exc:
            logger.error("Failed to seed announcements: %s", exc)

    # 3. Reconstruction Spots
    r_file = meta_dir / "reconstruction_spots.json"
    r_count = (await session.execute(select(func.count(MasterReconstructionSpot.id)))).scalar() or 0
    if r_count == 0 and r_file.exists():
        try:
            r_data = json.loads(r_file.read_text(encoding="utf-8"))
            for r in r_data:
                rid = str(r.get("spot_id") or r.get("id", ""))
                stmt = pg_insert(MasterReconstructionSpot).values(
                    id=rid,
                    name=str(r.get("name", "")),
                    data=r,
                ).on_conflict_do_nothing(index_elements=[MasterReconstructionSpot.id])
                await session.execute(stmt)
            counts["reconstruction_spots"] = len(r_data)
            logger.info("⛩️ Seeded %d master reconstruction spots", len(r_data))
        except Exception as exc:
            logger.error("Failed to seed reconstruction spots: %s", exc)

    # 4. Musics
    m_file = meta_dir / "musics.json"
    m_count = (await session.execute(select(func.count(MasterMusic.id)))).scalar() or 0
    if m_count == 0 and m_file.exists():
        try:
            m_data = json.loads(m_file.read_text(encoding="utf-8"))
            for m in m_data:
                stmt = pg_insert(MasterMusic).values(
                    id=int(m["id"]),
                    name=str(m.get("name", "")),
                    reading=str(m.get("reading", "")),
                    composer=str(m.get("composer", "")),
                    circle_id=int(m.get("circle_id", 0)),
                    open_at=int(m.get("open_at", 0)),
                    close_at=int(m.get("close_at", 0)),
                ).on_conflict_do_nothing(index_elements=[MasterMusic.id])
                await session.execute(stmt)
            counts["musics"] = len(m_data)
            logger.info("🎵 Seeded %d master musics", len(m_data))
        except Exception as exc:
            logger.error("Failed to seed musics: %s", exc)

    # 5. Scores
    s_file = meta_dir / "music_scores.json"
    s_count = (await session.execute(select(func.count(MasterScore.id)))).scalar() or 0
    if s_count == 0 and s_file.exists():
        try:
            s_data = json.loads(s_file.read_text(encoding="utf-8"))
            for s in s_data:
                stmt = pg_insert(MasterScore).values(
                    id=int(s["id"]),
                    music_id=int(s.get("music_id", 0)),
                    difficulty=int(s.get("difficulty", 1)),
                    level=int(s.get("level", 1)),
                    total_notes=int(s.get("total_notes", 0)),
                ).on_conflict_do_nothing(index_elements=[MasterScore.id])
                await session.execute(stmt)
            counts["music_scores"] = len(s_data)
            logger.info("🎼 Seeded %d master music scores", len(s_data))
        except Exception as exc:
            logger.error("Failed to seed music scores: %s", exc)

    # 6. Items
    i_file = meta_dir / "items.json"
    i_count = (await session.execute(select(func.count(MasterItem.id)))).scalar() or 0
    if i_count == 0 and i_file.exists():
        try:
            i_data = json.loads(i_file.read_text(encoding="utf-8"))
            for item in i_data:
                stmt = pg_insert(MasterItem).values(
                    id=int(item["id"]),
                    category=str(item.get("category", "")),
                    name=str(item.get("name", "")),
                    description=str(item.get("description", "")),
                ).on_conflict_do_nothing(index_elements=[MasterItem.id])
                await session.execute(stmt)
            counts["items"] = len(i_data)
            logger.info("🎒 Seeded %d master items", len(i_data))
        except Exception as exc:
            logger.error("Failed to seed items: %s", exc)

    # 7. Expeditions
    e_file = meta_dir / "expeditions.json"
    e_count = (await session.execute(select(func.count(MasterExpedition.id)))).scalar() or 0
    if e_count == 0 and e_file.exists():
        try:
            e_data = json.loads(e_file.read_text(encoding="utf-8"))
            for exp in e_data:
                stmt = pg_insert(MasterExpedition).values(
                    id=int(exp["id"]),
                    name=str(exp.get("name", "")),
                    duration_sec=int(exp.get("duration_sec", 0)),
                    wood_drop=int(exp.get("wood_drop", 0)),
                    stone_drop=int(exp.get("stone_drop", 0)),
                ).on_conflict_do_nothing(index_elements=[MasterExpedition.id])
                await session.execute(stmt)
            counts["expeditions"] = len(e_data)
            logger.info("🧭 Seeded %d master expeditions", len(e_data))
        except Exception as exc:
            logger.error("Failed to seed expeditions: %s", exc)

    # 8. Loot Box Products
    lb_file = meta_dir / "loot_box_products.json"
    lb_count = (await session.execute(select(func.count(MasterLootBoxProduct.id)))).scalar() or 0
    if lb_count == 0 and lb_file.exists():
        try:
            lb_data = json.loads(lb_file.read_text(encoding="utf-8"))
            for p in lb_data:
                pid = (
                    p.get("lootProduct", {}).get("lootBoxProductId")
                    or p.get("loot_product", {}).get("loot_box_product_id")
                    or p.get("id", "")
                )
                if pid:
                    stmt = pg_insert(MasterLootBoxProduct).values(
                        id=pid,
                        data=p,
                    ).on_conflict_do_nothing(index_elements=[MasterLootBoxProduct.id])
                    await session.execute(stmt)
            counts["loot_box_products"] = len(lb_data)
            logger.info("🎁 Seeded %d master loot box products", len(lb_data))
        except Exception as exc:
            logger.error("Failed to seed loot box products: %s", exc)

    # 9. Ondemand Gacha Entries
    od_file = meta_dir / "ondemand_gacha.json"
    od_count = (await session.execute(select(func.count(MasterOndemandEntry.key)))).scalar() or 0
    if od_count == 0 and od_file.exists():
        try:
            od_data = json.loads(od_file.read_text(encoding="utf-8"))
            for entry in od_data:
                k = entry.get("key", "")
                v = entry.get("value", {})
                if isinstance(v, str):
                    v = json.loads(v)
                if k:
                    stmt = pg_insert(MasterOndemandEntry).values(
                        key=k,
                        value=v,
                    ).on_conflict_do_nothing(index_elements=[MasterOndemandEntry.key])
                    await session.execute(stmt)
            counts["ondemand_entries"] = len(od_data)
            logger.info("🎟️ Seeded %d master ondemand gacha entries", len(od_data))
        except Exception as exc:
            logger.error("Failed to seed ondemand entries: %s", exc)

    # 10. Default Storage Templates
    ds_file = meta_dir / "default_storage.json"
    ds_count = (await session.execute(select(func.count(MasterDefaultStorage.key)))).scalar() or 0
    if ds_count == 0 and ds_file.exists():
        try:
            ds_data = json.loads(ds_file.read_text(encoding="utf-8"))
            for k, hex_val in ds_data.items():
                val_bytes = bytes.fromhex(hex_val)
                stmt = pg_insert(MasterDefaultStorage).values(
                    key=k,
                    value=val_bytes,
                ).on_conflict_do_nothing(index_elements=[MasterDefaultStorage.key])
                await session.execute(stmt)
            counts["default_storage"] = len(ds_data)
            logger.info("💾 Seeded %d master default storage entries", len(ds_data))
        except Exception as exc:
            logger.error("Failed to seed default storage: %s", exc)

    # 11. System Templates (Wallet & Login Bonus)
    st_count = (await session.execute(select(func.count(MasterSystemTemplate.key)))).scalar() or 0
    if st_count == 0:
        w_file = meta_dir / "wallet_template.json"
        if w_file.exists():
            try:
                w_data = json.loads(w_file.read_text(encoding="utf-8"))
                stmt = pg_insert(MasterSystemTemplate).values(
                    key="wallet",
                    data=w_data,
                ).on_conflict_do_nothing(index_elements=[MasterSystemTemplate.key])
                await session.execute(stmt)
                counts["system_wallet"] = 1
            except Exception as exc:
                logger.error("Failed to seed wallet template: %s", exc)

        lb_file = meta_dir / "login_bonuses.json"
        if lb_file.exists():
            try:
                lb_data = json.loads(lb_file.read_text(encoding="utf-8"))
                stmt = pg_insert(MasterSystemTemplate).values(
                    key="login_bonus",
                    data=lb_data,
                ).on_conflict_do_nothing(index_elements=[MasterSystemTemplate.key])
                await session.execute(stmt)
                counts["system_login_bonus"] = 1
            except Exception as exc:
                logger.error("Failed to seed login bonus template: %s", exc)
        logger.info("⚙️ Seeded system master templates")

    await session.commit()
    return counts
