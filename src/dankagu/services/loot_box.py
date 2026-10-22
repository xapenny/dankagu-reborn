"""Takasho LootBoxV3 servicer for gacha banners and card pulls."""

import json
import logging
import random
import time
import uuid
from dataclasses import dataclass

import grpc
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.core.database import get_sessionmaker
from dankagu.core.economy import (
    InsufficientFundsError,
    deduct_player_key_value,
    deduct_stones,
    get_or_create_wallet,
    get_player_key_value,
)
from dankagu.core.session import get_current_player_id
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.errors import fail_internal_on_error
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    loot_box_pb2,
    loot_box_pb2_grpc,
)
from dankagu.models.gacha import GachaBanner, GachaCard
from dankagu.models.master import MasterLootBoxProduct
from dankagu.models.receipt import PurchaseReceipt

logger = logging.getLogger("dankagu.services.loot_box")

# Fallback gacha IDs if database cache has not yet initialized
VALID_GACHA_IDS = {1, 3, 159, 160, 161, 163, 167, 171, 4087, 80001}

# Card handed out when the database holds no gacha rows at all (fresh install / CI),
# so that a purchase can never fail on an empty candidate pool.
STARTER_CARD_ID = 100001

# In-memory card pool and banner caches loaded directly from database
_CARDS_BY_RARITY: dict[int, list[int]] = {1: [], 2: [], 3: [], 4: []}
_BY_ELEMENT: dict[int, dict[int, list[int]]] = {1: {}, 2: {}, 3: {}}
_BANNERS: dict[int, GachaBanner] = {}
_CARDS_BY_ID: dict[int, GachaCard] = {}
_GACHA_CACHE_INITIALIZED = False


async def load_gacha_cache_from_db(session: AsyncSession) -> None:
    """Load card pool and banner configurations from database into memory."""
    global _CARDS_BY_RARITY, _BY_ELEMENT, _BANNERS, _CARDS_BY_ID, _GACHA_CACHE_INITIALIZED

    cards_stmt = select(GachaCard).where(GachaCard.enabled)
    cards = (await session.execute(cards_stmt)).scalars().all()

    banners_stmt = select(GachaBanner).where(GachaBanner.enabled)
    banners = (await session.execute(banners_stmt)).scalars().all()

    by_rarity: dict[int, list[int]] = {1: [], 2: [], 3: [], 4: []}
    by_elem: dict[int, dict[int, list[int]]] = {1: {}, 2: {}, 3: {}}
    cards_map: dict[int, GachaCard] = {}
    banners_map: dict[int, GachaBanner] = {}

    for c in cards:
        cards_map[c.id] = c
        by_rarity.setdefault(c.rarity, []).append(c.id)
        if c.element in (1, 2, 3):
            by_elem.setdefault(c.element, {}).setdefault(c.rarity, []).append(c.id)

    for b in banners:
        banners_map[b.id] = b

    _CARDS_BY_RARITY = by_rarity
    _BY_ELEMENT = by_elem
    _CARDS_BY_ID = cards_map
    _BANNERS = banners_map
    _GACHA_CACHE_INITIALIZED = True

    logger.info(
        "🃏 Loaded gacha cache from database: %d SSR, %d SR, %d R, %d N cards across %d banners",
        len(_CARDS_BY_RARITY.get(4, [])),
        len(_CARDS_BY_RARITY.get(3, [])),
        len(_CARDS_BY_RARITY.get(2, [])),
        len(_CARDS_BY_RARITY.get(1, [])),
        len(_BANNERS),
    )


async def ensure_gacha_cache_loaded(session: AsyncSession) -> None:
    """Ensure database-backed gacha card pool cache is loaded."""
    if not _GACHA_CACHE_INITIALIZED or not _CARDS_BY_ID:
        await load_gacha_cache_from_db(session)


