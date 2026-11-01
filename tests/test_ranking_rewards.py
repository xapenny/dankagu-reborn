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
from dankagu.models.storage import PlayerStorageEntry
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
                PlayerInventoryItem.system_resource_name.like("rank_rew:score38:%"),
            )
            .order_by(PlayerInventoryItem.created_at.asc())
        )
        items = (await session.execute(stmt)).scalars().all()

        # Score38 has 12 tiers (Rank 1 through Rank 5000). Since player is Rank 1,
        # all 12 milestone tiers should be granted!
        assert len(items) == 12

        labels = {it.system_resource_name.split("#")[0] for it in items}
        # Verify specific tiers exist
        assert "rank_rew:score38:tier_1" in labels
        assert "rank_rew:score38:tier_2" in labels
        assert "rank_rew:score38:tier_12" in labels

        # Check Tier 1 reward contains exclusive Rank 1 Emblem 3003801
        tier1_item = next(
            it for it in items if it.system_resource_name.startswith("rank_rew:score38:tier_1")
        )
        assert tier1_item.search_label in ("Other", "Item")
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
            PlayerInventoryItem.system_resource_name.like("rank_rew:score38:%"),
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
            PlayerInventoryItem.system_resource_name.like("rank_rew:point38:%"),
        )
        items = (await session.execute(stmt)).scalars().all()
        assert len(items) > 0

        # Check for card and item rewards
        card_items = [
            it
            for it in items
            if "#Card_" in it.system_resource_name or "#Growth_" in it.system_resource_name
        ]
        assert len(card_items) > 0
        assert any(it.search_label == "Card" for it in card_items)
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
            PlayerInventoryItem.system_resource_name.like("rank_rew:gpx_class:c4%"),
        )
        class_item = (await session.execute(c_stmt)).scalars().first()
        assert class_item is not None
        assert class_item.search_label == "VC"
        c_val = json.loads(class_item.value.decode("utf-8"))
        assert c_val["Prefix"] in ("VC", "VirtualCurrency", "Stone")
        assert c_val["Count"] == 1500  # 1500 VC stones for Bronze tier

        # 2. Check Group rank milestones (Group rank in Class 4)
        g_stmt = select(PlayerInventoryItem).where(
            PlayerInventoryItem.player_id == pid,
            PlayerInventoryItem.system_resource_name.like("rank_rew:gpx_group_c4:%"),
        )
        group_items = (await session.execute(g_stmt)).scalars().all()
        # All tiers matching rank are granted
        assert len(group_items) >= 15
        assert all(it.search_label in ("VC", "Item", "Coin", "Other", "Card") for it in group_items)

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
        has_gpx = any(it.system_resource_name.startswith("rank_rew:gpx_") for it in all_mailbox)
        has_ev = any(it.system_resource_name.startswith("rank_rew:point38:") for it in all_mailbox)
        assert has_gpx
        assert has_ev

    reset_current_player(tokens)


