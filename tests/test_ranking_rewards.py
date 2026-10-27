"""Tests for immediate milestone ranking rewards delivery in Events and Medley Grand Prix."""

import json
import time
import uuid

import pytest
from player_factory import ensure_player
from sqlalchemy import select

from dankagu.core.database import get_sessionmaker
from dankagu.core.session import reset_current_player, set_current_player
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_inventory_pb2,
    player_storage_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    ranking_pb2,
    score_ranking_pb2,
)
from dankagu.models.inventory import PlayerInventoryItem
from dankagu.models.ranking import EventLeaderboard
from dankagu.services.player_inventory import PlayerInventoryService
from dankagu.services.player_storage import PlayerStorageService
from dankagu.services.ranking import RankingService, ScoreRankingService


class MockContext:
    """Mock gRPC context."""

    def __init__(self) -> None:
        self.code = None
        self.details = None

    async def abort(self, code, details):
        self.code = code
        self.details = details
        raise RuntimeError(f"RPC Aborted: {code} - {details}")


@pytest.mark.asyncio
async def test_event_score_ranking_instant_milestones() -> None:
    """Achieving a score rank immediately distributes all reached milestone rewards to mailbox."""
    pid = f"test-ev-score-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid, nickname="EventTester")
    tokens = set_current_player(pid)
    ctx = MockContext()

    ranking_svc = RankingService()
    event_id = "38"

    # 1. Register a top score (5,000,000) -> Player gets Rank 1
    reg_req = ranking_pb2.RankingRegisterV1.Request(
        ranking_event_id=event_id,
        score=5000000,
    )
    reg_res = await ranking_svc.RegisterV1(reg_req, ctx)  # type: ignore[arg-type]
    assert reg_res.score == 5000000

    # 2. Check PlayerInventoryItem table for milestone mail items
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        stmt = (
            select(PlayerInventoryItem)
            .where(
                PlayerInventoryItem.player_id == pid,
                PlayerInventoryItem.search_label.like("rank_rew:score38:%"),
            )
            .order_by(PlayerInventoryItem.created_at.asc())
        )
        items = (await session.execute(stmt)).scalars().all()

        # Score38 has 12 tiers (Rank 1 through Rank 5000). Since player is Rank 1,
        # all 12 milestone tiers should be granted!
        assert len(items) == 12

        labels = {it.search_label for it in items}
        # Verify specific tiers exist
        assert "rank_rew:score38:tier_1" in labels
        assert "rank_rew:score38:tier_2" in labels
        assert "rank_rew:score38:tier_12" in labels

        # Check Tier 1 reward contains exclusive Rank 1 Emblem 3003801
        tier1_item = next(it for it in items if it.search_label == "rank_rew:score38:tier_1")
        data = json.loads(tier1_item.value.decode("utf-8"))
        assert data["Prefix"] == "Emblem"
        assert data["Value"] == 3003801
        assert data["Count"] == 1
        assert "達成" in tier1_item.message or "入賞" in tier1_item.message

    # 3. Idempotency test: Registering another higher score should NOT duplicate rewards
    reg_req2 = ranking_pb2.RankingRegisterV1.Request(
        ranking_event_id=event_id,
        score=5500000,
    )
    reg_res2 = await ranking_svc.RegisterV1(reg_req2, ctx)  # type: ignore[arg-type]
    assert reg_res2.score == 5500000

    async with session_factory() as session:
        count_stmt = select(PlayerInventoryItem).where(
            PlayerInventoryItem.player_id == pid,
            PlayerInventoryItem.search_label.like("rank_rew:score38:%"),
        )
        count_items = (await session.execute(count_stmt)).scalars().all()
        assert len(count_items) == 12  # Still exactly 12 items!

    reset_current_player(tokens)


