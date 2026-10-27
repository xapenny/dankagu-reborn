"""Tests for heroine and character milestone emblem grants and UnlockEntityStatusCollection--8 fallback."""

import json
import time
import uuid

import pytest
from player_factory import ensure_player
from sqlalchemy import select

from dankagu.core.database import get_sessionmaker
from dankagu.core.session import set_current_player
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import player_storage_pb2
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_storage.v2_pb2 import (
    Criterion,
    Entry,
)
from dankagu.models.inventory import PlayerInventoryItem
from dankagu.models.storage import PlayerStorageEntry
from dankagu.services.box_loot_box import encode_raw_protobuf, parse_raw_protobuf
from dankagu.services.player_storage import (
    PlayerStorageService,
    add_emblems_to_collection,
    check_and_grant_heroine_milestones,
)


class MockContext:
    def __init__(self) -> None:
        self.aborted = False

    async def abort(self, code: object, details: str = "") -> None:
        self.aborted = True
        import grpc

        raise grpc.RpcError(f"Aborted: {code} - {details}")


@pytest.mark.asyncio
async def test_add_emblems_to_collection() -> None:
    now = 1792937000

    # 1. Start from None
    blob1 = add_emblems_to_collection(None, [1000001], now)
    assert len(blob1) > 0
    p1 = parse_raw_protobuf(blob1)
    assert p1[1][0][1] == 8
    assert len(p1[2]) == 1

    # 2. Add another emblem
    blob2 = add_emblems_to_collection(blob1, [1100001], now)
    p2 = parse_raw_protobuf(blob2)
    assert len(p2[2]) == 2

    # 3. Add duplicate (should not grow)
    blob3 = add_emblems_to_collection(blob2, [1000001], now)
    assert len(blob3) == len(blob2)


@pytest.mark.asyncio
async def test_unlock_collection_8_fallback() -> None:
    service = PlayerStorageService()
    ctx = MockContext()
    pid = f"test-player-emblem-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid)
    set_current_player(pid)

    req = player_storage_pb2.PlayerStorageGetEntriesV2.Request(
        criteria=[
            Criterion(
                key="UnlockEntityStatusCollection--", matching_type=Criterion.MatchingType.FORWARD
            )
        ]
    )
    res = await service.GetEntriesV2(req, ctx)  # type: ignore[arg-type]
    keys = {e.key: e.value for e in res.entries}

    assert "UnlockEntityStatusCollection--8" in keys
    assert keys["UnlockEntityStatusCollection--8"] == b"\x08\x08"


@pytest.mark.asyncio
async def test_heroine_milestone_auto_grant() -> None:
    pid = f"test-player-grant-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid)
    now = int(time.time())

    # Create dummy HeroineStatus with chara_id=1 (Reimu) and exp=50 (Lv 51)
    heroine_raw = encode_raw_protobuf(
        {
            1: [(0, 918819949263454210)],
            2: [(0, 1)],  # chara_id = 1
            8: [(0, 50)],  # exp = 50 >= 49 (Lv 50 reached)
        }
    )

    entry = Entry(key="HeroineStatus--918819949263454210", value=heroine_raw)

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        u8_entry = await check_and_grant_heroine_milestones(session, pid, [entry], now)
        await session.commit()

        # Check that NO PlayerInventoryItem was created (titles have MaxCount=1 and are unlocked directly)
        res = await session.execute(
            select(PlayerInventoryItem).where(
                PlayerInventoryItem.player_id == pid,
                PlayerInventoryItem.system_resource_name == "Emblem_1000001",
            )
        )
        assert res.scalar_one_or_none() is None

        # Check that UnlockEntityStatusCollection--8 in storage was updated
        res_u8 = await session.execute(
            select(PlayerStorageEntry).where(
                PlayerStorageEntry.player_id == pid,
                PlayerStorageEntry.key == "UnlockEntityStatusCollection--8",
            )
        )
        u8 = res_u8.scalar_one_or_none()
        assert u8 is not None
        p_u8 = parse_raw_protobuf(u8.value)
        assert p_u8[1][0][1] == 8
        assert len(p_u8[2]) == 1
        assert u8_entry is not None
        assert u8_entry.key == "UnlockEntityStatusCollection--8"


@pytest.mark.asyncio
async def test_all_unlock_and_episode_collections_fallback() -> None:
    service = PlayerStorageService()
    ctx = MockContext()
    pid = f"test-player-colls-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid)
    set_current_player(pid)

    # 1. Test all 16 Unlock categories
    req_u = player_storage_pb2.PlayerStorageGetEntriesV2.Request(
        criteria=[
            Criterion(
                key="UnlockEntityStatusCollection--", matching_type=Criterion.MatchingType.FORWARD
            )
        ]
    )
    res_u = await service.GetEntriesV2(req_u, ctx)  # type: ignore[arg-type]
    keys_u = {e.key for e in res_u.entries}
    for cat_id in range(1, 17):
        assert f"UnlockEntityStatusCollection--{cat_id}" in keys_u

    # 2. Test Episode categories
    req_ep = player_storage_pb2.PlayerStorageGetEntriesV2.Request(
        criteria=[
            Criterion(
                key="EpisodeEntityStatusCollection--", matching_type=Criterion.MatchingType.FORWARD
            )
        ]
    )
    res_ep = await service.GetEntriesV2(req_ep, ctx)  # type: ignore[arg-type]
    keys_ep = {e.key for e in res_ep.entries}
    for ep_cat in (1, 2, 3, 4, 5, 6, 10):
        assert f"EpisodeEntityStatusCollection--{ep_cat}" in keys_ep


@pytest.mark.asyncio
async def test_receive_v1_unlock_fallback() -> None:
    from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
        player_inventory_pb2,
    )
    from dankagu.services.player_inventory import PlayerInventoryService

    inv_service = PlayerInventoryService()
    ctx = MockContext()
    pid = f"test-player-recv-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid)
    set_current_player(pid)
    now = int(time.time())

    # Create gift box mail with SdSkin (prefix="SdSkin", value=36001)
    gift_id = str(uuid.uuid4())
    mail_item = PlayerInventoryItem(
        id=gift_id,
        player_id=pid,
        item_type=0,
        schema_key="ITEM",
        value=json.dumps({"Prefix": "SdSkin", "Value": 36001, "Count": 1}).encode("utf-8"),
        route=2,
        message="3Dスキンプレゼント",
        search_label="Item",
        opened_at=now,
        expired_at=now + 86400 * 90,
        system_resource_name="SdSkin_36001",
        system_resource_num=1,
        is_claimed=False,
        created_at=now,
    )

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        session.add(mail_item)
        await session.commit()

    # Client claims gift without providing UnlockEntityStatusCollection--16 in entries
    req = player_inventory_pb2.PlayerInventoryReceiveV1.Request(
        delete_inventory_ids=[gift_id],
        next_revision="rev_test_1",
    )
    res = await inv_service.ReceiveV1(req, ctx)  # type: ignore[arg-type]

    # Verify server created UnlockEntityStatusCollection--16 and echoed in response.entries
    entry_keys = {e.key: e.value for e in res.entries}
    assert "UnlockEntityStatusCollection--16" in entry_keys
    parsed = parse_raw_protobuf(entry_keys["UnlockEntityStatusCollection--16"])
    assert parsed[1][0][1] == 16  # category = 16
    assert len(parsed[2]) == 1  # 1 submessage for skin 36001