@pytest.mark.asyncio
async def test_mailbox_filtering_and_legacy_healing() -> None:
    """Legacy items with search_label='rank_rew:...' are auto-migrated so client tab filtering finds them."""
    pid = f"test-heal-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid, nickname="HealTester")
    tokens = set_current_player(pid)
    ctx = MockContext()

    now = int(time.time())
    session_factory = get_sessionmaker()

    # 1. Seed legacy items directly into PlayerInventoryItem with search_label = "rank_rew:..."
    async with session_factory() as session:
        legacy_vc = PlayerInventoryItem(
            id=str(uuid.uuid4()),
            player_id=pid,
            item_type=1,
            schema_key="VC",
            value=json.dumps({"Prefix": "VC", "Value": 90001, "Count": 1000}).encode("utf-8"),
            route=4,
            message="【メドレーグランプリ】グループ第1位達成報酬！",
            search_label="rank_rew:gpx_group_c4:tier_1",
            opened_at=now,
            expired_at=now + 86400 * 90,
            system_resource_name="FREE_STONE",
            system_resource_num=1000,
            is_claimed=False,
            created_at=now,
        )
        legacy_item = PlayerInventoryItem(
            id=str(uuid.uuid4()),
            player_id=pid,
            item_type=0,
            schema_key="ITEM",
            value=json.dumps({"Prefix": "GenericItem", "Value": 70010, "Count": 2500}).encode(
                "utf-8"
            ),
            route=4,
            message="【メドレーグランプリ】グループ第1位達成報酬！",
            search_label="rank_rew:gpx_group_c4:tier_1",
            opened_at=now,
            expired_at=now + 86400 * 90,
            system_resource_name="ITEM",
            system_resource_num=2500,
            is_claimed=False,
            created_at=now,
        )
        session.add(legacy_vc)
        session.add(legacy_item)
        await session.commit()

    inv_svc = PlayerInventoryService()

    # 2. Client calls GetInventoriesV1 with search_labels used by PresentService
    client_search_labels = ["VC", "Card", "Item", "Coin", "SelectTicket", "GachaTicket", "Other"]
    req = player_inventory_pb2.PlayerInventoryGetInventoriesV1.Request()
    req.criterion.search_labels.extend(client_search_labels)
    resp = await inv_svc.GetInventoriesV1(req, ctx)  # type: ignore[arg-type]

    # Both items must be found because heal_legacy_ranking_reward_items migrated their search_labels!
    resp_ids = {inv.id for inv in resp.inventories}
    assert legacy_vc.id in resp_ids
    assert legacy_item.id in resp_ids

    # 3. Check DB to ensure search_label was permanently healed and milestone key preserved
    async with session_factory() as session:
        healed_vc = (
            await session.execute(
                select(PlayerInventoryItem).where(PlayerInventoryItem.id == legacy_vc.id)
            )
        ).scalar_one()
        assert healed_vc.search_label == "VC"
        assert healed_vc.system_resource_name.startswith("rank_rew:gpx_group_c4:tier_1")

        healed_item = (
            await session.execute(
                select(PlayerInventoryItem).where(PlayerInventoryItem.id == legacy_item.id)
            )
        ).scalar_one()
        assert healed_item.search_label == "Item"
        assert healed_item.system_resource_name.startswith("rank_rew:gpx_group_c4:tier_1")

    reset_current_player(tokens)


