"""Takasho PlayerInventory servicer for user inventories and mailbox gifts."""

import json
import logging
import time
from typing import Any

import grpc
from google.protobuf.internal.decoder import _DecodeVarint
from google.protobuf.internal.encoder import _EncodeVarint
from sqlalchemy import delete, func, select

from dankagu.core.database import get_sessionmaker
from dankagu.core.economy import add_stones, get_or_create_wallet
from dankagu.core.growth import (
    PREFIX_NAME_TO_ID,
    SKILL_ITEM_IDS,
    add_generic_item_stocks,
    add_growth_items,
)
from dankagu.core.session import get_current_player_id
from dankagu.core.storage_state import StorageRevisionManager
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.errors import fail_internal, fail_internal_on_error
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_inventory_pb2,
    player_inventory_pb2_grpc,
)
from dankagu.models.inventory import PlayerInventoryItem
from dankagu.models.storage import PlayerStorageEntry
from dankagu.services.player_storage import batch_upsert_storage_entries

logger = logging.getLogger("dankagu.services.player_inventory")


def parse_player_status(data: bytes) -> dict[int, int]:
    """Parse PlayerStatus protobuf binary into field_id -> varint dict."""
    pos = 0
    fields: dict[int, int] = {}
    while pos < len(data):
        tag, pos = _DecodeVarint(data, pos)
        fn = tag >> 3
        wt = tag & 7
        if wt == 0:
            val, pos = _DecodeVarint(data, pos)
            fields[fn] = val
        else:
            break
    return fields


