"""Takasho BoxLootBox servicer for event box gacha (イベントボックスガチャ)."""

import json
import logging
import random
import time
import uuid
from pathlib import Path

import grpc
from google.protobuf.internal.decoder import _DecodeVarint
from google.protobuf.internal.encoder import _EncodeVarint
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.config import settings
from dankagu.core.database import get_sessionmaker
from dankagu.core.economy import get_or_create_wallet
from dankagu.core.session import get_current_player_id
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.errors import fail_internal_on_error
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.consume_resource_set.v1_pb2 import (
    ResourceType,
)
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_inventory.v1_pb2 import (
    ItemType,
    Route,
)
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    box_loot_box_pb2,
    box_loot_box_pb2_grpc,
)
from dankagu.grpc.generated.takasho.schema.fes.resource.box_loot_box import v1_pb2 as box_v1_pb2
from dankagu.models.inventory import PlayerInventoryItem
from dankagu.models.storage import PlayerStorageEntry

logger = logging.getLogger("dankagu.services.box_loot_box")

_BOX_REWARDS_CACHE: dict[str, dict[str, list[dict]]] | None = None
OPEN_BOX_POINT_COST = 100


def get_event_box_rewards() -> dict[str, dict[str, list[dict]]]:
    """Load cached master reward catalog for all event box gachas."""
    global _BOX_REWARDS_CACHE
    if _BOX_REWARDS_CACHE is None:
        p = Path(settings.data_dir) / "metadata" / "event_box_rewards.json"
        if p.exists():
            try:
                _BOX_REWARDS_CACHE = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error("Failed to load event_box_rewards.json: %s", e)
                _BOX_REWARDS_CACHE = {}
        else:
            _BOX_REWARDS_CACHE = {}

        if not _BOX_REWARDS_CACHE:
            from dankagu.services.box_loot_box_data import FALLBACK_EVENT_BOX_REWARDS

            _BOX_REWARDS_CACHE = FALLBACK_EVENT_BOX_REWARDS
    return _BOX_REWARDS_CACHE


def parse_raw_protobuf(data: bytes) -> dict[int, list[tuple[int, int | bytes]]]:
    """Parse raw protobuf into field_number -> list of (wire_type, value)."""
    pos = 0
    fields: dict[int, list[tuple[int, int | bytes]]] = {}
    while pos < len(data):
        tag, pos = _DecodeVarint(data, pos)
        fn = tag >> 3
        wt = tag & 7
        if wt == 0:
            val, pos = _DecodeVarint(data, pos)
            fields.setdefault(fn, []).append((wt, val))
        elif wt == 2:
            length, pos = _DecodeVarint(data, pos)
            val_bytes = data[pos : pos + length]
            pos += length
            fields.setdefault(fn, []).append((wt, val_bytes))
        else:
            break
    return fields


def encode_raw_protobuf(fields: dict[int, list[tuple[int, int | bytes]]]) -> bytes:
    """Encode field_number -> list of (wire_type, value) into protobuf bytes."""
    buf = bytearray()
    for fn in sorted(fields.keys()):
        for wt, val in fields[fn]:
            tag = (fn << 3) | wt
            _EncodeVarint(buf.extend, tag)
            if wt == 0:
                assert isinstance(val, int)
                _EncodeVarint(buf.extend, val)
            elif wt == 2:
                assert isinstance(val, (bytes, bytearray))
                _EncodeVarint(buf.extend, len(val))
                buf.extend(val)
    return bytes(buf)


def parse_event_id_from_product(product_id: str) -> int:
    """Extract event id from product string like 'EventBoxReward_38_38'."""
    parts = product_id.split("_")
    for part in parts[1:]:
        if part.isdigit():
            return int(part)
    return 38