@pytest.mark.asyncio
async def test_class_group_leaderboard_isolated_by_class() -> None:
    """ClassGroup leaderboard only returns players in caller's class, while GrandPrix returns all."""
    from google.protobuf.internal import encoder

    ctx = MockContext()

    # Create Player Bronze (Class 4)
    pid_bronze = f"test-bronze-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid_bronze, nickname="BronzePlayer")
    tok_bronze = set_current_player(pid_bronze)

    storage_svc = PlayerStorageService()
    now = int(time.time())

    # Set GpxInfoStatus with lastClassID = 4 (Bronze)
    gpx_b = bytearray()
    gpx_b += encoder._VarintBytes((1 << 3) | 0) + encoder._VarintBytes(1)
    gpx_b += encoder._VarintBytes((6 << 3) | 0) + encoder._VarintBytes(4)  # Class 4

    req_b = player_storage_pb2.PlayerStorageSetEntriesV2.Request(next_revision="rev_b")
    eb = req_b.entries.add()
    eb.key = "GpxInfoStatus-V1-1"
    eb.value = bytes(gpx_b)
    eb.created_at = now
    eb.updated_at = now
    await storage_svc.SetEntriesV2(req_b, ctx)  # type: ignore[arg-type]

    # Register score 3,000,000 for Bronze
    gpx_svc = ScoreRankingService()
    await gpx_svc.RegisterV1(
        score_ranking_pb2.ScoreRankingRegisterV1.Request(score=3000000),
        ctx,  # type: ignore[arg-type]
    )

    # Create Player Platinum (Class 1)
    pid_plat = f"test-plat-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid_plat, nickname="PlatPlayer")
    tok_plat = set_current_player(pid_plat)

    # Set GpxInfoStatus with lastClassID = 1 (Platinum)
    gpx_p = bytearray()
    gpx_p += encoder._VarintBytes((1 << 3) | 0) + encoder._VarintBytes(1)
    gpx_p += encoder._VarintBytes((6 << 3) | 0) + encoder._VarintBytes(1)  # Class 1

    req_p = player_storage_pb2.PlayerStorageSetEntriesV2.Request(next_revision="rev_p")
    ep = req_p.entries.add()
    ep.key = "GpxInfoStatus-V1-1"
    ep.value = bytes(gpx_p)
    ep.created_at = now
    ep.updated_at = now
    await storage_svc.SetEntriesV2(req_p, ctx)  # type: ignore[arg-type]

    # Register score 6,000,000 for Platinum
    await gpx_svc.RegisterV1(
        score_ranking_pb2.ScoreRankingRegisterV1.Request(score=6000000),
        ctx,  # type: ignore[arg-type]
    )

    # 1. Check Bronze Player's Class Group Leaderboard
    set_current_player(pid_bronze)
    bronze_group_lb = await gpx_svc.GetClassGroupLeaderBoardV1(
        score_ranking_pb2.ScoreRankingGetClassGroupLeaderBoardV1.Request(),
        ctx,  # type: ignore[arg-type]
    )
    bronze_group_pids = [entry.player_id for entry in bronze_group_lb.player_rank_infos]
    assert pid_bronze in bronze_group_pids
    assert pid_plat not in bronze_group_pids  # Platinum player is NOT in Bronze group!

    # Bronze Player's Class Group Rank is 1 (within Bronze)
    bronze_group_rank = await gpx_svc.GetClassGroupRankV1(
        score_ranking_pb2.ScoreRankingGetClassGroupRankV1.Request(),
        ctx,  # type: ignore[arg-type]
    )
    assert len(bronze_group_rank.player_class_group_rank_infos) > 0
    assert bronze_group_rank.player_class_group_rank_infos[0].rank == 1

    # 2. Check Platinum Player's Class Group Leaderboard
    set_current_player(pid_plat)
    plat_group_lb = await gpx_svc.GetClassGroupLeaderBoardV1(
        score_ranking_pb2.ScoreRankingGetClassGroupLeaderBoardV1.Request(),
        ctx,  # type: ignore[arg-type]
    )
    plat_group_pids = [entry.player_id for entry in plat_group_lb.player_rank_infos]
    assert pid_plat in plat_group_pids
    assert pid_bronze not in plat_group_pids  # Bronze player is NOT in Platinum group!

    plat_group_rank = await gpx_svc.GetClassGroupRankV1(
        score_ranking_pb2.ScoreRankingGetClassGroupRankV1.Request(),
        ctx,  # type: ignore[arg-type]
    )
    assert len(plat_group_rank.player_class_group_rank_infos) > 0
    assert plat_group_rank.player_class_group_rank_infos[0].rank == 1

    # 3. Check Grand Prix Leaderboard (Global across all classes)
    set_current_player(pid_bronze)
    global_lb = await gpx_svc.GetGrandPrixLeaderBoardV1(
        score_ranking_pb2.ScoreRankingGetGrandPrixLeaderBoardV1.Request(),
        ctx,  # type: ignore[arg-type]
    )
    global_pids = [entry.player_id for entry in global_lb.player_rank_infos]
    assert pid_plat in global_pids
    assert pid_bronze in global_pids
    # In global, Plat (6M) is #1, Bronze (3M) is #2
    plat_entry = next(e for e in global_lb.player_rank_infos if e.player_id == pid_plat)
    bronze_entry = next(e for e in global_lb.player_rank_infos if e.player_id == pid_bronze)
    assert plat_entry.rank < bronze_entry.rank

    bronze_global_rank = await gpx_svc.GetGrandPrixRankV1(
        score_ranking_pb2.ScoreRankingGetGrandPrixRankV1.Request(),
        ctx,  # type: ignore[arg-type]
    )
    assert len(bronze_global_rank.player_grand_prix_rank_infos) > 0
    assert bronze_global_rank.player_grand_prix_rank_infos[0].rank >= 2  # Behind platinum!

    reset_current_player(tok_bronze)
    reset_current_player(tok_plat)


