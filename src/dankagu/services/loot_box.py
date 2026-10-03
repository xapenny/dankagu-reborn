"""Takasho LootBoxV3 servicer for gacha banners and card pulls."""

import json
import logging
import random
import time
import uuid
from pathlib import Path

import grpc

from dankagu.config import settings
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    loot_box_pb2,
    loot_box_pb2_grpc,
)

logger = logging.getLogger("dankagu.services.loot_box")

# Gacha IDs whose banners & logos exist in manifest 3760 and on disk
VALID_GACHA_IDS = {1, 3, 159, 160, 161, 165, 169, 173, 4001, 90010}

# Load card pool
_CARD_POOL_PATH = Path(__file__).resolve().parent.parent / "core" / "card_pool.json"
_CARDS_BY_RARITY: dict[int, list[int]] = {1: [], 2: [], 3: [], 4: []}
if _CARD_POOL_PATH.exists():
    try:
        raw_pool = json.loads(_CARD_POOL_PATH.read_text(encoding="utf-8"))
        _CARDS_BY_RARITY = {int(k): v for k, v in raw_pool.items()}
        logger.info(
            "🃏 Loaded card pool: %d SSR, %d SR, %d R, %d N cards",
            len(_CARDS_BY_RARITY.get(4, [])),
            len(_CARDS_BY_RARITY.get(3, [])),
            len(_CARDS_BY_RARITY.get(2, [])),
            len(_CARDS_BY_RARITY.get(1, [])),
        )
    except Exception as exc:
        logger.error("Failed to load card pool: %s", exc)


def _roll_card(is_guaranteed_sr: bool = False) -> int:
    """Roll a card with authentic gacha rates.

    Rates:
      Normal pull: SSR=4%, SR=16%, R=80%
      Guaranteed pull (10th pull): SSR=4%, SR=96%
    """
    if is_guaranteed_sr:
        r = random.random()
        rarity = 4 if r < 0.04 else 3
    else:
        r = random.random()
        if r < 0.04:
            rarity = 4
        elif r < 0.20:
            rarity = 3
        else:
            rarity = 2

    pool = _CARDS_BY_RARITY.get(rarity) or _CARDS_BY_RARITY.get(2) or [100001]
    return random.choice(pool)


