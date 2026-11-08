"""Takasho GameProduct servicer for shop products and item purchases."""

import base64
import json
import logging
import time
import uuid

import grpc
from google.protobuf import json_format
from sqlalchemy import select

from dankagu.core.database import get_sessionmaker
from dankagu.core.economy import (
    InsufficientFundsError,
    deduct_stones,
    get_or_create_wallet,
)
from dankagu.core.metadata import metadata
from dankagu.core.session import get_current_player_id
from dankagu.core.storage_state import StorageRevisionManager
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    game_product_pb2,
    game_product_pb2_grpc,
)
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.game_product.v1_pb2 import (
    GameProduct as ProtoGameProduct,
)
from dankagu.models.inventory import PlayerInventoryItem
from dankagu.models.storage import PlayerKeyValue
from dankagu.services.box_loot_box import encode_raw_protobuf
from dankagu.services.player_storage import _upsert_entry, batch_upsert_storage_entries

logger = logging.getLogger("dankagu.services.game_product")


class GameProductService(game_product_pb2_grpc.GameProductServicer):
    """Servicer for GameProduct listings and purchases."""

    def __init__(self) -> None:
        self.pages: dict[str, game_product_pb2.GameProductGetAvailableV1.Response] = {}

    async def _ensure_loaded(self) -> None:
        if self.pages:
            return

        try:
            raw_list = metadata.get_game_products()
            chunk_size = 100
            chunks = [raw_list[i : i + chunk_size] for i in range(0, len(raw_list), chunk_size)]
            for page_idx, chunk in enumerate(chunks, start=1):
                resp = game_product_pb2.GameProductGetAvailableV1.Response()
                for item in chunk:
                    prod = resp.game_products.add()
                    json_format.ParseDict(item, prod, ignore_unknown_fields=True)

                if page_idx < len(chunks):
                    next_obj = {"MaxResults": 100, "Page": page_idx + 1, "Options": ""}
                    resp.next_page_token = base64.b64encode(
                        json.dumps(next_obj, separators=(",", ":")).encode("utf-8")
                    ).decode("utf-8")
                else:
                    resp.next_page_token = ""

                cur_token = (
                    ""
                    if page_idx == 1
                    else base64.b64encode(
                        json.dumps(
                            {"MaxResults": 100, "Page": page_idx, "Options": ""},
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).decode("utf-8")
                )
                self.pages[cur_token] = resp

            logger.info(
                "🛍️ Loaded %d pages of game products from metadata (root has %d items)",
                len(self.pages),
                len(
                    self.pages.get(
                        "", game_product_pb2.GameProductGetAvailableV1.Response()
                    ).game_products
                ),
            )
        except Exception as e:
            logger.error("Failed to load game products from metadata: %s", e)

    def _find_product(self, product_id: str) -> ProtoGameProduct | None:
        """Find a game product by its ID across all cached pages."""
        for page in self.pages.values():
            for prod in page.game_products:
                if prod.game_product_id == product_id:
                    return prod
        return None

    def _get_product_cost(self, product_id: str) -> int:
        """Determine stone cost for a game product."""
        if product_id.startswith("ShopProduct_"):
            return 0
        elif product_id == "SystemGameProduct_1":
            return 50  # Live stage continue
        elif product_id == "SystemGameProduct_3":
            return 100  # TierSkip
        return 50

    async def _execute_purchase(
        self,
        session,
        current_pid: str,
        product_id: str,
        num: int,
        now: int,
    ) -> tuple[ProtoGameProduct | None, PlayerInventoryItem | None, int, int, dict[str, int]]:
        prod = self._find_product(product_id)
        if not prod:
            raw_data = metadata.get_game_product(product_id)
            if raw_data:
                prod = ProtoGameProduct()
                json_format.ParseDict(raw_data, prod, ignore_unknown_fields=True)

        free_remaining, paid_remaining = 0, 0
        pkv_dict: dict[str, int] = {}

        if prod and prod.consume_resource_sets:
            for cset in prod.consume_resource_sets:
                # ResourceType: 1 = PLAYER_KEY_VALUE
                if cset.resource_type == 1:
                    ticket_key = (
                        cset.consume_resources[0].resource_key if cset.consume_resources else ""
                    )
                    cost = abs(cset.amount) * num
                    stmt = select(PlayerKeyValue).where(
                        PlayerKeyValue.player_id == current_pid,
                        PlayerKeyValue.key == ticket_key,
                    )
                    pkv_row = (await session.execute(stmt)).scalars().first()
                    curr_val = pkv_row.value if pkv_row else 0
                    if curr_val < cost:
                        raise InsufficientFundsError(
                            f"Insufficient {ticket_key} (need {cost}, have {curr_val})"
                        )
                    pkv_row.value = curr_val - cost
                    pkv_row.updated_at = now
                    remaining_tickets = pkv_row.value
                    pkv_dict[ticket_key] = remaining_tickets

                    # Sync to PlayerStorageEntry
                    await _upsert_entry(
                        session,
                        current_pid,
                        f"PlayerKeyValue:{ticket_key}",
                        str(remaining_tickets).encode("utf-8"),
                        now,
                    )
                    if ticket_key.startswith("selectTicket_"):
                        g_str = ticket_key.replace("selectTicket_", "")
                        if g_str.isdigit():
                            g_id = int(g_str)
                            ticket_proto = encode_raw_protobuf(
                                {
                                    1: [(0, g_id)],
                                    2: [(0, g_id)],
                                    3: [(0, remaining_tickets)],
                                }
                            )
                            await _upsert_entry(
                                session,
                                current_pid,
                                f"GachaTicketExchangeStatus--{g_id}",
                                ticket_proto,
                                now,
                            )
                # ResourceType: 0 = VIRTUAL_CURRENCY
                elif cset.resource_type == 0:
                    cost = abs(cset.amount) * num
                    free_remaining, paid_remaining = await deduct_stones(session, current_pid, cost)

            if not any(c.resource_type == 0 for c in prod.consume_resource_sets):
                wallet = await get_or_create_wallet(session, current_pid)
                free_remaining, paid_remaining = wallet.free_stones, wallet.paid_stones
        else:
            cost = self._get_product_cost(product_id) * num
            if cost > 0:
                free_remaining, paid_remaining = await deduct_stones(session, current_pid, cost)
            else:
                wallet = await get_or_create_wallet(session, current_pid)
                free_remaining, paid_remaining = wallet.free_stones, wallet.paid_stones

        # Create PlayerInventoryItem if product yields an item
        inv_item: PlayerInventoryItem | None = None
        if prod:
            inv_id = str(uuid.uuid4())
            schema_key = prod.schema_key or "ITEM"
            value_bytes = prod.value
            search_label = prod.search_label or "Card"
            receivable_sec = int(prod.receivable_sec) if prod.receivable_sec else 2419200
            inv_item = PlayerInventoryItem(
                id=inv_id,
                player_id=current_pid,
                item_type=1,
                schema_key=schema_key,
                value=value_bytes,
                route=4,
                message="交換所で獲得した商品です",
                search_label=search_label,
                opened_at=now,
                expired_at=now + receivable_sec,
                system_resource_name="ShopProduct",
                system_resource_num=num,
                is_claimed=False,
                created_at=now,
            )
            session.add(inv_item)

        return prod, inv_item, free_remaining, paid_remaining, pkv_dict

    async def GetAvailableV1(
        self,
        request: game_product_pb2.GameProductGetAvailableV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> game_product_pb2.GameProductGetAvailableV1.Response:
        await self._ensure_loaded()
        token = request.page_token or ""
        logger.info(
            "🛍️ [GameProduct] GetAvailableV1 (page_token=%s)", token[:20] if token else "root"
        )
        if token in self.pages:
            return self.pages[token]
        return self.pages.get("", game_product_pb2.GameProductGetAvailableV1.Response())

    async def PurchaseV1(
        self,
        request: game_product_pb2.GameProductPurchaseV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> game_product_pb2.GameProductPurchaseV1.Response:
        await self._ensure_loaded()
        current_pid = get_current_player_id()
        if not current_pid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return game_product_pb2.GameProductPurchaseV1.Response()

        now = int(time.time())
        logger.info(
            "🛍️ [GameProduct] PurchaseV1 player=%s product=%s",
            current_pid,
            request.game_product_id,
        )

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            try:
                prod, inv_item, free_rem, paid_rem, pkv_dict = await self._execute_purchase(
                    session, current_pid, request.game_product_id, 1, now
                )
                await session.commit()
            except InsufficientFundsError as err:
                logger.warning("🛍️ Purchase rejected: %s", err)
                await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, str(err))
                return game_product_pb2.GameProductPurchaseV1.Response()

        response = game_product_pb2.GameProductPurchaseV1.Response()
        response.transaction_id = request.transaction_id

        vc_free = response.wallet.virtual_currencies.add()
        vc_free.virtual_currency_name = "FREE_STONE"
        vc_free.amount = free_rem

        vc_paid = response.wallet.virtual_currencies.add()
        vc_paid.virtual_currency_name = "PAID_STONE"
        vc_paid.amount = paid_rem

        for k, v in pkv_dict.items():
            pkv = response.wallet.player_key_values.add()
            pkv.key = k
            pkv.amount = v

        if prod:
            response.game_product.CopyFrom(prod)

        if inv_item:
            response.player_inventory.id = inv_item.id
            response.player_inventory.player_id = inv_item.player_id
            response.player_inventory.item_type = inv_item.item_type
            response.player_inventory.schema_key = inv_item.schema_key
            response.player_inventory.value = inv_item.value
            response.player_inventory.route = inv_item.route
            response.player_inventory.message = inv_item.message
            response.player_inventory.search_label = inv_item.search_label
            response.player_inventory.opened_at = inv_item.opened_at
            response.player_inventory.expired_at = inv_item.expired_at
            response.player_inventory.system_resource_name = inv_item.system_resource_name
            response.player_inventory.system_resource_num = inv_item.system_resource_num
            response.player_inventory.created_at = inv_item.created_at

        return response

    async def PurchaseV2(
        self,
        request: game_product_pb2.GameProductPurchaseV2.Request,
        context: grpc.aio.ServicerContext,
    ) -> game_product_pb2.GameProductPurchaseV2.Response:
        await self._ensure_loaded()
        current_pid = get_current_player_id()
        if not current_pid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return game_product_pb2.GameProductPurchaseV2.Response()

        num = max(1, request.purchase_num)
        now = int(time.time())
        logger.info(
            "🛍️ [GameProduct] PurchaseV2 player=%s product=%s num=%d",
            current_pid,
            request.game_product_id,
            num,
        )

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            try:
                prod, inv_item, free_rem, paid_rem, pkv_dict = await self._execute_purchase(
                    session, current_pid, request.game_product_id, num, now
                )
                await session.commit()
            except InsufficientFundsError as err:
                logger.warning("🛍️ Purchase rejected: %s", err)
                await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, str(err))
                return game_product_pb2.GameProductPurchaseV2.Response()

        response = game_product_pb2.GameProductPurchaseV2.Response()
        response.transaction_id = request.transaction_id

        vc_free = response.wallet.virtual_currencies.add()
        vc_free.virtual_currency_name = "FREE_STONE"
        vc_free.amount = free_rem

        vc_paid = response.wallet.virtual_currencies.add()
        vc_paid.virtual_currency_name = "PAID_STONE"
        vc_paid.amount = paid_rem

        for k, v in pkv_dict.items():
            pkv = response.wallet.player_key_values.add()
            pkv.key = k
            pkv.amount = v

        if prod:
            response.game_product.CopyFrom(prod)

        if inv_item:
            inv = response.player_inventories.add()
            inv.id = inv_item.id
            inv.player_id = inv_item.player_id
            inv.item_type = inv_item.item_type
            inv.schema_key = inv_item.schema_key
            inv.value = inv_item.value
            inv.route = inv_item.route
            inv.message = inv_item.message
            inv.search_label = inv_item.search_label
            inv.opened_at = inv_item.opened_at
            inv.expired_at = inv_item.expired_at
            inv.system_resource_name = inv_item.system_resource_name
            inv.system_resource_num = inv_item.system_resource_num
            inv.created_at = inv_item.created_at

        return response

    async def PurchaseAndSavePlayerStorageV1(
        self,
        request: game_product_pb2.GameProductPurchaseAndSavePlayerStorageV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> game_product_pb2.GameProductPurchaseAndSavePlayerStorageV1.Response:
        await self._ensure_loaded()
        current_pid = get_current_player_id()
        if not current_pid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return game_product_pb2.GameProductPurchaseAndSavePlayerStorageV1.Response()

        now = int(time.time())
        logger.info(
            "🛍️ [GameProduct] PurchaseAndSavePlayerStorageV1 player=%s product=%s entries=%d",
            current_pid,
            request.game_product_id,
            len(request.entries),
        )

        response = game_product_pb2.GameProductPurchaseAndSavePlayerStorageV1.Response()
        response.transaction_id = request.transaction_id

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            try:
                prod, inv_item, free_rem, paid_rem, pkv_dict = await self._execute_purchase(
                    session, current_pid, request.game_product_id, 1, now
                )
            except InsufficientFundsError as err:
                logger.warning("🛍️ Purchase rejected: %s", err)
                await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, str(err))
                return game_product_pb2.GameProductPurchaseAndSavePlayerStorageV1.Response()

            rows_to_upsert = []
            for proto_entry in request.entries:
                rows_to_upsert.append(
                    {
                        "player_id": current_pid,
                        "key": proto_entry.key,
                        "value": proto_entry.value,
                        "created_at": proto_entry.created_at or now,
                        "updated_at": now,
                    }
                )
                resp_entry = response.entries.add()
                resp_entry.player_id = current_pid
                resp_entry.key = proto_entry.key
                resp_entry.value = proto_entry.value
                resp_entry.created_at = proto_entry.created_at or now
                resp_entry.updated_at = now

            if rows_to_upsert:
                await batch_upsert_storage_entries(session, rows_to_upsert)

            revision = await StorageRevisionManager.set_revision_and_save(
                request.next_revision, player_id=current_pid, session=session
            )
            await session.commit()

        vc_free = response.wallet.virtual_currencies.add()
        vc_free.virtual_currency_name = "FREE_STONE"
        vc_free.amount = free_rem

        vc_paid = response.wallet.virtual_currencies.add()
        vc_paid.virtual_currency_name = "PAID_STONE"
        vc_paid.amount = paid_rem

        for k, v in pkv_dict.items():
            pkv = response.wallet.player_key_values.add()
            pkv.key = k
            pkv.amount = v

        response.revision = revision

        if prod:
            response.game_product.CopyFrom(prod)

        if inv_item:
            response.player_inventory.id = inv_item.id
            response.player_inventory.player_id = inv_item.player_id
            response.player_inventory.item_type = inv_item.item_type
            response.player_inventory.schema_key = inv_item.schema_key
            response.player_inventory.value = inv_item.value
            response.player_inventory.route = inv_item.route
            response.player_inventory.message = inv_item.message
            response.player_inventory.search_label = inv_item.search_label
            response.player_inventory.opened_at = inv_item.opened_at
            response.player_inventory.expired_at = inv_item.expired_at
            response.player_inventory.system_resource_name = inv_item.system_resource_name
            response.player_inventory.system_resource_num = inv_item.system_resource_num
            response.player_inventory.created_at = inv_item.created_at

        return response

    async def PurchaseAndSavePlayerStorageV2(
        self,
        request: game_product_pb2.GameProductPurchaseAndSavePlayerStorageV2.Request,
        context: grpc.aio.ServicerContext,
    ) -> game_product_pb2.GameProductPurchaseAndSavePlayerStorageV2.Response:
        await self._ensure_loaded()
        current_pid = get_current_player_id()
        if not current_pid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return game_product_pb2.GameProductPurchaseAndSavePlayerStorageV2.Response()

        num = max(1, request.purchase_num)
        now = int(time.time())
        logger.info(
            "🛍️ [GameProduct] PurchaseAndSavePlayerStorageV2 player=%s product=%s num=%d entries=%d",
            current_pid,
            request.game_product_id,
            num,
            len(request.entries),
        )

        response = game_product_pb2.GameProductPurchaseAndSavePlayerStorageV2.Response()
        response.transaction_id = request.transaction_id

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            try:
                prod, inv_item, free_rem, paid_rem, pkv_dict = await self._execute_purchase(
                    session, current_pid, request.game_product_id, num, now
                )
            except InsufficientFundsError as err:
                logger.warning("🛍️ Purchase rejected: %s", err)
                await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, str(err))
                return game_product_pb2.GameProductPurchaseAndSavePlayerStorageV2.Response()

            rows_to_upsert = []
            for proto_entry in request.entries:
                rows_to_upsert.append(
                    {
                        "player_id": current_pid,
                        "key": proto_entry.key,
                        "value": proto_entry.value,
                        "created_at": proto_entry.created_at or now,
                        "updated_at": now,
                    }
                )
                resp_entry = response.entries.add()
                resp_entry.player_id = current_pid
                resp_entry.key = proto_entry.key
                resp_entry.value = proto_entry.value
                resp_entry.created_at = proto_entry.created_at or now
                resp_entry.updated_at = now

            if rows_to_upsert:
                await batch_upsert_storage_entries(session, rows_to_upsert)

            revision = await StorageRevisionManager.set_revision_and_save(
                request.next_revision, player_id=current_pid, session=session
            )
            await session.commit()

        vc_free = response.wallet.virtual_currencies.add()
        vc_free.virtual_currency_name = "FREE_STONE"
        vc_free.amount = free_rem

        vc_paid = response.wallet.virtual_currencies.add()
        vc_paid.virtual_currency_name = "PAID_STONE"
        vc_paid.amount = paid_rem

        for k, v in pkv_dict.items():
            pkv = response.wallet.player_key_values.add()
            pkv.key = k
            pkv.amount = v

        response.revision = revision

        if prod:
            response.game_product.CopyFrom(prod)

        if inv_item:
            inv = response.player_inventories.add()
            inv.id = inv_item.id
            inv.player_id = inv_item.player_id
            inv.item_type = inv_item.item_type
            inv.schema_key = inv_item.schema_key
            inv.value = inv_item.value
            inv.route = inv_item.route
            inv.message = inv_item.message
            inv.search_label = inv_item.search_label
            inv.opened_at = inv_item.opened_at
            inv.expired_at = inv_item.expired_at
            inv.system_resource_name = inv_item.system_resource_name
            inv.system_resource_num = inv_item.system_resource_num
            inv.created_at = inv_item.created_at

        return response


