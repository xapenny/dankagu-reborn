"""Tests for event ranking emblem dynamic rank number preservation and display."""

import json
import time
import uuid

import pytest
from player_factory import ensure_player
from sqlalchemy import select

from dankagu.core.database import get_sessionmaker
from dankagu.core.ranking_rewards import (
    check_and_grant_event_ranking_milestones,
    resolve_event_emblem_rank,
)
from dankagu.core.session import reset_current_player, set_current_player
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_inventory_pb2,
    player_storage_pb2,
)
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_storage.v2_pb2 import (
    Criterion,
)
from dankagu.models.inventory import PlayerInventoryItem
from dankagu.models.ranking import EventLeaderboard
from dankagu.models.storage import PlayerStorageEntry
from dankagu.services.box_loot_box import encode_raw_protobuf, parse_raw_protobuf
from dankagu.services.player_inventory import (
    PlayerInventoryService,
    heal_legacy_ranking_reward_items,
)
from dankagu.services.player_storage import (
    PlayerStorageService,
    add_unlock_ids_to_collection,
    encode_event_emblem_rankings,
    parse_event_emblem_rankings,
    update_player_base_emblem_intvals,
)


class MockContext:
    """Mock gRPC context."""

    def __init__(self) -> None:
        self.code = None
        self.details = None

    async def abort(self, code, details):
        self.code = code
        self.details = details
        raise RuntimeError(f"RPC Aborted: {code} - {details}")


def test_event_emblem_ranking_proto_codec() -> None:
    """Verify encoding and decoding of EventEmblemRankingStatusCollection--1."""
    rankings = {
        3003804: 4,
        3003805: 15,
        3003806: 120,
    }
    encoded = encode_event_emblem_rankings(rankings, coll_id=1)
    assert encoded is not None

    fields = parse_raw_protobuf(encoded)
    assert fields[1][0][1] == 1  # ID = 1
    assert len(fields[2]) == 3  # 3 sub-messages

    decoded = parse_event_emblem_rankings(encoded)
    assert decoded == rankings


def test_resolve_event_emblem_rank() -> None:
    """Verify rank resolution for event emblems."""
    # Top 10 tier (3003804: 4..10)
    assert resolve_event_emblem_rank(3003804, actual_rank=4) == 4
    assert resolve_event_emblem_rank(3003804, actual_rank=7) == 7
    assert resolve_event_emblem_rank(3003804, actual_rank=1) == 4  # Clamped to tier min
    assert resolve_event_emblem_rank(3003804, actual_rank=0) == 4  # Fallback to rank_from

    # Top 100 tier (3003805: 11..100)
    assert resolve_event_emblem_rank(3003805, actual_rank=15) == 15
    assert resolve_event_emblem_rank(3003805, actual_rank=4) == 11  # Clamped to tier min
    assert resolve_event_emblem_rank(3003805, actual_rank=0) == 11

    # Generic event emblem fallback
    assert resolve_event_emblem_rank(3003204, actual_rank=5) == 5
    assert resolve_event_emblem_rank(3003204, actual_rank=0) == 4


def test_update_player_base_emblem_intvals() -> None:
    """Verify that PlayerBase--1 Emblem1IntVal (Tag 18) and Emblem2IntVal (Tag 19) are updated."""
    # Construct a PlayerBaseProto: ID=1, Emblem1ID=3003804 (Tag 5), Emblem2ID=3003805 (Tag 6)
    base_blob = encode_raw_protobuf(
        {
            1: [(0, 1)],
            5: [(0, 3003804)],
            6: [(0, 3003805)],
        }
    )

    rankings = {
        3003804: 4,
        3003805: 15,
    }

    new_blob, changed = update_player_base_emblem_intvals(base_blob, rankings)
    assert changed is True
    assert new_blob is not None

    fields = parse_raw_protobuf(new_blob)
    assert fields[5][0][1] == 3003804
    assert fields[6][0][1] == 3003805
    assert fields[18][0][1] == 4  # Emblem1IntVal
    assert fields[19][0][1] == 15  # Emblem2IntVal

    # Calling again with identical values should return changed = False
    _, changed_again = update_player_base_emblem_intvals(new_blob, rankings)
    assert changed_again is False