@pytest.mark.asyncio
async def test_event_point_ranking_milestones_and_claim() -> None:
    """Achieving event point milestones awards cards, orbs, and tickets, claimable via ReceiveV1."""
    pid = f"test-ev-point-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid, nickname="PointTester")
    tokens = set_current_player(pid)
    ctx = MockContext()

    ranking_svc = RankingService()
    point_event_id = "point38"

    # Register point score
    reg_req = ranking_pb2.RankingRegisterV1.Request(
        ranking_event_id=point_event_id,
        score=200000,
    )
    await ranking_svc.RegisterV1(reg_req, ctx)  # type: ignore[arg-type]

    # Verify items were created
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        stmt = select(PlayerInventoryItem).where(
            PlayerInventoryItem.player_id == pid,
            PlayerInventoryItem.search_label.like("rank_rew:point38:%"),
        )
        items = (await session.execute(stmt)).scalars().all()
        assert len(items) > 0

        # Check for card and item rewards
        card_items = [
            it
            for it in items
            if "Card" in it.system_resource_name or "Growth" in it.system_resource_name
        ]
        assert len(card_items) > 0
        item_ids_to_receive = [it.id for it in items[:5]]

    # Claim items through PlayerInventoryService
    inv_svc = PlayerInventoryService()
    rec_req = player_inventory_pb2.PlayerInventoryReceiveV1.Request(
        delete_inventory_ids=item_ids_to_receive
    )
    rec_res = await inv_svc.ReceiveV1(rec_req, ctx)  # type: ignore[arg-type]
    assert rec_res is not None

    # Verify database marks them claimed
    async with session_factory() as session:
        stmt_claimed = select(PlayerInventoryItem).where(
            PlayerInventoryItem.id.in_(item_ids_to_receive)
        )
        claimed_items = (await session.execute(stmt_claimed)).scalars().all()
        for ci in claimed_items:
            assert ci.is_claimed is True

    reset_current_player(tokens)


@pytest.mark.asyncio
async def test_gpx_class_and_group_instant_milestones() -> None:
    """Registering Grand Prix score grants Class and Group milestone rewards immediately."""
    pid = f"test-gpx-rew-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid, nickname="GpxChampion")
    tokens = set_current_player(pid)
    ctx = MockContext()

    gpx_svc = ScoreRankingService()

    # Register score in Grand Prix (New player defaults to Class 4 Bronze)
    reg_req = score_ranking_pb2.ScoreRankingRegisterV1.Request(score=3000000)
    reg_res = await gpx_svc.RegisterV1(reg_req, ctx)  # type: ignore[arg-type]
    assert reg_res.score == 3000000
    assert reg_res.rank >= 1

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        # 1. Check Class milestone (Class 4 Bronze milestone granted)
        c_stmt = select(PlayerInventoryItem).where(
            PlayerInventoryItem.player_id == pid,
            PlayerInventoryItem.search_label == "rank_rew:gpx_class:c4",
        )
        class_item = (await session.execute(c_stmt)).scalars().first()
        assert class_item is not None
        c_val = json.loads(class_item.value.decode("utf-8"))
        assert c_val["Prefix"] in ("VC", "VirtualCurrency", "Stone")
        assert c_val["Count"] == 1500  # 1500 VC stones for Bronze tier

        # 2. Check Group rank milestones (Group rank in Class 4)
        g_stmt = select(PlayerInventoryItem).where(
            PlayerInventoryItem.player_id == pid,
            PlayerInventoryItem.search_label.like("rank_rew:gpx_group_c4:%"),
        )
        group_items = (await session.execute(g_stmt)).scalars().all()
        # All tiers matching rank are granted
        assert len(group_items) >= 15

    # 3. Test ReceiveClassPrizeV1 RPC also returns success idempotently
    prize_req = score_ranking_pb2.ScoreRankingReceiveClassPrizeV1.Request()
    prize_res = await gpx_svc.ReceiveClassPrizeV1(prize_req, ctx)  # type: ignore[arg-type]
    assert prize_res is not None

    reset_current_player(tokens)