class LootBoxService(loot_box_pb2_grpc.LootBoxV3Servicer):
    """Servicer for LootBox V3 gacha banners."""

    def __init__(self) -> None:
        self.cached_available: loot_box_pb2.LootBoxV3GetAvailableV1.Response | None = None
        self._load_loot_boxes()

    def _load_loot_boxes(self) -> None:
        hex_file = settings.hexdata_cf_dir / "loot_box.hex"
        if not hex_file.exists():
            return

        try:
            hex_data = hex_file.read_text(encoding="utf-8").strip()
            orig_resp = loot_box_pb2.LootBoxV3GetAvailableV1.Response.FromString(
                bytes.fromhex(hex_data)
            )

            # Filter products to only include banners with existing assets
            filtered = loot_box_pb2.LootBoxV3GetAvailableV1.Response()
            for prod in orig_resp.loot_box_products:
                pid = prod.loot_product.loot_box_product_id
                parts = pid.split("_")
                gid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                if gid in VALID_GACHA_IDS:
                    p = filtered.loot_box_products.add()
                    p.CopyFrom(prod)
                    # Extend open/close dates to year 2100 so all banners are active
                    p.loot_product.opened_at = 1592373600
                    p.loot_product.closed_at = 4102498799
                    if pid == "G_1_10_TT_L":
                        p.purchased_count = 0

            # Do not set next_page_token to prevent client pagination issues
            filtered.next_page_token = ""
            self.cached_available = filtered
            logger.info(
                "🎁 Loaded %d gacha products across valid banners %s",
                len(self.cached_available.loot_box_products),
                sorted(VALID_GACHA_IDS),
            )
        except Exception as e:
            logger.error("Failed to parse loot_box.hex: %s", e)

    async def GetAvailableV1(
        self,
        request: loot_box_pb2.LootBoxV3GetAvailableV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> loot_box_pb2.LootBoxV3GetAvailableV1.Response:
        token = request.page_token or ""
        logger.info("🎁 [LootBox] GetAvailableV1 (page_token=%s)", token[:20] if token else "root")
        if token:
            # All valid products are delivered on the root page
            return loot_box_pb2.LootBoxV3GetAvailableV1.Response()

        if self.cached_available:
            return self.cached_available
        return loot_box_pb2.LootBoxV3GetAvailableV1.Response()

    async def PurchaseV1(
        self,
        request: loot_box_pb2.LootBoxV3PurchaseV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> loot_box_pb2.LootBoxV3PurchaseV1.Response:
        pid = request.loot_box_product_id
        is_ten_pull = "_10_" in pid
        pull_count = 10 if is_ten_pull else 1
        logger.info(
            "🎁 [LootBox] PurchaseV1 product=%s (count=%d, tx=%s)",
            pid,
            pull_count,
            request.transaction_id,
        )

        response = loot_box_pb2.LootBoxV3PurchaseV1.Response()
        response.transaction_id = request.transaction_id
        now = int(time.time())

        # Generate prizes
        prizes = response.loot_box_content_set_prizes.add()
        prizes.loot_box_content_set_id = f"P_{pid}"

        drawn_cards: list[int] = []
        for i in range(pull_count):
            # 10th card in a 10-pull has SR or SSR guaranteed
            is_guaranteed = is_ten_pull and (i == pull_count - 1)
            card_id = _roll_card(is_guaranteed_sr=is_guaranteed)
            drawn_cards.append(card_id)

            item_payload = json.dumps(
                {
                    "Prefix": "Card",
                    "Value": card_id,
                    "Count": 1,
                }
            ).encode("utf-8")

            # 1. loot_box_contents
            c = prizes.loot_box_contents.add()
            c.loot_box_content_id = f"C_{card_id}_{i}"
            c.item_type = 0
            c.schema_key = "ITEM"
            c.value = item_payload
            c.weight = 1

            # 2. player_inventories (Required by client's GachaProvider/ItemReceiver to display and receive cards)
            inv = response.player_inventories.add()
            inv.id = str(uuid.uuid4())
            inv.player_id = "dankagu-reborn-user"
            inv.item_type = 0  # GameRuntime
            inv.schema_key = "ITEM"
            inv.value = item_payload
            inv.route = 3  # Route.LootBox
            inv.message = "夢見くじで獲得した商品です"
            inv.search_label = "Card"
            inv.opened_at = now
            inv.expired_at = 4102498799
            inv.system_resource_num = 1
            inv.created_at = now

        if is_ten_pull:
            extra_inv = response.extra_player_inventories.add()
            extra_inv.id = str(uuid.uuid4())
            extra_inv.player_id = "dankagu-reborn-user"
            extra_inv.item_type = 0
            extra_inv.schema_key = "ITEM"
            extra_inv.value = json.dumps(
                {
                    "Prefix": "Growth",
                    "Value": 31009,
                    "Count": 10,
                }
            ).encode("utf-8")
            extra_inv.route = 3
            extra_inv.message = "おまけ報酬です"
            extra_inv.search_label = "Item"
            extra_inv.opened_at = now
            extra_inv.expired_at = 4102498799
            extra_inv.system_resource_num = 10
            extra_inv.created_at = now

        logger.info("🎉 [LootBox] Drew %d cards: %s", pull_count, drawn_cards)

        # Update wallet balance
        stone_cost = 1500 if is_ten_pull else 150
        # Provide plenty of virtual currencies
        vc_free = response.wallet.virtual_currencies.add()
        vc_free.virtual_currency_name = "FREE_STONE"
        vc_free.amount = max(0, 99999 - stone_cost)

        vc_paid = response.wallet.virtual_currencies.add()
        vc_paid.virtual_currency_name = "PAID_STONE"
        vc_paid.amount = 10000

        return response

    async def GetProbabilityV1(
        self,
        request: loot_box_pb2.LootBoxV3GetProbabilityV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> loot_box_pb2.LootBoxV3GetProbabilityV1.Response:
        logger.info("🎁 [LootBox] GetProbabilityV1 (%s)", request.loot_box_product_id)
        return loot_box_pb2.LootBoxV3GetProbabilityV1.Response()

    async def GetDetailV1(
        self,
        request: loot_box_pb2.LootBoxV3GetDetailV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> loot_box_pb2.LootBoxV3GetDetailV1.Response:
        logger.info("🎁 [LootBox] GetDetailV1 (%s)", request.loot_box_product_id)
        return loot_box_pb2.LootBoxV3GetDetailV1.Response()


def register_loot_box_servicer(server: grpc.aio.Server) -> None:
    servicer = LootBoxService()
    method_handlers = {
        "GetAvailableV1": takasho_unary_handler(
            servicer.GetAvailableV1,
            loot_box_pb2.LootBoxV3GetAvailableV1.Request,
            loot_box_pb2.LootBoxV3GetAvailableV1.Response,
        ),
        "PurchaseV1": takasho_unary_handler(
            servicer.PurchaseV1,
            loot_box_pb2.LootBoxV3PurchaseV1.Request,
            loot_box_pb2.LootBoxV3PurchaseV1.Response,
        ),
        "GetProbabilityV1": takasho_unary_handler(
            servicer.GetProbabilityV1,
            loot_box_pb2.LootBoxV3GetProbabilityV1.Request,
            loot_box_pb2.LootBoxV3GetProbabilityV1.Response,
        ),
        "GetDetailV1": takasho_unary_handler(
            servicer.GetDetailV1,
            loot_box_pb2.LootBoxV3GetDetailV1.Request,
            loot_box_pb2.LootBoxV3GetDetailV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.LootBoxV3", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