def _roll_card(
    gacha_id: int,
    is_guaranteed_sr: bool = False,
    is_guaranteed_ssr: bool = False,
) -> int:
    """Roll a card with authentic gacha rates and banner-specific pools.

    Rates:
      Normal pull: SSR=4%, SR=16%, R=80%
      Guaranteed SR (10th pull): SSR=4%, SR=96%
      Guaranteed SSR: SSR=100%
    """
    banner = _BANNERS.get(gacha_id)
    elem_filter = banner.element if banner else 0
    pickups = banner.pickups if banner else []

    # 1. Determine rarity
    if is_guaranteed_ssr:
        rarity = 4
    elif is_guaranteed_sr:
        rarity = 4 if random.random() < 0.04 else 3
    else:
        r = random.random()
        if r < 0.04:
            rarity = 4
        elif r < 0.20:
            rarity = 3
        else:
            rarity = 2

    # 2. Candidate pool for this rarity (respecting element restriction if banner is elemental)
    if elem_filter in (1, 2, 3) and elem_filter in _BY_ELEMENT:
        elem_dict = _BY_ELEMENT[elem_filter]
        candidate_pool = elem_dict.get(rarity) or []
    else:
        candidate_pool = _CARDS_BY_RARITY.get(rarity) or []

    if not candidate_pool:
        # Fall back to other rarities or the common pool
        for fallback_r in (4, 3, 2):
            candidate_pool = _CARDS_BY_RARITY.get(fallback_r) or []
            if candidate_pool:
                break

    # 3. Check for pickup cards in this rarity
    rarity_pickups = [
        int(cid)
        for cid in pickups
        if _CARDS_BY_ID.get(int(cid)) and _CARDS_BY_ID[int(cid)].rarity == rarity
    ]

    # 4. Rate-up application for pickups (70% for SSR pickup, 50% for SR pickup)
    if rarity_pickups:
        rate_up_prob = 0.70 if rarity == 4 else 0.50
        if random.random() < rate_up_prob:
            return random.choice(rarity_pickups)

    # 5. Non-pickup roll from candidate pool
    non_pickups = [cid for cid in candidate_pool if cid not in rarity_pickups]
    if non_pickups:
        return random.choice(non_pickups)

    if candidate_pool:
        return random.choice(candidate_pool)

    # 6. Nothing loaded at all: fallback to starter card
    logger.warning(
        "Gacha card pool is empty (database not seeded); falling back to starter card %d",
        STARTER_CARD_ID,
    )
    return STARTER_CARD_ID


@dataclass(frozen=True)
class MasterPrice:
    """Price of a loot box product as the master data describes it."""

    pull_count: int = 0
    stone_cost: int = 0
    point_cost: int = 0
    resource_label: str = ""


