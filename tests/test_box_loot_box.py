"""Tests for BoxLootBox gRPC service (Event Box Gacha)."""

import time
import uuid

import pytest
from player_factory import ensure_player
from sqlalchemy.dialects.postgresql import insert as pg_insert

from dankagu.core.database import get_sessionmaker
from dankagu.core.session import set_current_player
from dankagu.grpc.generated.takasho.schema.fes.player_api import box_loot_box_pb2
from dankagu.models.storage import PlayerStorageEntry
from dankagu.services.box_loot_box import (
    BoxLootBoxService,
    encode_raw_protobuf,
)


class MockContext:
    def __init__(self) -> None:
        self.aborted = False

    async def abort(self, code: object, details: str = "") -> None:
        self.aborted = True
        import grpc

        raise grpc.RpcError(f"Aborted: {code} - {details}")


@pytest.mark.asyncio
async def test_box_loot_box_lifecycle() -> None:
    service = BoxLootBoxService()
    ctx = MockContext()
    pid = f"test-player-box-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid)
    set_current_player(pid)

    # 1. Seed active event 38 in PlayerStorage with 1000 resource points (enough for 10 pulls)
    ev_fields = {
        1: [(0, 38)],
        2: [(0, 1000)],
        3: [(0, 1000)],  # 1000 spendable event points
        15: [(0, 0)],    # box 1 (0-indexed progress)
    }
    encoded_status = encode_raw_protobuf(ev_fields)
    now = int(time.time())

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        stmt = pg_insert(PlayerStorageEntry).values(
            player_id=pid,
            key="EventInfoStatus-V38-38",
            value=encoded_status,
            created_at=now,
            updated_at=now,
        )
        await session.execute(stmt)
        await session.commit()

    # 2. GetAvailableV1
    req_avail = box_loot_box_pb2.BoxLootBoxV1GetAvailableV1.Request()
    res_avail = await service.GetAvailableV1(req_avail, ctx)  # type: ignore[arg-type]
    assert len(res_avail.box_loot_box_products) >= 1
    prod = res_avail.box_loot_box_products[0]
    assert prod.box_loot_box_product_id == "EventBoxReward_38_38"
    assert len(prod.box_loot_box_boxes) == 1
    box = prod.box_loot_box_boxes[0]
    assert box.box_loot_box_box_num == 1
    assert len(box.box_loot_box_contents) > 0

    # 3. GetDetailV1
    req_detail = box_loot_box_pb2.BoxLootBoxV1GetDetailV1.Request(
        box_loot_box_product_ids=["EventBoxReward_38_38"]
    )
    res_detail = await service.GetDetailV1(req_detail, ctx)  # type: ignore[arg-type]
    assert len(res_detail.box_loot_box_products) == 1
    prod_detail = res_detail.box_loot_box_products[0]
    assert prod_detail.box_loot_box_product_id == "EventBoxReward_38_38"
    assert len(prod_detail.box_loot_box_boxes) == 1
    assert prod_detail.box_loot_box_boxes[0].box_loot_box_box_num == 1

    # 4. PurchaseV1 (draw 1 item, costs 100 points)
    req_purchase = box_loot_box_pb2.BoxLootBoxV1PurchaseV1.Request(
        box_loot_box_product_id="EventBoxReward_38_38",
        num=1,
    )
    res_purchase = await service.PurchaseV1(req_purchase, ctx)  # type: ignore[arg-type]
    assert len(res_purchase.box_loot_box_contents) == 1
    assert len(res_purchase.player_inventories) == 1
    assert res_purchase.player_inventories[0].player_id == pid
    assert res_purchase.remain_num >= 0

    # Verify points deducted in storage
    async with session_factory() as session:
        entry = await session.get(PlayerStorageEntry, (pid, "EventInfoStatus-V38-38"))
        assert entry is not None
        # Parse fields
        from dankagu.services.box_loot_box import parse_raw_protobuf
        updated_fields = parse_raw_protobuf(entry.value)
        # 1000 - 100 = 900
        assert updated_fields[3][0][1] == 900

    # 5. ResetV1 (Advance to box 2)
    req_reset = box_loot_box_pb2.BoxLootBoxV1ResetV1.Request(
        box_loot_box_product_id="EventBoxReward_38_38"
    )
    res_reset = await service.ResetV1(req_reset, ctx)  # type: ignore[arg-type]
    assert res_reset.box_loot_box.box_loot_box_box_num == 2
    assert len(res_reset.box_loot_box.box_loot_box_contents) > 0
