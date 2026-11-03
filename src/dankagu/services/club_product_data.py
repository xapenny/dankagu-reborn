"""Master data catalog and pricing helpers for Club Shop exchange."""

from __future__ import annotations

import json
from functools import lru_cache

from dankagu.config import settings


@lru_cache(maxsize=1)
def get_club_products_catalog() -> dict[str, dict]:
    """Load club shop products catalog from data/metadata/club_products.json."""
    p = settings.data_dir / "metadata" / "club_products.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def get_club_product_price(product_id_str: str) -> int:
    """Return the price (in ClubPoint) for a given club product.

    Product ID may be formatted as '5:1' or '1'.
    """
    clean_id_str = product_id_str.split(":")[-1] if ":" in product_id_str else product_id_str
    catalog = get_club_products_catalog()
    if clean_id_str in catalog:
        return int(catalog[clean_id_str].get("price", 200))

    # Minimal fallback for offline/test environments
    try:
        pid = int(clean_id_str)
        if pid <= 19 or pid == 132:
            return 2000
    except ValueError:
        pass
    return 200