class BoxLootBoxService(box_loot_box_pb2_grpc.BoxLootBoxServicer):
    """Servicer for Danmaku Kagura Event Box Loot Box (イベントボックスガチャ)."""

    async def _get_player_event_status(
        self, session: AsyncSession, player_id: str, event_id: int
    ) -> tuple[dict[int, list[tuple[int, int | bytes]]], int]:
        """Fetch EventInfoStatus fields and current box number for player."""
        key = f"EventInfoStatus-V{event_id}-{event_id}"
        stmt = select(PlayerStorageEntry).where(
            PlayerStorageEntry.player_id == player_id,
            PlayerStorageEntry.key == key,
        )
        res = await session.execute(stmt)
        entry = res.scalar_one_or_none()

        if entry and entry.value:
            fields = parse_raw_protobuf(entry.value)
        else:
            fields = {
                1: [(0, event_id)],
                2: [(0, 0)],
                3: [(0, 0)],
                6: [(2, b"\x01\x01\x01\x01")],
                7: [(0, int(time.time()))],
                11: [(0, int(time.time()))],
                14: [(0, 0)],
                15: [(0, 0)],
            }

        catalog = get_event_box_rewards().get(str(event_id), {})
        total_boxes = len(catalog) if catalog else 13
        prog_list = fields.get(15, [])
        progress = prog_list[0][1] if prog_list and isinstance(prog_list[0][1], int) else 0
        box_no = progress + 1
        if box_no > total_boxes:
            box_no = total_boxes
        box_no = max(1, box_no)

        return fields, box_no

    async def _get_or_init_box_state(
        self, session: AsyncSession, player_id: str, event_id: int, box_no: int
    ) -> tuple[dict[str, int], bool]:
        """Retrieve remaining prize counts and can_reset state for this box."""
        state_key = f"_BoxLootState_{event_id}_{box_no}"
        stmt = select(PlayerStorageEntry).where(
            PlayerStorageEntry.player_id == player_id,
            PlayerStorageEntry.key == state_key,
        )
        res = await session.execute(stmt)
        entry = res.scalar_one_or_none()

        catalog = get_event_box_rewards().get(str(event_id), {})
        box_entries = catalog.get(str(box_no), [])

        if entry and entry.value:
            try:
                data = json.loads(entry.value.decode("utf-8"))
                remain_counts: dict[str, int] = data.get("remain_counts", {})
                can_reset: bool = bool(data.get("can_reset", False))
                # Ensure all box entry IDs are present
                for e in box_entries:
                    sid = str(e["id"])
                    if sid not in remain_counts:
                        remain_counts[sid] = int(e["reward_count"])
                return remain_counts, can_reset
            except Exception as ex:
                logger.warning("Failed parsing box state %s: %s", state_key, ex)

        # Fresh box stock initialization
        remain_counts = {str(e["id"]): int(e["reward_count"]) for e in box_entries}
        can_reset = False
        now = int(time.time())
        save_val = json.dumps({"remain_counts": remain_counts, "can_reset": can_reset}).encode(
            "utf-8"
        )
        upsert_stmt = pg_insert(PlayerStorageEntry).values(
            player_id=player_id,
            key=state_key,
            value=save_val,
            created_at=now,
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
        return remain_counts, can_reset

    async def _save_box_state(
        self,
        session: AsyncSession,
        player_id: str,
        event_id: int,
        box_no: int,
        remain_counts: dict[str, int],
        can_reset: bool,
    ) -> None:
        """Persist remaining prize counts and can_reset flag."""
        state_key = f"_BoxLootState_{event_id}_{box_no}"
        now = int(time.time())
        save_val = json.dumps({"remain_counts": remain_counts, "can_reset": can_reset}).encode(
            "utf-8"
        )
        upsert_stmt = pg_insert(PlayerStorageEntry).values(
            player_id=player_id,
            key=state_key,
            value=save_val,
            created_at=now,
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

    def _build_box_proto(
        self,
        event_id: int,
        box_no: int,
        box_entries: list[dict],
        remain_counts: dict[str, int],
        total_boxes: int,
    ) -> box_v1_pb2.BoxLootBoxBox:
        """Construct BoxLootBoxBox protobuf message matching client model."""
        box = box_v1_pb2.BoxLootBoxBox()
        box.box_loot_box_box_num = box_no
        box.inventory_message = "イベント報酬"
        box.is_repeat = box_no >= total_boxes
        box.has_extra = False

        total_cnt = sum(int(e["reward_count"]) for e in box_entries)
        remain_cnt = sum(remain_counts.get(str(e["id"]), 0) for e in box_entries)
        box.total_content_num = total_cnt
        box.remain_content_num = remain_cnt

        pickup_entry = None
        for entry in box_entries:
            c = box.box_loot_box_contents.add()
            cid = f"EventBoxReward_{event_id}_{box_no}_{entry['id']}"
            c.box_loot_box_content_id = cid
            c.item_type = (
                ItemType.VIRTUAL_CURRENCY if entry["item_type"] == 1 else ItemType.GAME_RUNTIME
            )
            c.schema_key = entry["schema_key"]
            c.amount = int(entry["count"])
            c.resource_name = f"{entry['prefix_name']}_{entry['val']}"
            c.resource_num = int(entry["count"])
            c.remain_amount = remain_counts.get(str(entry["id"]), 0)
            c.search_label = entry["search_label"]
            c.value = json.dumps(
                {
                    "Prefix": entry["prefix_name"],
                    "Value": entry["val"],
                    "Count": entry["count"],
                    "ContentId": cid,
                    "Reset": entry["reset"],
                    "TableID": entry["id"],
                }
            ).encode("utf-8")

            if entry["reset"] and pickup_entry is None:
                pickup_entry = (c, entry)

        if pickup_entry:
            box.has_pickup = True
            box.pickup.box_loot_box_pickup_id = f"P_{event_id}_{box_no}"
            box.pickup.condition_operator = box_v1_pb2.BoxLootBoxPickup.ConditionOperator.And
            pu_content = box.pickup.pickup_contents.add()
            pu_content.CopyFrom(pickup_entry[0])
        else:
            box.has_pickup = False

        # Consume resource set (100 event points per pull)
        crs = box.consume_resource_sets.add()
        crs.consume_resource_set_id = f"CRS_{event_id}_{box_no}"
        crs.resource_type = ResourceType.PLAYER_KEY_VALUE
        crs.amount = OPEN_BOX_POINT_COST
        cr = crs.consume_resources.add()
        cr.consume_resource_id = f"CR_{event_id}_{box_no}"
        cr.resource_key = f"EventInfoStatus-V{event_id}-{event_id}"
        cr.priority = 1

        return box

    async def _build_product_proto(
        self, session: AsyncSession, player_id: str, event_id: int
    ) -> box_v1_pb2.BoxLootBoxProduct:
        """Construct full BoxLootBoxProduct for an event."""
        _fields, current_box_no = await self._get_player_event_status(session, player_id, event_id)
        catalog = get_event_box_rewards().get(str(event_id), {})
        total_boxes = len(catalog) if catalog else 13
        box_entries = catalog.get(str(current_box_no), [])

        remain_counts, _can_reset = await self._get_or_init_box_state(
            session, player_id, event_id, current_box_no
        )

        product = box_v1_pb2.BoxLootBoxProduct()
        product.box_loot_box_product_id = f"EventBoxReward_{event_id}_{event_id}"
        product.opened_at = 1590000000
        product.closed_at = 4102498799
        product.box_num = current_box_no
        product.total_box_num = total_boxes

        box = self._build_box_proto(
            event_id, current_box_no, box_entries, remain_counts, total_boxes
        )
        pbox = product.box_loot_box_boxes.add()
        pbox.CopyFrom(box)

        return product

    async def GetAvailableV1(
        self,
        request: box_loot_box_pb2.BoxLootBoxV1GetAvailableV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> box_loot_box_pb2.BoxLootBoxV1GetAvailableV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        logger.info("📦 [BoxLootBox] GetAvailableV1 (player=%s)", current_pid)
        response = box_loot_box_pb2.BoxLootBoxV1GetAvailableV1.Response()

        catalog = get_event_box_rewards()
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # Active event 38 by default or events in catalog
            for ev_id_str in ["38"]:
                if ev_id_str in catalog:
                    prod = await self._build_product_proto(session, current_pid, int(ev_id_str))
                    p = response.box_loot_box_products.add()
                    p.CopyFrom(prod)

        return response

    async def GetDetailV1(
        self,
        request: box_loot_box_pb2.BoxLootBoxV1GetDetailV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> box_loot_box_pb2.BoxLootBoxV1GetDetailV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        logger.info(
            "📦 [BoxLootBox] GetDetailV1 (player=%s, products=%s)",
            current_pid,
            list(request.box_loot_box_product_ids),
        )
        response = box_loot_box_pb2.BoxLootBoxV1GetDetailV1.Response()

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            for pid in request.box_loot_box_product_ids:
                ev_id = parse_event_id_from_product(pid)
                prod = await self._build_product_proto(session, current_pid, ev_id)
                p = response.box_loot_box_products.add()
                p.CopyFrom(prod)

        return response

    @fail_internal_on_error("Failed to complete box loot box purchase")
    async def PurchaseV1(
        self,
        request: box_loot_box_pb2.BoxLootBoxV1PurchaseV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> box_loot_box_pb2.BoxLootBoxV1PurchaseV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        ev_id = parse_event_id_from_product(request.box_loot_box_product_id)
        requested_num = max(1, request.num)

        logger.info(
            "📦 [BoxLootBox] PurchaseV1 (player=%s, product=%s, num=%d)",
            current_pid,
            request.box_loot_box_product_id,
            requested_num,
        )

        response = box_loot_box_pb2.BoxLootBoxV1PurchaseV1.Response()
        response.transaction_id = request.transaction_id

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # 1. Fetch player event status & points
            fields, box_no = await self._get_player_event_status(session, current_pid, ev_id)
            catalog = get_event_box_rewards().get(str(ev_id), {})
            box_entries = catalog.get(str(box_no), [])

            # Check point balance
            pt_list = fields.get(3, [])
            current_points = pt_list[0][1] if pt_list and isinstance(pt_list[0][1], int) else 0

            # 2. Fetch box stock
            remain_counts, can_reset = await self._get_or_init_box_state(
                session, current_pid, ev_id, box_no
            )

            # Available stock
            available_entries = [e for e in box_entries if remain_counts.get(str(e["id"]), 0) > 0]
            total_remaining = sum(remain_counts.get(str(e["id"]), 0) for e in available_entries)

            if total_remaining == 0:
                can_reset = True
                response.remain_num = 0
                response.can_reset = True
                return response

            affordable_draws = current_points // OPEN_BOX_POINT_COST
            actual_num = min(requested_num, affordable_draws, total_remaining)

            if actual_num <= 0:
                logger.warning(
                    "📦 [BoxLootBox] Player %s has insufficient points (%d) for draw",
                    current_pid,
                    current_points,
                )
                actual_num = min(requested_num, total_remaining)  # fallback to allow draw

            # 3. Perform weighted lottery from remaining stock
            now = int(time.time())
            drawn_items: list[dict] = []

            for _ in range(actual_num):
                pool = [e for e in box_entries if remain_counts.get(str(e["id"]), 0) > 0]
                if not pool:
                    break
                weights = [int(e["weight"]) * remain_counts[str(e["id"])] for e in pool]
                total_weight = sum(weights)
                if total_weight <= 0:
                    chosen = random.choice(pool)
                else:
                    chosen = random.choices(pool, weights=weights, k=1)[0]

                sid = str(chosen["id"])
                remain_counts[sid] -= 1
                if chosen["reset"]:
                    can_reset = True
                drawn_items.append(chosen)

            # Check if all items exhausted
            total_remaining_after = sum(remain_counts.get(str(e["id"]), 0) for e in box_entries)
            if total_remaining_after == 0:
                can_reset = True

            # 4. Deduct event points
            cost = actual_num * OPEN_BOX_POINT_COST
            new_points = max(0, current_points - cost)
            fields[3] = [(0, new_points)]
            status_key = f"EventInfoStatus-V{ev_id}-{ev_id}"
            encoded_val = encode_raw_protobuf(fields)

            upsert_status = pg_insert(PlayerStorageEntry).values(
                player_id=current_pid,
                key=status_key,
                value=encoded_val,
                created_at=now,
                updated_at=now,
            )
            upsert_status = upsert_status.on_conflict_do_update(
                index_elements=[PlayerStorageEntry.player_id, PlayerStorageEntry.key],
                set_={
                    "value": upsert_status.excluded.value,
                    "updated_at": upsert_status.excluded.updated_at,
                },
            )
            await session.execute(upsert_status)

            # 5. Persist box state
            await self._save_box_state(
                session, current_pid, ev_id, box_no, remain_counts, can_reset
            )

            # 6. Deliver rewards to player inventory (Mailbox / claimable)
            for entry in drawn_items:
                cid = f"EventBoxReward_{ev_id}_{box_no}_{entry['id']}"

                # Content proto in response
                c = response.box_loot_box_contents.add()
                c.box_loot_box_content_id = cid
                c.item_type = (
                    ItemType.VIRTUAL_CURRENCY if entry["item_type"] == 1 else ItemType.GAME_RUNTIME
                )
                c.schema_key = entry["schema_key"]
                c.amount = int(entry["count"])
                c.resource_name = f"{entry['prefix_name']}_{entry['val']}"
                c.resource_num = int(entry["count"])
                c.remain_amount = remain_counts.get(str(entry["id"]), 0)
                c.search_label = entry["search_label"]
                c.value = json.dumps(
                    {
                        "Prefix": entry["prefix_name"],
                        "Value": entry["val"],
                        "Count": entry["count"],
                        "ContentId": cid,
                        "Reset": entry["reset"],
                        "TableID": entry["id"],
                    }
                ).encode("utf-8")

                # Inventory item
                inv_val = json.dumps(
                    {"Prefix": entry["prefix_name"], "Value": entry["val"], "Count": entry["count"]}
                ).encode("utf-8")
                mail_item = PlayerInventoryItem(
                    id=str(uuid.uuid4()),
                    player_id=current_pid,
                    item_type=int(c.item_type),
                    schema_key=entry["schema_key"],
                    value=inv_val,
                    route=int(Route.BOX_LOOT_BOX),
                    message="イベントボックスガチャ報酬",
                    search_label=entry["search_label"],
                    opened_at=now,
                    expired_at=now + 86400 * 90,
                    system_resource_name=f"{entry['prefix_name']}_{entry['val']}",
                    system_resource_num=int(entry["count"]),
                    is_claimed=False,
                    created_at=now,
                )
                session.add(mail_item)

                # Add to response player_inventories
                p_inv = response.player_inventories.add()
                p_inv.id = mail_item.id
                p_inv.player_id = current_pid
                p_inv.item_type = mail_item.item_type
                p_inv.schema_key = mail_item.schema_key
                p_inv.value = mail_item.value
                p_inv.route = mail_item.route
                p_inv.message = mail_item.message
                p_inv.search_label = mail_item.search_label
                p_inv.opened_at = mail_item.opened_at
                p_inv.expired_at = mail_item.expired_at
                p_inv.system_resource_name = mail_item.system_resource_name
                p_inv.system_resource_num = mail_item.system_resource_num
                p_inv.created_at = mail_item.created_at

            # 7. Wallet info
            wallet_row = await get_or_create_wallet(session, current_pid)
            vc_free = response.wallet.virtual_currencies.add()
            vc_free.virtual_currency_name = "FREE_STONE"
            vc_free.amount = wallet_row.free_stones

            vc_paid = response.wallet.virtual_currencies.add()
            vc_paid.virtual_currency_name = "PAID_STONE"
            vc_paid.amount = wallet_row.paid_stones

            pkv = response.wallet.player_key_values.add()
            pkv.key = f"EventInfoStatus-V{ev_id}-{ev_id}"
            pkv.amount = new_points

            response.remain_num = total_remaining_after
            response.can_reset = can_reset

            await session.commit()

        return response

    @fail_internal_on_error("Failed to reset box loot box")
    async def ResetV1(
        self,
        request: box_loot_box_pb2.BoxLootBoxV1ResetV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> box_loot_box_pb2.BoxLootBoxV1ResetV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        ev_id = parse_event_id_from_product(request.box_loot_box_product_id)

        logger.info(
            "📦 [BoxLootBox] ResetV1 (player=%s, product=%s)",
            current_pid,
            request.box_loot_box_product_id,
        )

        response = box_loot_box_pb2.BoxLootBoxV1ResetV1.Response()

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            fields, box_no = await self._get_player_event_status(session, current_pid, ev_id)
            catalog = get_event_box_rewards().get(str(ev_id), {})
            total_boxes = len(catalog) if catalog else 13

            # Advance to next box (or repeat if loop box)
            if box_no < total_boxes:
                new_box_no = box_no + 1
            else:
                new_box_no = total_boxes

            # Update progress in EventInfoStatus (field 15 = new_box_no - 1)
            fields[15] = [(0, new_box_no - 1)]
            now = int(time.time())
            fields[7] = [(0, now)]
            status_key = f"EventInfoStatus-V{ev_id}-{ev_id}"
            encoded_val = encode_raw_protobuf(fields)

            upsert_status = pg_insert(PlayerStorageEntry).values(
                player_id=current_pid,
                key=status_key,
                value=encoded_val,
                created_at=now,
                updated_at=now,
            )
            upsert_status = upsert_status.on_conflict_do_update(
                index_elements=[PlayerStorageEntry.player_id, PlayerStorageEntry.key],
                set_={
                    "value": upsert_status.excluded.value,
                    "updated_at": upsert_status.excluded.updated_at,
                },
            )
            await session.execute(upsert_status)

            # Reinitialize new box state
            new_box_entries = catalog.get(str(new_box_no), [])
            new_remain_counts = {str(e["id"]): int(e["reward_count"]) for e in new_box_entries}
            await self._save_box_state(
                session, current_pid, ev_id, new_box_no, new_remain_counts, False
            )

            await session.commit()

            box_proto = self._build_box_proto(
                ev_id, new_box_no, new_box_entries, new_remain_counts, total_boxes
            )
            response.box_loot_box.CopyFrom(box_proto)

        return response


def register_box_loot_box_servicer(server: grpc.aio.Server) -> None:
    """Register BoxLootBox gRPC handlers onto server."""
    servicer = BoxLootBoxService()
    method_handlers = {
        "GetAvailableV1": takasho_unary_handler(
            servicer.GetAvailableV1,
            box_loot_box_pb2.BoxLootBoxV1GetAvailableV1.Request,
            box_loot_box_pb2.BoxLootBoxV1GetAvailableV1.Response,
        ),
        "GetDetailV1": takasho_unary_handler(
            servicer.GetDetailV1,
            box_loot_box_pb2.BoxLootBoxV1GetDetailV1.Request,
            box_loot_box_pb2.BoxLootBoxV1GetDetailV1.Response,
        ),
        "PurchaseV1": takasho_unary_handler(
            servicer.PurchaseV1,
            box_loot_box_pb2.BoxLootBoxV1PurchaseV1.Request,
            box_loot_box_pb2.BoxLootBoxV1PurchaseV1.Response,
        ),
        "ResetV1": takasho_unary_handler(
            servicer.ResetV1,
            box_loot_box_pb2.BoxLootBoxV1ResetV1.Request,
            box_loot_box_pb2.BoxLootBoxV1ResetV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.fes.player_api.BoxLootBox", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
    logger.info(
        "📦 [BoxLootBox] Servicer registered under takasho.schema.fes.player_api.BoxLootBox"
    )
