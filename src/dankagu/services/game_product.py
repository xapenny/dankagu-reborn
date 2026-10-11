"""Takasho GameProduct servicer for shop products and item purchases."""

import logging
import time

import grpc
from sqlalchemy.dialects.postgresql import insert as pg_insert

from dankagu.config import settings
from dankagu.core.database import get_sessionmaker
from dankagu.core.economy import InsufficientFundsError, deduct_stones
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
from dankagu.models.storage import PlayerStorageEntry

logger = logging.getLogger("dankagu.services.game_product")


class GameProductService(game_product_pb2_grpc.GameProductServicer):
    """Servicer for GameProduct listings and purchases."""

    def __init__(self) -> None:
        self.pages: dict[str, game_product_pb2.GameProductGetAvailableV1.Response] = {}

    async def _ensure_loaded(self) -> None:
        if self.pages:
            return

        import base64
        import json
        from google.protobuf import json_format
        from sqlalchemy import select

        from dankagu.models.master import MasterGameProduct

        try:
            session_factory = get_sessionmaker()
            async with session_factory() as session:
                result = await session.execute(select(MasterGameProduct))
                rows = result.scalars().all()
                raw_list = [r.data for r in rows]

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
                "🛍️ Loaded %d pages of game products from PostgreSQL (root has %d items)",
                len(self.pages),
                len(
                    self.pages.get(
                        "", game_product_pb2.GameProductGetAvailableV1.Response()
                    ).game_products
                ),
            )
        except Exception as e:
            logger.error("Failed to load game products from database: %s", e)

    def _find_product(self, product_id: str) -> ProtoGameProduct | None:
        """Find a game product by its ID across all cached pages."""
        for page in self.pages.values():
            for prod in page.game_products:
                if prod.game_product_id == product_id:
                    return prod
        return None

    def _get_product_cost(self, product_id: str) -> int:
        """Determine stone cost for a game product."""
        if product_id == "SystemGameProduct_1":
            return 50  # Live stage continue
        elif product_id == "SystemGameProduct_3":
            return 100  # TierSkip
        return 50

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

        cost = self._get_product_cost(request.game_product_id)
        logger.info(
            "🛍️ [GameProduct] PurchaseV1 player=%s product=%s cost=%d",
            current_pid,
            request.game_product_id,
            cost,
        )

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            try:
                free_remaining, paid_remaining = await deduct_stones(session, current_pid, cost)
                await session.commit()
            except InsufficientFundsError as err:
                logger.warning("🛍️ Purchase rejected: %s", err)
                await context.abort(
                    grpc.StatusCode.RESOURCE_EXHAUSTED, "Insufficient virtual currencies"
                )
                return game_product_pb2.GameProductPurchaseV1.Response()

        response = game_product_pb2.GameProductPurchaseV1.Response()
        response.transaction_id = request.transaction_id

        vc_free = response.wallet.virtual_currencies.add()
        vc_free.virtual_currency_name = "FREE_STONE"
        vc_free.amount = free_remaining

        vc_paid = response.wallet.virtual_currencies.add()
        vc_paid.virtual_currency_name = "PAID_STONE"
        vc_paid.amount = paid_remaining

        prod = self._find_product(request.game_product_id)
        if prod:
            response.game_product.CopyFrom(prod)

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
        cost = self._get_product_cost(request.game_product_id) * num
        logger.info(
            "🛍️ [GameProduct] PurchaseV2 player=%s product=%s num=%d total_cost=%d",
            current_pid,
            request.game_product_id,
            num,
            cost,
        )

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            try:
                free_remaining, paid_remaining = await deduct_stones(session, current_pid, cost)
                await session.commit()
            except InsufficientFundsError as err:
                logger.warning("🛍️ Purchase rejected: %s", err)
                await context.abort(
                    grpc.StatusCode.RESOURCE_EXHAUSTED, "Insufficient virtual currencies"
                )
                return game_product_pb2.GameProductPurchaseV2.Response()

        response = game_product_pb2.GameProductPurchaseV2.Response()
        response.transaction_id = request.transaction_id

        vc_free = response.wallet.virtual_currencies.add()
        vc_free.virtual_currency_name = "FREE_STONE"
        vc_free.amount = free_remaining

        vc_paid = response.wallet.virtual_currencies.add()
        vc_paid.virtual_currency_name = "PAID_STONE"
        vc_paid.amount = paid_remaining

        prod = self._find_product(request.game_product_id)
        if prod:
            response.game_product.CopyFrom(prod)

        return response

    async def PurchaseAndSavePlayerStorageV1(
        self,
        request: game_product_pb2.GameProductPurchaseAndSavePlayerStorageV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> game_product_pb2.GameProductPurchaseAndSavePlayerStorageV1.Response:
        current_pid = get_current_player_id()
        if not current_pid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return game_product_pb2.GameProductPurchaseAndSavePlayerStorageV1.Response()

        cost = self._get_product_cost(request.game_product_id)
        logger.info(
            "🛍️ [GameProduct] PurchaseAndSavePlayerStorageV1 player=%s product=%s cost=%d entries=%d",
            current_pid,
            request.game_product_id,
            cost,
            len(request.entries),
        )

        now = int(time.time())
        response = game_product_pb2.GameProductPurchaseAndSavePlayerStorageV1.Response()
        response.transaction_id = request.transaction_id

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            try:
                free_remaining, paid_remaining = await deduct_stones(session, current_pid, cost)
            except InsufficientFundsError as err:
                logger.warning("🛍️ Purchase rejected: %s", err)
                await context.abort(
                    grpc.StatusCode.RESOURCE_EXHAUSTED, "Insufficient virtual currencies"
                )
                return game_product_pb2.GameProductPurchaseAndSavePlayerStorageV1.Response()

            # Save entries
            for proto_entry in request.entries:
                upsert_stmt = pg_insert(PlayerStorageEntry).values(
                    player_id=current_pid,
                    key=proto_entry.key,
                    value=proto_entry.value,
                    created_at=proto_entry.created_at or now,
                    updated_at=now,
                )
                upsert_stmt = upsert_stmt.on_conflict_do_update(
                    index_elements=[PlayerStorageEntry.player_id, PlayerStorageEntry.key],
                    set_={
                        "value": upsert_stmt.excluded.value,
                        "updated_at": upsert_stmt.excluded.updated_at,
                    },
                )
                await session.execute(upsert_stmt)

                resp_entry = response.entries.add()
                resp_entry.player_id = current_pid
                resp_entry.key = proto_entry.key
                resp_entry.value = proto_entry.value
                resp_entry.created_at = proto_entry.created_at or now
                resp_entry.updated_at = now

            await session.commit()

        vc_free = response.wallet.virtual_currencies.add()
        vc_free.virtual_currency_name = "FREE_STONE"
        vc_free.amount = free_remaining

        vc_paid = response.wallet.virtual_currencies.add()
        vc_paid.virtual_currency_name = "PAID_STONE"
        vc_paid.amount = paid_remaining

        response.revision = await StorageRevisionManager.set_revision_and_save(
            request.next_revision, player_id=current_pid
        )

        prod = self._find_product(request.game_product_id)
        if prod:
            response.game_product.CopyFrom(prod)

        return response

    async def PurchaseAndSavePlayerStorageV2(
        self,
        request: game_product_pb2.GameProductPurchaseAndSavePlayerStorageV2.Request,
        context: grpc.aio.ServicerContext,
    ) -> game_product_pb2.GameProductPurchaseAndSavePlayerStorageV2.Response:
        current_pid = get_current_player_id()
        if not current_pid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return game_product_pb2.GameProductPurchaseAndSavePlayerStorageV2.Response()

        num = max(1, request.purchase_num)
        cost = self._get_product_cost(request.game_product_id) * num
        logger.info(
            "🛍️ [GameProduct] PurchaseAndSavePlayerStorageV2 player=%s product=%s num=%d cost=%d entries=%d",
            current_pid,
            request.game_product_id,
            num,
            cost,
            len(request.entries),
        )

        now = int(time.time())
        response = game_product_pb2.GameProductPurchaseAndSavePlayerStorageV2.Response()
        response.transaction_id = request.transaction_id

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            try:
                free_remaining, paid_remaining = await deduct_stones(session, current_pid, cost)
            except InsufficientFundsError as err:
                logger.warning("🛍️ Purchase rejected: %s", err)
                await context.abort(
                    grpc.StatusCode.RESOURCE_EXHAUSTED, "Insufficient virtual currencies"
                )
                return game_product_pb2.GameProductPurchaseAndSavePlayerStorageV2.Response()

            # Save entries
            for proto_entry in request.entries:
                upsert_stmt = pg_insert(PlayerStorageEntry).values(
                    player_id=current_pid,
                    key=proto_entry.key,
                    value=proto_entry.value,
                    created_at=proto_entry.created_at or now,
                    updated_at=now,
                )
                upsert_stmt = upsert_stmt.on_conflict_do_update(
                    index_elements=[PlayerStorageEntry.player_id, PlayerStorageEntry.key],
                    set_={
                        "value": upsert_stmt.excluded.value,
                        "updated_at": upsert_stmt.excluded.updated_at,
                    },
                )
                await session.execute(upsert_stmt)

                resp_entry = response.entries.add()
                resp_entry.player_id = current_pid
                resp_entry.key = proto_entry.key
                resp_entry.value = proto_entry.value
                resp_entry.created_at = proto_entry.created_at or now
                resp_entry.updated_at = now

            await session.commit()

        vc_free = response.wallet.virtual_currencies.add()
        vc_free.virtual_currency_name = "FREE_STONE"
        vc_free.amount = free_remaining

        vc_paid = response.wallet.virtual_currencies.add()
        vc_paid.virtual_currency_name = "PAID_STONE"
        vc_paid.amount = paid_remaining

        response.revision = await StorageRevisionManager.set_revision_and_save(
            request.next_revision, player_id=current_pid
        )

        prod = self._find_product(request.game_product_id)
        if prod:
            response.game_product.CopyFrom(prod)

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