@pytest.mark.asyncio
async def test_skip_already_unlocked_emblem_and_capped_items() -> None:
    """Milestone reward distribution skips unique emblems already owned and capped items."""
    pid = f"test-skip-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid, nickname="SkipTester")
    tok = set_current_player(pid)
    ctx = MockContext()

    # 1. Player already owns Emblem 3003812 in UnlockEntityStatusCollection--8
    storage_svc = PlayerStorageService()
    now = int(time.time())
    from dankagu.services.box_loot_box import encode_raw_protobuf

    emblem_sub = {
        1: [(0, now)],
        2: [(0, now)],
        3: [(0, 8)],
        4: [(0, 3003812)],
        6: [(0, 1)],
    }
    emblem_col = {
        1: [(0, 8)],
        2: [(2, encode_raw_protobuf(emblem_sub))],
    }

    req_storage = player_storage_pb2.PlayerStorageSetEntriesV2.Request(next_revision="rev_emb")
    e_emb = req_storage.entries.add()
    e_emb.key = "UnlockEntityStatusCollection--8"
    e_emb.value = encode_raw_protobuf(emblem_col)
    e_emb.created_at = now
    e_emb.updated_at = now

    # 2. Player also already has 99 of kaleidoscope 32004
    growth_item = {
        1: [(0, 32004)],
        2: [(0, 99)],
        4: [(0, now)],
    }
    e_growth = req_storage.entries.add()
    e_growth.key = "GrowthItemStock--32004"
    e_growth.value = encode_raw_protobuf(growth_item)
    e_growth.created_at = now
    e_growth.updated_at = now

    await storage_svc.SetEntriesV2(req_storage, ctx)  # type: ignore[arg-type]

    # 3. Trigger rank 500,000 in Event Score 38 (which would grant Emblem 3003812 in tier 12)
    ranking_svc = RankingService()
    await ranking_svc.RegisterV1(
        ranking_pb2.RankingRegisterV1.Request(ranking_event_id="38", score=1000),
        ctx,  # type: ignore[arg-type]
    )

    # 4. Check mailbox: Emblem 3003812 must NOT be in mailbox!
    inv_svc = PlayerInventoryService()
    res = await inv_svc.GetInventoriesV1(
        player_inventory_pb2.PlayerInventoryGetInventoriesV1.Request(),
        ctx,  # type: ignore[arg-type]
    )
    for inv in res.inventories:
        val_d = json.loads(inv.value.decode("utf-8"))
        assert val_d.get("Value") != 3003812, "Emblem 3003812 should have been skipped!"

    reset_current_player(tok)


