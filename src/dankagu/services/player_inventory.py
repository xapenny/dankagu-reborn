"""Takasho PlayerInventory servicer for user inventories and mailbox gifts."""

import json
import logging
import time

import grpc
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from dankagu.core.database import get_sessionmaker
from dankagu.core.economy import add_stones, get_or_create_wallet
from dankagu.core.growth import add_growth_items
from dankagu.core.session import get_current_player_id
from dankagu.core.storage_state import StorageRevisionManager
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_inventory_pb2,
    player_inventory_pb2_grpc,
)
from dankagu.models.inventory import PlayerInventoryItem
from dankagu.models.storage import PlayerStorageEntry

logger = logging.getLogger("dankagu.services.player_inventory")


class PlayerInventoryService(player_inventory_pb2_grpc.PlayerInventoryServicer):
    """Servicer for player inventory items and receiving rewards."""

    async def GetInventoriesV1(
        self,
        request: player_inventory_pb2.PlayerInventoryGetInventoriesV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_inventory_pb2.PlayerInventoryGetInventoriesV1.Response:
        current_pid = get_current_player_id()
        logger.info("📦 [PlayerInventory] GetInventoriesV1 (player=%s)", current_pid)
        response = player_inventory_pb2.PlayerInventoryGetInventoriesV1.Response()
        if not current_pid:
            return response

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = (
                select(PlayerInventoryItem)
                .where(
                    PlayerInventoryItem.player_id == current_pid,
                    PlayerInventoryItem.is_claimed == False,  # noqa: E712
                )
                .order_by(PlayerInventoryItem.created_at.desc())
            )
            res = await session.execute(stmt)
            items = res.scalars().all()

        for item in items:
            inv = response.inventories.add()
            inv.id = item.id
            inv.player_id = item.player_id
            inv.item_type = item.item_type
            inv.schema_key = item.schema_key
            inv.value = item.value
            inv.route = item.route
            inv.message = item.message
            inv.search_label = item.search_label
            inv.opened_at = item.opened_at
            inv.expired_at = item.expired_at
            inv.system_resource_name = item.system_resource_name
            inv.system_resource_num = item.system_resource_num
            inv.created_at = item.created_at

        return response

    async def GetInventoriesAndCountV1(
        self,
        request: player_inventory_pb2.PlayerInventoryGetInventoriesAndCountV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_inventory_pb2.PlayerInventoryGetInventoriesAndCountV1.Response:
        current_pid = get_current_player_id()
        logger.info("📦 [PlayerInventory] GetInventoriesAndCountV1 (player=%s)", current_pid)
        response = player_inventory_pb2.PlayerInventoryGetInventoriesAndCountV1.Response()
        if not current_pid:
            return response

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = (
                select(PlayerInventoryItem)
                .where(
                    PlayerInventoryItem.player_id == current_pid,
                    PlayerInventoryItem.is_claimed == False,  # noqa: E712
                )
                .order_by(PlayerInventoryItem.created_at.desc())
            )
            res = await session.execute(stmt)
            items = res.scalars().all()

        for item in items:
            inv = response.inventories.add()
            inv.id = item.id
            inv.player_id = item.player_id
            inv.item_type = item.item_type
            inv.schema_key = item.schema_key
            inv.value = item.value
            inv.route = item.route
            inv.message = item.message
            inv.search_label = item.search_label
            inv.opened_at = item.opened_at
            inv.expired_at = item.expired_at
            inv.system_resource_name = item.system_resource_name
            inv.system_resource_num = item.system_resource_num
            inv.created_at = item.created_at

        response.inventories_count = len(response.inventories)
        return response

    async def GetReceivedInventoriesV1(
        self,
        request: player_inventory_pb2.PlayerInventoryGetReceivedInventoriesV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_inventory_pb2.PlayerInventoryGetReceivedInventoriesV1.Response:
        current_pid = get_current_player_id()
        logger.info("📦 [PlayerInventory] GetReceivedInventoriesV1 (player=%s)", current_pid)
        response = player_inventory_pb2.PlayerInventoryGetReceivedInventoriesV1.Response()
        if not current_pid:
            return response

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = (
                select(PlayerInventoryItem)
                .where(
                    PlayerInventoryItem.player_id == current_pid,
                    PlayerInventoryItem.is_claimed == True,  # noqa: E712
                )
                .order_by(PlayerInventoryItem.received_at.desc())
                .limit(100)
            )
            res = await session.execute(stmt)
            items = res.scalars().all()

        for item in items:
            r = response.received_inventories.add()
            r.id = item.id
            r.original_inventory_id = item.id
            r.player_id = item.player_id
            r.item_type = item.item_type
            r.schema_key = item.schema_key
            r.value = item.value
            r.route = item.route
            r.message = item.message
            r.search_label = item.search_label
            r.opened_at = item.opened_at
            r.expired_at = item.expired_at
            r.system_resource_name = item.system_resource_name
            r.system_resource_num = item.system_resource_num
            r.created_at = item.created_at
            r.received_at = item.received_at or item.created_at

        return response

    async def ReceiveV1(
        self,
        request: player_inventory_pb2.PlayerInventoryReceiveV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_inventory_pb2.PlayerInventoryReceiveV1.Response:
        current_pid = get_current_player_id()
        if not current_pid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return player_inventory_pb2.PlayerInventoryReceiveV1.Response()

        ids_to_claim = list(request.delete_inventory_ids)
        logger.info(
            "📦 [PlayerInventory] ReceiveV1 player=%s (%d gifts to claim, %d entries)",
            current_pid,
            len(ids_to_claim),
            len(request.entries),
        )

        response = player_inventory_pb2.PlayerInventoryReceiveV1.Response()
        now = int(time.time())

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # 1. Claim items
            if ids_to_claim:
                stmt = select(PlayerInventoryItem).where(
                    PlayerInventoryItem.player_id == current_pid,
                    PlayerInventoryItem.id.in_(ids_to_claim),
                    PlayerInventoryItem.is_claimed == False,  # noqa: E712
                )
                res = await session.execute(stmt)
                items = res.scalars().all()

                for item in items:
                    stones_to_add = 0
                    if item.system_resource_name in ("FREE_STONE", "VirtualCurrency"):
                        stones_to_add = item.system_resource_num
                    else:
                        try:
                            parsed = json.loads(item.value.decode("utf-8"))
                            if (
                                parsed.get("Prefix") in ("VirtualCurrency", "Stone")
                                or parsed.get("Value") == "FREE_STONE"
                            ):
                                stones_to_add = int(parsed.get("Count", 0))
                        except Exception:
                            pass

                    if stones_to_add > 0:
                        await add_stones(session, current_pid, free_amount=stones_to_add)

                    # Check for growth items
                    growth_item_ids: list[int] = []
                    growth_count = item.system_resource_num or 500
                    try:
                        parsed = json.loads(item.value.decode("utf-8"))
                        if parsed.get("Prefix") == "GrowthItem":
                            if "Items" in parsed:
                                growth_item_ids = parsed["Items"]
                            elif "Item" in parsed:
                                growth_item_ids = [parsed["Item"]]
                            growth_count = parsed.get("Count", growth_count)
                    except Exception:
                        pass

                    if growth_item_ids:
                        await add_growth_items(session, current_pid, growth_item_ids, count=growth_count)

                    item.is_claimed = True
                    item.received_at = now

            # 2. Save any storage entries sent by client
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

            # 3. Fetch latest wallet
            wallet = await get_or_create_wallet(session, current_pid)
            free_stones = wallet.free_stones
            paid_stones = wallet.paid_stones

            await session.commit()

        # Update wallet in response
        vc_free = response.wallet.virtual_currencies.add()
        vc_free.virtual_currency_name = "FREE_STONE"
        vc_free.amount = free_stones

        vc_paid = response.wallet.virtual_currencies.add()
        vc_paid.virtual_currency_name = "PAID_STONE"
        vc_paid.amount = paid_stones

        response.revision = await StorageRevisionManager.set_revision_and_save(
            request.next_revision, player_id=current_pid
        )
        return response


def register_player_inventory_servicer(server: grpc.aio.Server) -> None:
    servicer = PlayerInventoryService()
    method_handlers = {
        "GetInventoriesV1": takasho_unary_handler(
            servicer.GetInventoriesV1,
            player_inventory_pb2.PlayerInventoryGetInventoriesV1.Request,
            player_inventory_pb2.PlayerInventoryGetInventoriesV1.Response,
        ),
        "GetReceivedInventoriesV1": takasho_unary_handler(
            servicer.GetReceivedInventoriesV1,
            player_inventory_pb2.PlayerInventoryGetReceivedInventoriesV1.Request,
            player_inventory_pb2.PlayerInventoryGetReceivedInventoriesV1.Response,
        ),
        "ReceiveV1": takasho_unary_handler(
            servicer.ReceiveV1,
            player_inventory_pb2.PlayerInventoryReceiveV1.Request,
            player_inventory_pb2.PlayerInventoryReceiveV1.Response,
        ),
        "GetInventoriesAndCountV1": takasho_unary_handler(
            servicer.GetInventoriesAndCountV1,
            player_inventory_pb2.PlayerInventoryGetInventoriesAndCountV1.Request,
            player_inventory_pb2.PlayerInventoryGetInventoriesAndCountV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.PlayerInventory", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