@pytest.mark.asyncio
async def test_grant_ranking_milestone_mailbox_event_rank_no() -> None:
    """When event ranking milestones are granted, mailbox items have EventRankNo set."""
    pid = f"test-emblem-mail-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid, nickname="EmblemMailUser")
    session_factory = get_sessionmaker()

    # Player reaches Rank 4 in score38
    async with session_factory() as session:
        granted = await check_and_grant_event_ranking_milestones(
            session, pid, "score38", current_rank=4
        )
        assert granted > 0
        await session.commit()

        # Check mailbox items
        stmt = select(PlayerInventoryItem).where(
            PlayerInventoryItem.player_id == pid,
            PlayerInventoryItem.system_resource_name.like("rank_rew:score38:%"),
        )
        items = (await session.execute(stmt)).scalars().all()
        assert len(items) > 0

        # Tier 4 grants Emblem 3003804
        tier4_item = next((it for it in items if "tier_4" in (it.system_resource_name or "")), None)
        assert tier4_item is not None
        val_data = json.loads(tier4_item.value.decode("utf-8"))
        assert val_data["Prefix"] == "Emblem"
        assert val_data["Value"] == 3003804
        assert val_data["EventRankNo"] == 4
        assert tier4_item.schema_key == "EVENT_EMBLEM_DIRECT"

        # Check EventEmblemRankingStatusCollection--1 in storage
        st_stmt = select(PlayerStorageEntry).where(
            PlayerStorageEntry.player_id == pid,
            PlayerStorageEntry.key == "EventEmblemRankingStatusCollection--1",
        )
        coll_entry = (await session.execute(st_stmt)).scalar_one_or_none()
        assert coll_entry is not None
        rankings = parse_event_emblem_rankings(coll_entry.value)
        assert rankings.get(3003804) == 4


@pytest.mark.asyncio
async def test_claim_emblem_mailbox_stores_ranking_and_returns_entry() -> None:
    """Claiming an emblem from mailbox in ReceiveV1 stores EventEmblemRankingStatusCollection--1."""
    pid = f"test-emblem-claim-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid, nickname="EmblemClaimUser")
    tokens = set_current_player(pid)
    ctx = MockContext()
    now = int(time.time())

    session_factory = get_sessionmaker()
    inv_id = str(uuid.uuid4())
    async with session_factory() as session:
        # Create an emblem gift in mailbox with EventRankNo=12
        inv_item = PlayerInventoryItem(
            id=inv_id,
            player_id=pid,
            item_type=0,
            schema_key="EVENT_EMBLEM_DIRECT",
            value=json.dumps(
                {
                    "Prefix": "Emblem",
                    "Value": 3003805,
                    "Count": 1,
                    "EventRankNo": 12,
                }
            ).encode("utf-8"),
            route=4,
            message="【イベントランキング】100位以内達成報酬！",
            search_label="Other",
            opened_at=now,
            expired_at=now + 86400 * 90,
            system_resource_name="rank_rew:score38:tier_5#Emblem_3003805",
            system_resource_num=1,
            is_claimed=False,
            created_at=now,
        )
        session.add(inv_item)
        await session.commit()

    inv_svc = PlayerInventoryService()
    req = player_inventory_pb2.PlayerInventoryReceiveV1.Request(
        delete_inventory_ids=[inv_id],
        next_revision="rev-claim-1",
    )
    res = await inv_svc.ReceiveV1(req, ctx)  # type: ignore[arg-type]

    # Check response entries has EventEmblemRankingStatusCollection--1
    coll_resp = next(
        (e for e in res.entries if e.key == "EventEmblemRankingStatusCollection--1"), None
    )
    assert coll_resp is not None
    rankings = parse_event_emblem_rankings(coll_resp.value)
    assert rankings.get(3003805) == 12

    # Check database persistence
    async with session_factory() as session:
        st_stmt = select(PlayerStorageEntry).where(
            PlayerStorageEntry.player_id == pid,
            PlayerStorageEntry.key == "EventEmblemRankingStatusCollection--1",
        )
        entry = (await session.execute(st_stmt)).scalar_one_or_none()
        assert entry is not None
        db_rankings = parse_event_emblem_rankings(entry.value)
        assert db_rankings.get(3003805) == 12

    reset_current_player(tokens)