@pytest.mark.asyncio
async def test_player_storage_auto_sync_gpx_and_event_milestones() -> None:
    """Saving GpxInfoStatus or EventInfoStatus in PlayerStorage auto-syncs leaderboards and grants milestones."""
    pid = f"test-storage-sync-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid, nickname="StorageSyncTester")
    tokens = set_current_player(pid)
    ctx = MockContext()

    storage_svc = PlayerStorageService()

    # 1. Craft GpxInfoStatusProto with Phase 5 (1005000) score = 1,800,000, lastClassID = 1
    from google.protobuf.internal import encoder

    p5_bytes = bytearray()
    p5_bytes += encoder._VarintBytes((1 << 3) | 0) + encoder._VarintBytes(1005000)
    p5_bytes += encoder._VarintBytes((2 << 3) | 2) + b"\x03\x00\x00\x00"
    # scores: 600000, 600000, 600000
    sc_buf = (
        encoder._VarintBytes(600000) + encoder._VarintBytes(600000) + encoder._VarintBytes(600000)
    )
    p5_bytes += (
        encoder._VarintBytes((4 << 3) | 2) + encoder._VarintBytes(len(sc_buf)) + bytes(sc_buf)
    )
    p5_bytes += encoder._VarintBytes((5 << 3) | 0) + encoder._VarintBytes(1800000)

    gpx_root = bytearray()
    gpx_root += encoder._VarintBytes((1 << 3) | 0) + encoder._VarintBytes(1)  # ID=1
    gpx_root += (
        encoder._VarintBytes((2 << 3) | 2) + encoder._VarintBytes(len(p5_bytes)) + bytes(p5_bytes)
    )
    gpx_root += encoder._VarintBytes((6 << 3) | 0) + encoder._VarintBytes(1)  # lastClassID=1

    # 2. Craft EventInfoStatusProto: event_id=38, event_point=150000, max_score=2200000
    ev_bytes = bytearray()
    ev_bytes += encoder._VarintBytes((1 << 3) | 0) + encoder._VarintBytes(38)  # event_id = 38
    ev_bytes += encoder._VarintBytes((2 << 3) | 0) + encoder._VarintBytes(150000)  # points = 150000
    ev_bytes += encoder._VarintBytes((5 << 3) | 0) + encoder._VarintBytes(
        2200000
    )  # score = 2200000

    now = int(time.time())
    set_req = player_storage_pb2.PlayerStorageSetEntriesV2.Request(
        next_revision="rev_1",
    )
    e1 = set_req.entries.add()
    e1.key = "GpxInfoStatus-V1-1"
    e1.value = bytes(gpx_root)
    e1.created_at = now
    e1.updated_at = now

    e2 = set_req.entries.add()
    e2.key = "EventInfoStatus-V38-38"
    e2.value = bytes(ev_bytes)
    e2.created_at = now
    e2.updated_at = now

    await storage_svc.SetEntriesV2(set_req, ctx)  # type: ignore[arg-type]

    # Verify Leaderboards and Mailbox items
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        # Check GPX Leaderboard
        gpx_lb = (
            await session.execute(
                select(EventLeaderboard).where(
                    EventLeaderboard.ranking_event_id == "gpx_s1_p1005",
                    EventLeaderboard.player_id == pid,
                )
            )
        ).scalar()
        assert gpx_lb is not None
        assert gpx_lb.score == 1800000

        # Check Event Point Leaderboard
        pt_lb = (
            await session.execute(
                select(EventLeaderboard).where(
                    EventLeaderboard.ranking_event_id == "point38",
                    EventLeaderboard.player_id == pid,
                )
            )
        ).scalar()
        assert pt_lb is not None
        assert pt_lb.score == 150000

        # Check Event Score Leaderboard
        sc_lb = (
            await session.execute(
                select(EventLeaderboard).where(
                    EventLeaderboard.ranking_event_id == "score38",
                    EventLeaderboard.player_id == pid,
                )
            )
        ).scalar()
        assert sc_lb is not None
        assert sc_lb.score == 2200000

        # Check Mailbox Items for GPX and Event milestones
        m_stmt = select(PlayerInventoryItem).where(PlayerInventoryItem.player_id == pid)
        all_mailbox = (await session.execute(m_stmt)).scalars().all()
        assert len(all_mailbox) > 0

        # Confirm both GPX and Event items exist in mailbox
        has_gpx = any(it.search_label.startswith("rank_rew:gpx_") for it in all_mailbox)
        has_ev = any(it.search_label.startswith("rank_rew:point38:") for it in all_mailbox)
        assert has_gpx
        assert has_ev

    reset_current_player(tokens)