def register_game_product_servicer(server: grpc.aio.Server) -> None:
    servicer = GameProductService()
    method_handlers = {
        "GetAvailableV1": takasho_unary_handler(
            servicer.GetAvailableV1,
            game_product_pb2.GameProductGetAvailableV1.Request,
            game_product_pb2.GameProductGetAvailableV1.Response,
        ),
        "PurchaseV1": takasho_unary_handler(
            servicer.PurchaseV1,
            game_product_pb2.GameProductPurchaseV1.Request,
            game_product_pb2.GameProductPurchaseV1.Response,
        ),
        "PurchaseV2": takasho_unary_handler(
            servicer.PurchaseV2,
            game_product_pb2.GameProductPurchaseV2.Request,
            game_product_pb2.GameProductPurchaseV2.Response,
        ),
        "PurchaseAndSavePlayerStorageV1": takasho_unary_handler(
            servicer.PurchaseAndSavePlayerStorageV1,
            game_product_pb2.GameProductPurchaseAndSavePlayerStorageV1.Request,
            game_product_pb2.GameProductPurchaseAndSavePlayerStorageV1.Response,
        ),
        "PurchaseAndSavePlayerStorageV2": takasho_unary_handler(
            servicer.PurchaseAndSavePlayerStorageV2,
            game_product_pb2.GameProductPurchaseAndSavePlayerStorageV2.Request,
            game_product_pb2.GameProductPurchaseAndSavePlayerStorageV2.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.GameProduct", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