@pytest.mark.asyncio
async def test_heal_legacy_unlocked_emblems_and_player_base() -> None:
    """heal_legacy_ranking_reward_items heals unlocked event emblems and equips ranking in PlayerBase."""
    pid = f"test-emblem-heal-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid, nickname="EmblemHealUser")
    session_factory = get_sessionmaker()
    now = int(time.time())

    # 1. 3 other players have higher scores, so this player is Rank 4
    for i, s in enumerate([3000000, 2000000, 1500000], start=1):
        other_pid = f"other-{i}-{uuid.uuid4().hex[:6]}"
        await ensure_player(other_pid, nickname=f"Other{i}")
        async with session_factory() as session:
            session.add(
                EventLeaderboard(
                    ranking_event_id="score38",
                    player_id=other_pid,
                    score=s,
                    updated_at=now,
                )
            )
            await session.commit()

    async with session_factory() as session:
        session.add(
            EventLeaderboard(
                ranking_event_id="score38",
                player_id=pid,
                score=1000000,
                updated_at=now,
            )
        )
        # 2. Player unlocked Emblem 3003804 in UnlockEntityStatusCollection--8
        u_blob = add_unlock_ids_to_collection(None, 8, [3003804], now)
        session.add(
            PlayerStorageEntry(
                player_id=pid,
                key="UnlockEntityStatusCollection--8",
                value=u_blob,
                created_at=now,
                updated_at=now,
            )
        )
        # 3. Player equipped Emblem 3003804 in PlayerBase--1 without IntVal
        pb_blob = encode_raw_protobuf(
            {
                1: [(0, 1)],
                5: [(0, 3003804)],
            }
        )
        session.add(
            PlayerStorageEntry(
                player_id=pid,
                key="PlayerBase--1",
                value=pb_blob,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

        # Run healing pass
        await heal_legacy_ranking_reward_items(session, pid)

        # Check EventEmblemRankingStatusCollection--1 was generated with rank 4
        coll_entry = (
            await session.execute(
                select(PlayerStorageEntry).where(
                    PlayerStorageEntry.player_id == pid,
                    PlayerStorageEntry.key == "EventEmblemRankingStatusCollection--1",
                )
            )
        ).scalar_one_or_none()
        assert coll_entry is not None
        rankings = parse_event_emblem_rankings(coll_entry.value)
        assert rankings.get(3003804) == 4

        # Check PlayerBase--1 has Emblem1IntVal (Tag 18) set to 4
        pb_entry = (
            await session.execute(
                select(PlayerStorageEntry).where(
                    PlayerStorageEntry.player_id == pid,
                    PlayerStorageEntry.key == "PlayerBase--1",
                )
            )
        ).scalar_one_or_none()
        assert pb_entry is not None
        pb_fields = parse_raw_protobuf(pb_entry.value)
        assert pb_fields.get(18, [(0, 0)])[0][1] == 4


@pytest.mark.asyncio
async def test_get_entries_v2_auto_backfill_and_healing() -> None:
    """GetEntriesV2 automatically backfills EventEmblemRankingStatusCollection and heals PlayerBase."""
    pid = f"test-emblem-get-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid, nickname="EmblemGetUser")
    tokens = set_current_player(pid)
    ctx = MockContext()
    now = int(time.time())

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        # Unlock emblem 3003806 (tier 6: 101..500)
        u_blob = add_unlock_ids_to_collection(None, 8, [3003806], now)
        session.add(
            PlayerStorageEntry(
                player_id=pid,
                key="UnlockEntityStatusCollection--8",
                value=u_blob,
                created_at=now,
                updated_at=now,
            )
        )
        # Equip in PlayerBase--1
        pb_blob = encode_raw_protobuf(
            {
                1: [(0, 1)],
                5: [(0, 3003806)],
            }
        )
        session.add(
            PlayerStorageEntry(
                player_id=pid,
                key="PlayerBase--1",
                value=pb_blob,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    storage_svc = PlayerStorageService()
    req = player_storage_pb2.PlayerStorageGetEntriesV2.Request()
    c1 = req.criteria.add()
    c1.matching_type = Criterion.MatchingType.FORWARD
    c1.key = "EventEmblemRankingStatusCollection--"
    c2 = req.criteria.add()
    c2.matching_type = Criterion.MatchingType.EXACT
    c2.key = "PlayerBase--1"

    res = await storage_svc.GetEntriesV2(req, ctx)  # type: ignore[arg-type]

    # 1. EventEmblemRankingStatusCollection--1 should be returned with fallback rank 101
    coll_resp = next(
        (e for e in res.entries if e.key == "EventEmblemRankingStatusCollection--1"), None
    )
    assert coll_resp is not None
    rankings = parse_event_emblem_rankings(coll_resp.value)
    assert rankings.get(3003806) == 101

    # 2. PlayerBase--1 should have Emblem1IntVal=101
    pb_resp = next((e for e in res.entries if e.key == "PlayerBase--1"), None)
    assert pb_resp is not None
    pb_fields = parse_raw_protobuf(pb_resp.value)
    assert pb_fields.get(18, [(0, 0)])[0][1] == 101

    reset_current_player(tokens)