async def _master_price(session: AsyncSession, product_id: str) -> MasterPrice | None:
    """Read the authoritative price for ``product_id`` from master data.

    Master only carries a usable number for some products: ``G_1_10_TT_L`` consumes
    the ``tutorial`` key with amount ``-1`` (a ticket, not a price), while
    ``G_3_10_VC_L`` states ``1500``. Products the client asks for that are absent
    (``G_159_*``, for instance) stay on the built-in heuristic; inventing a price for
    them here would be worse than leaving the status quo in place.
    """
    row = await session.get(MasterLootBoxProduct, product_id)
    if row is None or not isinstance(row.data, dict):
        return None

    product = row.data.get("lootProduct")
    if not isinstance(product, dict):
        product = row.data

    raw_pull_count = product.get("totalLotNum")
    try:
        pull_count = int(raw_pull_count) if raw_pull_count is not None else 0
    except (TypeError, ValueError):
        pull_count = 0

    resource_sets = product.get("consumeResourceSets") or []
    if not isinstance(resource_sets, list) or not resource_sets:
        return MasterPrice(pull_count=pull_count)

    first = resource_sets[0] if isinstance(resource_sets[0], dict) else {}
    resource_type = first.get("resourceType")
    try:
        amount = int(first.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0

    if amount <= 0:
        # A non-positive amount is not a price (the tutorial key uses -1); report it
        # as "no charge" and let the caller log exactly what master said.
        return MasterPrice(pull_count=pull_count, resource_label=str(resource_type))

    if resource_type == "PLAYER_KEY_VALUE":
        return MasterPrice(
            pull_count=pull_count, point_cost=amount, resource_label="PLAYER_KEY_VALUE"
        )
    return MasterPrice(
        pull_count=pull_count, stone_cost=amount, resource_label=str(resource_type or "VC")
    )


class LootBoxService(loot_box_pb2_grpc.LootBoxV3Servicer):
    """Servicer for LootBox V3 gacha banners."""

    def __init__(self) -> None:
        self.all_template_products: list[object] = []

    async def _ensure_loaded(self) -> None:
        if self.all_template_products:
            return

        from google.protobuf import json_format
        from sqlalchemy import select

        from dankagu.models.master import MasterLootBoxProduct

        try:
            session_factory = get_sessionmaker()
            async with session_factory() as session:
                result = await session.execute(select(MasterLootBoxProduct))
                rows = result.scalars().all()
                for r in rows:
                    resp = loot_box_pb2.LootBoxV3GetAvailableV1.Response()
                    p = resp.loot_box_products.add()
                    json_format.ParseDict(r.data, p, ignore_unknown_fields=True)
                    self.all_template_products.append(p)
                logger.info(
                    "🎁 Loaded %d gacha products from PostgreSQL",
                    len(self.all_template_products),
                )
        except Exception as e:
            logger.error("Failed to load loot box products from database: %s", e)

    async def GetAvailableV1(
        self,
        request: loot_box_pb2.LootBoxV3GetAvailableV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> loot_box_pb2.LootBoxV3GetAvailableV1.Response:
        await self._ensure_loaded()
        token = request.page_token or ""
        logger.info(
            "🎁 [LootBox] GetAvailableV1 (page_token=%s)",
            token[:20] if token else "root",
        )
        if token:
            return loot_box_pb2.LootBoxV3GetAvailableV1.Response()

        # Ensure database gacha cache is loaded
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            await ensure_gacha_cache_loaded(session)

        # Active banners from database (ensure 1 is always included for tutorial)
        active_banner_ids = (set(_BANNERS.keys()) | {1}) if _BANNERS else VALID_GACHA_IDS

        # Deliver all products belonging to active banners
        response = loot_box_pb2.LootBoxV3GetAvailableV1.Response()
        seen_pids = set()
        for prod in self.all_template_products:
            pid = prod.loot_product.loot_box_product_id  # type: ignore[attr-defined]
            if pid in seen_pids:
                continue
            seen_pids.add(pid)
            parts = pid.split("_")
            gid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            if gid in active_banner_ids:
                p = response.loot_box_products.add()
                p.CopyFrom(prod)  # type: ignore[arg-type]
                p.loot_product.opened_at = 1592373600
                p.loot_product.closed_at = 4102498799
                p.purchased_count = 0

        response.next_page_token = ""
        return response

    @fail_internal_on_error("Failed to complete a loot box purchase")
    async def PurchaseV1(
        self,
        request: loot_box_pb2.LootBoxV3PurchaseV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> loot_box_pb2.LootBoxV3PurchaseV1.Response:
        current_pid = get_current_player_id()
        if not current_pid:
            logger.warning("🎁 [LootBox] PurchaseV1 called without player session context")
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return loot_box_pb2.LootBoxV3PurchaseV1.Response()

        pid = request.loot_box_product_id
        parts = pid.split("_")
        gacha_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 159
        is_ten_pull = "_10_" in pid or request.purchase_num == 10
        pull_count = (
            request.purchase_num if request.purchase_num > 0 else (10 if is_ten_pull else 1)
        )
        is_pt = (
            "_PT_" in pid
            or request.resource_type == loot_box_pb2.LootBoxV3PurchaseV1.Request.PLAYER_KEY_VALUE
        )
        is_free_or_ticket = "_GT" in pid or "_AN_" in pid or "ticket" in pid.lower()
        is_vc = ("_VC_" in pid) or ("_TT_" in pid) or (not is_pt and not is_free_or_ticket)
        stone_cost = (1500 if (is_ten_pull or pull_count == 10) else 150) if is_vc else 0
        point_cost = (5 * pull_count) if is_pt else 0
        now = int(time.time())

        point_remaining: int | None = None
        # Atomic currency handling and card persistence
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            await ensure_gacha_cache_loaded(session)

            # Master data is authoritative where it actually carries a price: the
            # heuristic above priced the tutorial ten-pull (``G_1_10_TT_L``) as 1500
            # stones although master says it consumes the ``tutorial`` key, and its
            # default gacha id 159 is not a product that exists at all.
            master_price = await _master_price(session, pid)
            if master_price is not None:
                pull_count = master_price.pull_count or pull_count
                if master_price.stone_cost:
                    stone_cost, point_cost, is_vc, is_pt = master_price.stone_cost, 0, True, False
                elif master_price.point_cost:
                    stone_cost, point_cost, is_vc, is_pt = 0, master_price.point_cost, False, True
                else:
                    # Master describes a ticket/key consumption here, not a price
                    # (``G_1_10_TT_L`` consumes the ``tutorial`` key with amount -1).
                    # Charging nothing would hand out free ten-pulls, so keep the
                    # built-in price and say exactly what master declared.
                    logger.warning(
                        "🎁 [LootBox] master lists %s as consuming %s with no numeric price; "
                        "keeping the built-in price (stone=%d, pt=%d)",
                        pid,
                        master_price.resource_label,
                        stone_cost,
                        point_cost,
                    )
            else:
                logger.warning(
                    "🎁 [LootBox] %s is absent from master_loot_box_products; "
                    "pricing it with the built-in heuristic",
                    pid,
                )

            logger.info(
                "🎁 [LootBox] PurchaseV1 player=%s product=%s (gacha=%d, count=%d, "
                "stone_cost=%d, pt_cost=%d, tx=%s)",
                current_pid,
                pid,
                gacha_id,
                pull_count,
                stone_cost,
                point_cost,
                request.transaction_id,
            )

            # Idempotency gate: the first delivery of a transaction owns the purchase,
            # every later one replays it. Without this a client retry charges again
            # and rolls a second set of cards for one pull.
            transaction_id = (request.transaction_id or "").strip()
            replayed = False
            drawn_cards: list[int] = []
            if transaction_id:
                inserted = await session.execute(
                    pg_insert(PurchaseReceipt)
                    .values(
                        player_id=current_pid,
                        transaction_id=transaction_id,
                        product_id=pid,
                        card_ids="[]",
                        created_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=["player_id", "transaction_id"])
                )
                if not inserted.rowcount:
                    replayed = True
                    stored = await session.get(PurchaseReceipt, (current_pid, transaction_id))
                    if stored is not None and stored.card_ids:
                        drawn_cards = [int(card) for card in json.loads(stored.card_ids)]
                    logger.info(
                        "🎁 [LootBox] transaction %s was already accepted; replaying %d card(s) "
                        "without charging",
                        transaction_id,
                        len(drawn_cards),
                    )
            else:
                logger.warning(
                    "🎁 [LootBox] purchase without a transaction_id for player %s; "
                    "it cannot be deduplicated",
                    current_pid,
                )

            if replayed:
                wallet = await get_or_create_wallet(session, current_pid)
                free_remaining, paid_remaining = wallet.free_stones, wallet.paid_stones
                point_remaining = await get_player_key_value(
                    session, current_pid, "point", default=8612
                )
            else:
                if stone_cost > 0:
                    try:
                        free_remaining, paid_remaining = await deduct_stones(
                            session, current_pid, stone_cost
                        )
                    except InsufficientFundsError as err:
                        logger.warning(
                            "🎁 [LootBox] Insufficient funds for %s: %s", current_pid, err
                        )
                        await context.abort(
                            grpc.StatusCode.RESOURCE_EXHAUSTED, "Insufficient virtual currencies"
                        )
                        return loot_box_pb2.LootBoxV3PurchaseV1.Response()
                else:
                    wallet = await get_or_create_wallet(session, current_pid)
                    free_remaining, paid_remaining = wallet.free_stones, wallet.paid_stones

                if point_cost > 0:
                    try:
                        point_remaining = await deduct_player_key_value(
                            session, current_pid, "point", point_cost, default=8612
                        )
                    except InsufficientFundsError as err:
                        logger.warning(
                            "🎁 [LootBox] Insufficient points for %s: %s", current_pid, err
                        )
                        await context.abort(
                            grpc.StatusCode.RESOURCE_EXHAUSTED, "Insufficient dream points"
                        )
                        return loot_box_pb2.LootBoxV3PurchaseV1.Response()
                else:
                    point_remaining = await get_player_key_value(
                        session, current_pid, "point", default=8612
                    )

                banner = await session.get(GachaBanner, gacha_id)
                has_guaranteed_ssr = (
                    (banner.guaranteed_ssr if banner else False)
                    or gacha_id in (1, 3, 163, 167, 171)
                    or "SSR" in pid
                    or "ssrGachaTicket" in pid
                )

                for i in range(pull_count):
                    is_last = i == pull_count - 1
                    is_guaranteed_ssr = (
                        is_last and (is_ten_pull or pull_count == 10) and has_guaranteed_ssr
                    ) or ("ssrGachaTicket" in pid or "pickupSsrGachaTicket" in pid)
                    is_guaranteed_sr = (
                        is_last and (is_ten_pull or pull_count == 10) and not is_guaranteed_ssr
                    )
                    card_id = _roll_card(
                        gacha_id,
                        is_guaranteed_sr=is_guaranteed_sr,
                        is_guaranteed_ssr=is_guaranteed_ssr,
                    )
                    drawn_cards.append(card_id)

                if transaction_id:
                    # Same transaction as the charge: a crash leaves neither behind.
                    await session.execute(
                        update(PurchaseReceipt)
                        .where(
                            PurchaseReceipt.player_id == current_pid,
                            PurchaseReceipt.transaction_id == transaction_id,
                        )
                        .values(card_ids=json.dumps(drawn_cards))
                    )

            await session.commit()

        response = loot_box_pb2.LootBoxV3PurchaseV1.Response()
        response.transaction_id = request.transaction_id

        # Generate prizes
        prizes = response.loot_box_content_set_prizes.add()
        prizes.loot_box_content_set_id = f"P_{pid}"

        for i, card_id in enumerate(drawn_cards):
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

            # 2. player_inventories
            inv = response.player_inventories.add()
            inv.id = str(uuid.uuid4())
            inv.player_id = current_pid
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

        if (is_ten_pull or pull_count == 10) and is_vc:
            extra_inv = response.extra_player_inventories.add()
            extra_inv.id = str(uuid.uuid4())
            extra_inv.player_id = current_pid
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

        logger.info(
            "🎉 [LootBox] Player %s drew %d cards from gacha %d: %s (remaining free=%d, paid=%d, pt=%s)",
            current_pid,
            pull_count,
            gacha_id,
            drawn_cards,
            free_remaining,
            paid_remaining,
            point_remaining,
        )

        # Update wallet balance in response
        vc_free = response.wallet.virtual_currencies.add()
        vc_free.virtual_currency_name = "FREE_STONE"
        vc_free.amount = free_remaining

        vc_paid = response.wallet.virtual_currencies.add()
        vc_paid.virtual_currency_name = "PAID_STONE"
        vc_paid.amount = paid_remaining

        if point_remaining is not None:
            pkv = response.wallet.player_key_values.add()
            pkv.key = "point"
            pkv.amount = point_remaining

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