def encode_player_status(fields: dict[int, int]) -> bytes:
    """Encode field_id -> varint dict into PlayerStatus protobuf binary."""
    buf = bytearray()
    for fn in sorted(fields.keys()):
        val = fields[fn]
        tag = (fn << 3) | 0
        _EncodeVarint(buf.extend, tag)
        _EncodeVarint(buf.extend, val)
    return bytes(buf)


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

        limit = 100
        expired_before = 0
        expired_after = 0
        search_labels: list[str] = []
        if request.HasField("criterion"):
            if 0 < request.criterion.count <= 100:
                limit = request.criterion.count
            expired_before = request.criterion.expired_at_before
            expired_after = request.criterion.expired_at_after
            if request.criterion.search_labels:
                search_labels = [s for s in request.criterion.search_labels if s]

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(PlayerInventoryItem).where(
                PlayerInventoryItem.player_id == current_pid,
                PlayerInventoryItem.is_claimed == False,  # noqa: E712
            )
            if search_labels and "NoFilter" not in search_labels:
                stmt = stmt.where(PlayerInventoryItem.search_label.in_(search_labels))
            if expired_before > 0:
                stmt = stmt.where(PlayerInventoryItem.expired_at <= expired_before)
            if expired_after > 0:
                stmt = stmt.where(PlayerInventoryItem.expired_at >= expired_after)
            stmt = stmt.order_by(
                PlayerInventoryItem.created_at.desc(), PlayerInventoryItem.id.asc()
            ).limit(limit)
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

        limit = 100
        expired_before = 0
        expired_after = 0
        search_labels: list[str] = []
        if request.HasField("criterion"):
            if 0 < request.criterion.count <= 100:
                limit = request.criterion.count
            expired_before = request.criterion.expired_at_before
            expired_after = request.criterion.expired_at_after
            if request.criterion.search_labels:
                search_labels = [s for s in request.criterion.search_labels if s]

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            base_filters = [
                PlayerInventoryItem.player_id == current_pid,
                PlayerInventoryItem.is_claimed == False,  # noqa: E712
            ]
            if search_labels and "NoFilter" not in search_labels:
                base_filters.append(PlayerInventoryItem.search_label.in_(search_labels))
            if expired_before > 0:
                base_filters.append(PlayerInventoryItem.expired_at <= expired_before)
            if expired_after > 0:
                base_filters.append(PlayerInventoryItem.expired_at >= expired_after)

            count_stmt = select(func.count()).select_from(PlayerInventoryItem).where(*base_filters)
            total_count = int((await session.execute(count_stmt)).scalar() or 0)

            stmt = (
                select(PlayerInventoryItem)
                .where(*base_filters)
                .order_by(PlayerInventoryItem.created_at.desc(), PlayerInventoryItem.id.asc())
                .limit(limit)
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

        response.inventories_count = total_count
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

    @fail_internal_on_error("Failed to claim mailbox items")
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
        client_entry_keys = {e.key for e in request.entries}

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            rows_to_upsert: list[dict[str, Any]] = []
            keys_to_delete: list[str] = []

            for proto_entry in request.entries:
                if not proto_entry.value:
                    keys_to_delete.append(proto_entry.key)
                    continue

                rows_to_upsert.append(
                    {
                        "player_id": current_pid,
                        "key": proto_entry.key,
                        "value": proto_entry.value,
                        "created_at": proto_entry.created_at or now,
                        "updated_at": now,
                    }
                )

            if keys_to_delete:
                await session.execute(
                    delete(PlayerStorageEntry).where(
                        PlayerStorageEntry.player_id == current_pid,
                        PlayerStorageEntry.key.in_(keys_to_delete),
                    )
                )

            if rows_to_upsert:
                await batch_upsert_storage_entries(session, rows_to_upsert)

            written_map = {row["key"]: row for row in rows_to_upsert}

            for key, row in written_map.items():
                resp_entry = response.entries.add()

                resp_entry.player_id = current_pid

                resp_entry.key = key

                resp_entry.value = row["value"]

                resp_entry.created_at = row["created_at"]

                resp_entry.updated_at = row["updated_at"]

            # 2. Claim items and apply server-side wallet / bonus storage updates
            if ids_to_claim:
                stmt = select(PlayerInventoryItem).where(
                    PlayerInventoryItem.player_id == current_pid,
                    PlayerInventoryItem.id.in_(ids_to_claim),
                    PlayerInventoryItem.is_claimed == False,  # noqa: E712
                )
                res = await session.execute(stmt)
                items = res.scalars().all()

                total_stones_to_add = 0
                total_coin_to_add = 0
                top_up_kaleidoscopes = False
                growth_by_count: dict[int, list[int]] = {}
                generic_by_prefix_and_count: dict[tuple[int, int], list[int]] = {}
                unlocks_by_cat: dict[int, list[int]] = {}

                for item in items:
                    parsed: dict = {}
                    try:
                        parsed = json.loads(item.value.decode("utf-8"))
                    except Exception as exc:
                        # Refuse to guess. The previous fallback kept `parsed = {}`
                        # and then used `system_resource_num or 200` — so an
                        # unreadable mailbox payload silently granted 200 stones.
                        await fail_internal(
                            context,
                            "Mailbox payload is unreadable; refusing to grant a guessed reward",
                            exc,
                            inventory_id=item.id,
                            label=item.search_label,
                        )

                    prefix = str(parsed.get("Prefix", ""))
                    val = parsed.get("Value")
                    count = int(parsed.get("Count", item.system_resource_num or 200))

                    if (
                        item.system_resource_name in ("FREE_STONE", "VirtualCurrency")
                        or prefix in ("VC", "VirtualCurrency", "Stone")
                        or val == "FREE_STONE"
                    ):
                        total_stones_to_add += count

                    # Server-side fallback for Growth items if client didn't send entries
                    if prefix in ("Growth", "GrowthItem"):
                        growth_ids: list[int] = []
                        if isinstance(val, int) and val > 0:
                            if f"GrowthItemStock--{val}" not in client_entry_keys:
                                growth_ids = [val]
                        elif "Items" in parsed:
                            growth_ids = [
                                int(x)
                                for x in parsed["Items"]
                                if f"GrowthItemStock--{x}" not in client_entry_keys
                            ]
                        if growth_ids:
                            growth_by_count.setdefault(count, []).extend(growth_ids)

                    # Server-side fallback for GenericItem / HakoniwaMaterial / BoostHeal / HeroineFragment
                    elif prefix in PREFIX_NAME_TO_ID and isinstance(val, int) and val > 0:
                        prefix_id = PREFIX_NAME_TO_ID[prefix]
                        comp_id = prefix_id * 1_000_000 + val
                        if f"ItemStockStatus--{comp_id}" not in client_entry_keys:
                            generic_by_prefix_and_count.setdefault((prefix_id, count), []).append(
                                val
                            )

                        # When claiming Kaleidoscope Shards (32005 / 32006), also top up full Kaleidoscopes (32003 / 32004) to 99
                        if prefix == "GenericItem" and val in (32005, 32006):
                            top_up_kaleidoscopes = True

                    # Server-side fallback for Coin (HaveCoin in PlayerStatus--1)
                    elif prefix == "Coin":
                        if "PlayerStatus--1" not in client_entry_keys:
                            total_coin_to_add += count

                    # Server-side fallback for Unlock-based items (Emblem, ClubStamp, AnotherMusic, SdSkin)
                    elif (
                        prefix in ("Emblem", "ClubStamp", "AnotherMusic", "SdSkin")
                        and isinstance(val, int)
                        and val > 0
                    ):
                        prefix_to_cat = {
                            "Emblem": 8,
                            "ClubStamp": 11,
                            "AnotherMusic": 14,
                            "SdSkin": 16,
                        }
                        cat_id = prefix_to_cat[prefix]
                        if f"UnlockEntityStatusCollection--{cat_id}" not in client_entry_keys:
                            unlocks_by_cat.setdefault(cat_id, []).append(val)

                    item.is_claimed = True
                    item.received_at = now

                if total_stones_to_add > 0:
                    await add_stones(session, current_pid, free_amount=total_stones_to_add)

                for cnt, g_ids in growth_by_count.items():
                    await add_growth_items(session, current_pid, g_ids, count=cnt)

                for (p_id, cnt), item_vals in generic_by_prefix_and_count.items():
                    await add_generic_item_stocks(session, current_pid, p_id, item_vals, count=cnt)

                if top_up_kaleidoscopes:
                    await add_growth_items(
                        session, current_pid, SKILL_ITEM_IDS, count=99, max_cap=99
                    )

                for cat_id, u_ids in unlocks_by_cat.items():
                    u_key = f"UnlockEntityStatusCollection--{cat_id}"
                    u_stmt = select(PlayerStorageEntry).where(
                        PlayerStorageEntry.player_id == current_pid,
                        PlayerStorageEntry.key == u_key,
                    )
                    curr_u = (await session.execute(u_stmt)).scalar_one_or_none()
                    from dankagu.services.player_storage import add_unlock_ids_to_collection

                    curr_val = curr_u.value if curr_u else None
                    new_val = add_unlock_ids_to_collection(curr_val, cat_id, u_ids, now)
                    if curr_u:
                        curr_u.value = new_val
                        curr_u.updated_at = now
                    else:
                        session.add(
                            PlayerStorageEntry(
                                player_id=current_pid,
                                key=u_key,
                                value=new_val,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                    resp_entry = response.entries.add()
                    resp_entry.player_id = current_pid
                    resp_entry.key = u_key
                    resp_entry.value = new_val
                    resp_entry.created_at = curr_u.created_at if curr_u else now
                    resp_entry.updated_at = now

                if total_coin_to_add > 0:
                    ps_res = await session.execute(
                        select(PlayerStorageEntry).where(
                            PlayerStorageEntry.player_id == current_pid,
                            PlayerStorageEntry.key == "PlayerStatus--1",
                        )
                    )
                    ps_entry = ps_res.scalars().first()
                    if ps_entry and ps_entry.value:
                        fields = parse_player_status(ps_entry.value)
                        fields[9] = min(fields.get(9, 0) + total_coin_to_add, 999_999_999)
                        new_val = encode_player_status(fields)
                        ps_entry.value = new_val
                        ps_entry.updated_at = now

                        resp_entry = response.entries.add()
                        resp_entry.player_id = current_pid
                        resp_entry.key = "PlayerStatus--1"
                        resp_entry.value = new_val
                        resp_entry.created_at = ps_entry.created_at
                        resp_entry.updated_at = now

            # 3. Fetch latest wallet and persist revision in the same transaction
            wallet = await get_or_create_wallet(session, current_pid)
            free_stones = wallet.free_stones
            paid_stones = wallet.paid_stones

            revision = await StorageRevisionManager.set_revision_and_save(
                request.next_revision, player_id=current_pid, session=session
            )
            await session.commit()

        # Update wallet in response
        vc_free = response.wallet.virtual_currencies.add()
        vc_free.virtual_currency_name = "FREE_STONE"
        vc_free.amount = free_stones

        vc_paid = response.wallet.virtual_currencies.add()
        vc_paid.virtual_currency_name = "PAID_STONE"
        vc_paid.amount = paid_stones

        response.revision = revision
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