@pytest.mark.asyncio
async def test_heal_legacy_ranking_reward_auto_claims_blocked_items() -> None:
    """Heal legacy migration auto-claims blocked items (already owned emblems or capped items)."""
    pid = f"test-heal-block-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid, nickname="HealBlockTester")
    tok = set_current_player(pid)
    ctx = MockContext()

    storage_svc = PlayerStorageService()
    now = int(time.time())
    from dankagu.services.box_loot_box import encode_raw_protobuf

    # Player has Emblem 3003801
    emblem_sub = {1: [(0, now)], 2: [(0, now)], 3: [(0, 8)], 4: [(0, 3003801)], 6: [(0, 1)]}
    emblem_col = {1: [(0, 8)], 2: [(2, encode_raw_protobuf(emblem_sub))]}
    req_storage = player_storage_pb2.PlayerStorageSetEntriesV2.Request(next_revision="rev_emb2")
    e_emb = req_storage.entries.add()
    e_emb.key = "UnlockEntityStatusCollection--8"
    e_emb.value = encode_raw_protobuf(emblem_col)
    e_emb.created_at = now
    e_emb.updated_at = now
    await storage_svc.SetEntriesV2(req_storage, ctx)  # type: ignore[arg-type]

    # Seed 2 unclaimed items directly in DB:
    # Item 1: Emblem 3003801 (blocked because already owned)
    # Item 2: Free Stones (VC 90001, claimable)
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        session.add(
            PlayerInventoryItem(
                id=str(uuid.uuid4()),
                player_id=pid,
                item_type=0,
                schema_key="ITEM",
                value=json.dumps({"Prefix": "Emblem", "Value": 3003801, "Count": 1}).encode(
                    "utf-8"
                ),
                route=4,
                message="Emblem reward",
                search_label="rank_rew:score38:tier_1",
                opened_at=now,
                expired_at=now + 86400,
                system_resource_name="rank_rew:score38:tier_1#Emblem_3003801",
                system_resource_num=1,
                is_claimed=False,
                created_at=now,
            )
        )
        session.add(
            PlayerInventoryItem(
                id=str(uuid.uuid4()),
                player_id=pid,
                item_type=1,
                schema_key="ITEM",
                value=json.dumps({"Prefix": "VC", "Value": 90001, "Count": 5000}).encode("utf-8"),
                route=4,
                message="Stones reward",
                search_label="rank_rew:score38:tier_1",
                opened_at=now,
                expired_at=now + 86400,
                system_resource_name="rank_rew:score38:tier_1#VC_90001",
                system_resource_num=5000,
                is_claimed=False,
                created_at=now,
            )
        )
        await session.commit()

    # Opening the mailbox should auto-heal: Emblem marked as claimed, Free Stones preserved as VC tab
    inv_svc = PlayerInventoryService()
    res = await inv_svc.GetInventoriesV1(
        player_inventory_pb2.PlayerInventoryGetInventoriesV1.Request(),
        ctx,  # type: ignore[arg-type]
    )
    assert len(res.inventories) == 1
    assert res.inventories[0].search_label == "VC"
    assert res.inventories[0].system_resource_num == 5000

    # Claim the remaining stone gift smoothly
    rec_req = player_inventory_pb2.PlayerInventoryReceiveV1.Request(
        delete_inventory_ids=[res.inventories[0].id]
    )
    rec_resp = await inv_svc.ReceiveV1(rec_req, ctx)  # type: ignore[arg-type]
    assert rec_resp is not None

    reset_current_player(tok)


