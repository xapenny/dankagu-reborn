"""In-memory game master data repository loaded directly from data/metadata/*.json."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from dankagu.config import settings

logger = logging.getLogger("dankagu.core.metadata")


@dataclass
class GachaCard:
    """Card definition in the gacha pool."""

    id: int
    rarity: int
    element: int = 0
    chara_id: int = 0
    name: str = ""
    second_name: str = ""
    enabled: bool = True

    def __repr__(self) -> str:
        return f"<GachaCard id={self.id} rarity={self.rarity} element={self.element}>"


@dataclass
class GachaBanner:
    """Gacha banner configuration."""

    id: int
    name: str
    element: int = 0
    guaranteed_ssr: bool = False
    pickups: list[int] = field(default_factory=list)
    enabled: bool = True

    def __repr__(self) -> str:
        return f"<GachaBanner id={self.id} name={self.name!r} elem={self.element} guaranteed_ssr={self.guaranteed_ssr}>"


class MetadataRepository:
    """Singleton repository holding all static master game catalogs in memory."""

    def __init__(self) -> None:
        self.game_products: list[dict[str, Any]] = []
        self.game_products_by_id: dict[str, dict[str, Any]] = {}
        self.announcements: list[dict[str, Any]] = []
        self.loot_box_products: dict[str, dict[str, Any]] = {}
        self.reconstruction_spots: list[dict[str, Any]] = []
        self.ondemand_entries: dict[str, dict[str, Any]] = {}
        self.default_storage: dict[str, bytes] = {}
        self.system_templates: dict[str, dict[str, Any]] = {}

        # Gacha pool structures
        self.banners: dict[int, GachaBanner] = {}
        self.cards: dict[int, GachaCard] = {}
        self.cards_by_rarity: dict[int, list[int]] = {1: [], 2: [], 3: [], 4: []}
        self.cards_by_element: dict[int, dict[int, list[int]]] = {1: {}, 2: {}, 3: {}}

        self._loaded: bool = False

    def load_all(self, force: bool = False) -> None:
        """Load all metadata JSON files from data/metadata/ into memory."""
        if self._loaded and not force:
            return

        meta_dir = settings.data_dir / "metadata"
        if not meta_dir.exists():
            logger.warning("Metadata directory not found: %s", meta_dir)
            self._loaded = True
            return

        # 1. Game Products
        gp_file = meta_dir / "game_products.json"
        if gp_file.exists():
            try:
                raw_gp = json.loads(gp_file.read_text(encoding="utf-8"))
                self.game_products = raw_gp if isinstance(raw_gp, list) else []
                self.game_products_by_id = {
                    str(p.get("game_product_id") or p.get("id", "")): p
                    for p in self.game_products
                    if (p.get("game_product_id") or p.get("id"))
                }
            except Exception as e:
                logger.error("Failed to load game_products.json: %s", e)

        # 2. Announcements
        ann_file = meta_dir / "announcements.json"
        if ann_file.exists():
            try:
                raw_ann = json.loads(ann_file.read_text(encoding="utf-8"))
                self.announcements = raw_ann if isinstance(raw_ann, list) else []
            except Exception as e:
                logger.error("Failed to load announcements.json: %s", e)

        # 3. Loot Box Products
        lb_file = meta_dir / "loot_box_products.json"
        if lb_file.exists():
            try:
                raw_lb = json.loads(lb_file.read_text(encoding="utf-8"))
                if isinstance(raw_lb, list):
                    self.loot_box_products = {}
                    for p in raw_lb:
                        pid = (
                            p.get("lootProduct", {}).get("lootBoxProductId")
                            or p.get("loot_product", {}).get("loot_box_product_id")
                            or p.get("id", "")
                        )
                        if pid:
                            self.loot_box_products[pid] = p
            except Exception as e:
                logger.error("Failed to load loot_box_products.json: %s", e)

        # 4. Card Pool & Banners
        cp_file = meta_dir / "card_pool.json"
        if cp_file.exists():
            try:
                cp_data = json.loads(cp_file.read_text(encoding="utf-8"))
                # Banners
                raw_banners = cp_data.get("banners", [])
                banner_items = (
                    list(raw_banners.values()) if isinstance(raw_banners, dict) else raw_banners
                )
                self.banners = {}
                for b in banner_items:
                    bid = int(b["id"])
                    self.banners[bid] = GachaBanner(
                        id=bid,
                        name=str(b.get("name", "")),
                        element=int(b.get("element", 0)),
                        guaranteed_ssr=bool(b.get("guaranteed_ssr", False)),
                        pickups=list(b.get("pickups", [])),
                        enabled=bool(b.get("enabled", True)),
                    )

                # Cards
                raw_cards = cp_data.get("cards", {})
                card_items = list(raw_cards.values()) if isinstance(raw_cards, dict) else raw_cards
                self.cards = {}
                by_rarity: dict[int, list[int]] = {1: [], 2: [], 3: [], 4: []}
                by_elem: dict[int, dict[int, list[int]]] = {1: {}, 2: {}, 3: {}}

                for c in card_items:
                    cid = int(c["id"])
                    card = GachaCard(
                        id=cid,
                        rarity=int(c.get("rarity", 1)),
                        element=int(c.get("element", 0)),
                        chara_id=int(c.get("chara_id", 0)),
                        name=str(c.get("name", "")),
                        second_name=str(c.get("second_name", "")),
                        enabled=bool(c.get("enabled", True)),
                    )
                    self.cards[cid] = card
                    if card.enabled:
                        by_rarity.setdefault(card.rarity, []).append(cid)
                        if card.element in (1, 2, 3):
                            by_elem.setdefault(card.element, {}).setdefault(card.rarity, []).append(
                                cid
                            )

                self.cards_by_rarity = by_rarity
                self.cards_by_element = by_elem
            except Exception as e:
                logger.error("Failed to load card_pool.json: %s", e)

        # 5. Reconstruction Spots
        rs_file = meta_dir / "reconstruction_spots.json"
        if rs_file.exists():
            try:
                raw_rs = json.loads(rs_file.read_text(encoding="utf-8"))
                self.reconstruction_spots = raw_rs if isinstance(raw_rs, list) else []
            except Exception as e:
                logger.error("Failed to load reconstruction_spots.json: %s", e)

        # 6. Ondemand Gacha Entries
        od_file = meta_dir / "ondemand_gacha.json"
        if od_file.exists():
            try:
                raw_od = json.loads(od_file.read_text(encoding="utf-8"))
                if isinstance(raw_od, list):
                    self.ondemand_entries = {}
                    for entry in raw_od:
                        k = entry.get("key", "")
                        v = entry.get("value", {})
                        if isinstance(v, str):
                            v = json.loads(v)
                        if k:
                            self.ondemand_entries[k] = v
            except Exception as e:
                logger.error("Failed to load ondemand_gacha.json: %s", e)

        # 7. Default Storage Templates
        ds_file = meta_dir / "default_storage.json"
        if ds_file.exists():
            try:
                raw_ds = json.loads(ds_file.read_text(encoding="utf-8"))
                if isinstance(raw_ds, dict):
                    self.default_storage = {
                        k: bytes.fromhex(hex_val) for k, hex_val in raw_ds.items()
                    }
            except Exception as e:
                logger.error("Failed to load default_storage.json: %s", e)

        # 8. System Templates (Wallet & Login Bonus)
        self.system_templates = {}
        w_file = meta_dir / "wallet_template.json"
        if w_file.exists():
            try:
                self.system_templates["wallet"] = json.loads(w_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error("Failed to load wallet_template.json: %s", e)

        lb_file = meta_dir / "login_bonuses.json"
        if lb_file.exists():
            try:
                self.system_templates["login_bonus"] = json.loads(
                    lb_file.read_text(encoding="utf-8")
                )
            except Exception as e:
                logger.error("Failed to load login_bonuses.json: %s", e)

        self._loaded = True
        logger.info(
            "📦 Metadata repository loaded: %d products, %d announcements, %d loot boxes, %d banners, %d cards",
            len(self.game_products),
            len(self.announcements),
            len(self.loot_box_products),
            len(self.banners),
            len(self.cards),
        )

    def reload(self) -> None:
        """Force reload metadata from disk."""
        self.load_all(force=True)

    # Accessors
    def get_game_products(self) -> list[dict[str, Any]]:
        if not self._loaded:
            self.load_all()
        return self.game_products

    def get_game_product(self, product_id: str) -> dict[str, Any] | None:
        if not self._loaded:
            self.load_all()
        return self.game_products_by_id.get(product_id)

    def get_available_announcements(self) -> list[dict[str, Any]]:
        if not self._loaded:
            self.load_all()
        return [a for a in self.announcements if a.get("enabled", False)]

    def get_all_announcements(self) -> list[dict[str, Any]]:
        if not self._loaded:
            self.load_all()
        return self.announcements

    def get_loot_box_product(self, product_id: str) -> dict[str, Any] | None:
        if not self._loaded:
            self.load_all()
        return self.loot_box_products.get(product_id)

    def get_all_loot_box_products(self) -> list[dict[str, Any]]:
        if not self._loaded:
            self.load_all()
        return list(self.loot_box_products.values())

    def register_loot_box_product(self, product_id: str, data: dict[str, Any]) -> None:
        """Register or override a loot box product in memory (used by tests)."""
        self.loot_box_products[product_id] = data

    def get_reconstruction_spots(self) -> list[dict[str, Any]]:
        if not self._loaded:
            self.load_all()
        return self.reconstruction_spots

    def get_ondemand_entries(self) -> dict[str, dict[str, Any]]:
        if not self._loaded:
            self.load_all()
        return self.ondemand_entries

    def get_ondemand_entry(self, key: str) -> dict[str, Any] | None:
        if not self._loaded:
            self.load_all()
        return self.ondemand_entries.get(key)

    def get_default_storage(self) -> dict[str, bytes]:
        if not self._loaded:
            self.load_all()
        return self.default_storage

    def get_wallet_template(self) -> dict[str, Any]:
        if not self._loaded:
            self.load_all()
        return self.system_templates.get("wallet", {})

    def get_login_bonus_template(self) -> dict[str, Any]:
        if not self._loaded:
            self.load_all()
        return self.system_templates.get("login_bonus", {})


metadata = MetadataRepository()