@pytest.mark.asyncio
async def test_event_card_milestone_deduplication_and_healing() -> None:
    """Card rewards are deduplicated: skipped if owned, clamped to 1, and duplicate mails auto-healed."""
    pid = f"test-card-heal-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid, nickname="CardHealTester")
    tok = set_current_player(pid)
    ctx = MockContext()

    storage_svc = PlayerStorageService()
    now = int(time.time())
    from dankagu.services.box_loot_box import encode_raw_protobuf
    from dankagu.services.player_inventory import encode_player_status, parse_player_status

    # 1. Player already owns Card 100369 in CardStatus--12345
    card_pb = {
        1: [(0, 12345)],
        2: [(0, 100369)],  # card_master_id = 100369
        3: [(0, now)],
    }
    ps_initial = {1: 1, 7: 0}  # cardMaxAdd_ = 0

    req_storage = player_storage_pb2.PlayerStorageSetEntriesV2.Request(next_revision="rev_c1")
    e_card = req_storage.entries.add()
    e_card.key = "CardStatus--12345"
    e_card.value = encode_raw_protobuf(card_pb)
    e_card.created_at = now
    e_card.updated_at = now

    e_ps = req_storage.entries.add()
    e_ps.key = "PlayerStatus--1"
    e_ps.value = encode_player_status(ps_initial)
    e_ps.created_at = now
    e_ps.updated_at = now

    await storage_svc.SetEntriesV2(req_storage, ctx)  # type: ignore[arg-type]

    # 2. Register Rank 1 in Event Point 38 (which would otherwise grant 100369 and 100370 across 13 tiers)
    ranking_svc = RankingService()
    await ranking_svc.RegisterV1(
        ranking_pb2.RankingRegisterV1.Request(ranking_event_id="point38", score=5000000),
        ctx,  # type: ignore[arg-type]
    )

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        # Check all granted items for this player
        stmt = select(PlayerInventoryItem).where(
            PlayerInventoryItem.player_id == pid,
            PlayerInventoryItem.system_resource_name.like("rank_rew:point38:%"),
        )
        items = (await session.execute(stmt)).scalars().all()
        card_items = [it for it in items if "#Card_" in it.system_resource_name]

        # Card 100369 was already owned, so it MUST NOT be granted!
        assert not any("#Card_100369" in it.system_resource_name for it in card_items)

        # Card 100370 was NOT owned, so it MUST be granted exactly ONCE across all 13 tiers!
        pal_items = [it for it in card_items if "#Card_100370" in it.system_resource_name]
        assert len(pal_items) == 1
        pal_data = json.loads(pal_items[0].value.decode("utf-8"))
        assert pal_data["Prefix"] == "Card"
        assert pal_data["Value"] == 100370
        assert pal_data["Count"] == 1  # Count is clamped to 1!

    # 3. Simulate legacy unhealed state: seed 3 duplicate mails of 100370 with Count = 17, 10, 5
    async with session_factory() as session:
        for cnt in (17, 10, 5):
            session.add(
                PlayerInventoryItem(
                    id=str(uuid.uuid4()),
                    player_id=pid,
                    item_type=0,
                    schema_key="CARD",
                    value=json.dumps({"Prefix": "Card", "Value": 100370, "Count": cnt}).encode(
                        "utf-8"
                    ),
                    route=4,
                    message="Duplicate card mail",
                    search_label="Card",
                    opened_at=now,
                    expired_at=now + 86400,
                    system_resource_name="rank_rew:point38:tier_test#Card_100370",
                    system_resource_num=cnt,
                    is_claimed=False,
                    created_at=now,
                )
            )
        await session.commit()

    # 4. Opening mailbox triggers heal_legacy_ranking_reward_items
    inv_svc = PlayerInventoryService()
    res = await inv_svc.GetInventoriesV1(
        player_inventory_pb2.PlayerInventoryGetInventoriesV1.Request(),
        ctx,  # type: ignore[arg-type]
    )

    # In mailbox: exactly 1 card item for 100370 must remain, and its Count must be 1!
    active_cards = [inv for inv in res.inventories if inv.search_label == "Card"]
    assert len(active_cards) == 1
    assert active_cards[0].system_resource_num == 1
    active_data = json.loads(active_cards[0].value.decode("utf-8"))
    assert active_data["Count"] == 1

    # Verify PlayerStatus--1 was upgraded to at least 300
    async with session_factory() as session:
        ps_stmt = select(PlayerStorageEntry).where(
            PlayerStorageEntry.player_id == pid,
            PlayerStorageEntry.key == "PlayerStatus--1",
        )
        ps_saved = (await session.execute(ps_stmt)).scalar_one()
        fields = parse_player_status(ps_saved.value)
        assert fields.get(7, 0) >= 300

    # 5. Successfully claim the card item
    rec_req = player_inventory_pb2.PlayerInventoryReceiveV1.Request(
        delete_inventory_ids=[active_cards[0].id]
    )
    rec_resp = await inv_svc.ReceiveV1(rec_req, ctx)  # type: ignore[arg-type]
    assert rec_resp is not None

    reset_current_player(tok)
