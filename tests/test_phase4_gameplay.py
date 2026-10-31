"""Integration and unit tests for Phase 4: Gameplay settlement, leaderboards, shrine reconstruction & login tracking."""

import time
import uuid

import grpc
import pytest
from sqlalchemy import select

from dankagu.core.database import get_sessionmaker
from dankagu.core.session import set_current_player
from dankagu.core.starter import seed_starter_player
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_inventory_pb2,
    system_pb2,
    wallet_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    reconstruction_spot_pb2,
    regular_ranking_pb2,
    score_ranking_pb2,
)
from dankagu.models.player import Player
from dankagu.models.storage import PlayerStorageEntry
from dankagu.services.player_inventory import PlayerInventoryService
from dankagu.services.ranking import RegularRankingService, ScoreRankingService
from dankagu.services.reconstruction_spot import ReconstructionSpotService
from dankagu.services.system import (
    SystemService,
    get_jst_game_day,
    process_player_login,
)
from dankagu.services.wallet import WalletService


class DummyContext:
    """Mock gRPC ServicerContext for testing."""

    def __init__(self) -> None:
        self.aborted = False
        self.code = None
        self.details = None

    async def abort(self, code: grpc.StatusCode, details: str) -> None:
        self.aborted = True
        self.code = code
        self.details = details
        raise grpc.aio.AioRpcError(
            code=code, initial_metadata=(), trailing_metadata=(), details=details
        )


@pytest.fixture
async def sample_player() -> Player:
    """Create and seed a test player."""
    session_factory = get_sessionmaker()
    pid = f"test-p4-{uuid.uuid4().hex[:8]}"
    uid = f"usr-{uuid.uuid4().hex[:8]}"
    now = int(time.time())

    async with session_factory() as session:
        player = Player(
            id=pid,
            user_id=uid,
            name="Marisa",
            nickname="OrdinaryMagician",
            registered_at=now,
            last_login_at=now,
            total_login_days=1,
        )
        session.add(player)
        await session.flush()
        await seed_starter_player(session, player)
        await session.commit()
        await session.refresh(player)

    return player


@pytest.fixture
async def second_player() -> Player:
    """Create a second test player for ranking competitions."""
    session_factory = get_sessionmaker()
    pid = f"test-p4-{uuid.uuid4().hex[:8]}"
    uid = f"usr-{uuid.uuid4().hex[:8]}"
    now = int(time.time())

    async with session_factory() as session:
        player = Player(
            id=pid,
            user_id=uid,
            name="Reimu",
            nickname="ShrineMaiden",
            registered_at=now,
            last_login_at=now,
            total_login_days=1,
        )
        session.add(player)
        await session.flush()
        await seed_starter_player(session, player)
        await session.commit()
        await session.refresh(player)

    return player


@pytest.mark.asyncio
async def test_regular_ranking_register_and_rank(
    sample_player: Player, second_player: Player
) -> None:
    """Test registering song scores, high-score preservation, and dynamic rank updates."""
    ranking_service = RegularRankingService()
    ranking_key = f"stage_test_{uuid.uuid4().hex[:6]}_extra"
    ctx = DummyContext()

    # 1. Player 1 registers initial score: 500,000
    set_current_player(sample_player.id, "dummy_token")
    req1 = regular_ranking_pb2.RegularRankingRegisterV1.Request(
        ranking_key=ranking_key,
        score=500000,
    )
    res1 = await ranking_service.RegisterV1(req1, ctx)
    assert res1.player_ranking.player_id == sample_player.id
    assert res1.player_ranking.nick_name == "OrdinaryMagician"
    assert res1.player_ranking.score == 500000
    assert res1.player_ranking.rank == 1

    # 2. Player 1 registers a lower score: 400,000 (should retain 500,000)
    req_lower = regular_ranking_pb2.RegularRankingRegisterV1.Request(
        ranking_key=ranking_key,
        score=400000,
    )
    res_lower = await ranking_service.RegisterV1(req_lower, ctx)
    assert res_lower.player_ranking.score == 500000
    assert res_lower.player_ranking.rank == 1

    # 3. Player 2 registers higher score: 800,000 -> Player 2 is rank 1
    set_current_player(second_player.id, "dummy_token")
    req2 = regular_ranking_pb2.RegularRankingRegisterV1.Request(
        ranking_key=ranking_key,
        score=800000,
    )
    res2 = await ranking_service.RegisterV1(req2, ctx)
    assert res2.player_ranking.player_id == second_player.id
    assert res2.player_ranking.nick_name == "ShrineMaiden"
    assert res2.player_ranking.score == 800000
    assert res2.player_ranking.rank == 1

    # 4. Player 1 checks or registers again: rank should now be 2
    set_current_player(sample_player.id, "dummy_token")
    req_p1_re = regular_ranking_pb2.RegularRankingRegisterV1.Request(
        ranking_key=ranking_key,
        score=550000,
    )
    res_p1_re = await ranking_service.RegisterV1(req_p1_re, ctx)
    assert res_p1_re.player_ranking.score == 550000
    assert res_p1_re.player_ranking.rank == 2


@pytest.mark.asyncio
async def test_regular_ranking_get_top_ranking(
    sample_player: Player, second_player: Player
) -> None:
    """Test fetching paginated top rankings with ordered entries and nicknames."""
    ranking_service = RegularRankingService()
    ranking_key = f"stage_top_{uuid.uuid4().hex[:6]}"
    ctx = DummyContext()

    # Seed two players
    set_current_player(sample_player.id, "dummy_token")
    await ranking_service.RegisterV1(
        regular_ranking_pb2.RegularRankingRegisterV1.Request(ranking_key=ranking_key, score=300000),
        ctx,
    )
    set_current_player(second_player.id, "dummy_token")
    await ranking_service.RegisterV1(
        regular_ranking_pb2.RegularRankingRegisterV1.Request(ranking_key=ranking_key, score=900000),
        ctx,
    )

    # Fetch top ranking
    top_req = regular_ranking_pb2.RegularRankingGetTopRankingV1.Request(
        ranking_key=ranking_key,
        start=1,
        count=10,
    )
    top_res = await ranking_service.GetTopRankingV1(top_req, ctx)
    assert len(top_res.player_rankings) == 2

    first = top_res.player_rankings[0]
    assert first.player_id == second_player.id
    assert first.nick_name == "ShrineMaiden"
    assert first.score == 900000
    assert first.rank == 1

    second = top_res.player_rankings[1]
    assert second.player_id == sample_player.id
    assert second.nick_name == "OrdinaryMagician"
    assert second.score == 300000
    assert second.rank == 2

    # Test pagination: start=2, count=1
    page2_req = regular_ranking_pb2.RegularRankingGetTopRankingV1.Request(
        ranking_key=ranking_key,
        start=2,
        count=1,
    )
    page2_res = await ranking_service.GetTopRankingV1(page2_req, ctx)
    assert len(page2_res.player_rankings) == 1
    assert page2_res.player_rankings[0].player_id == sample_player.id
    assert page2_res.player_rankings[0].rank == 2


@pytest.mark.asyncio
async def test_score_ranking_session_integration(sample_player: Player) -> None:
    """Test ScoreRankingService uses authenticated player context."""
    set_current_player(sample_player.id, "token_p4")
    score_service = ScoreRankingService()
    ctx = DummyContext()

    gp_res = await score_service.GetGrandPrixRankV1(
        score_ranking_pb2.ScoreRankingGetGrandPrixRankV1.Request(), ctx
    )
    assert len(gp_res.player_grand_prix_rank_infos) == 1
    assert gp_res.player_grand_prix_rank_infos[0].player_id == sample_player.id

    cg_res = await score_service.GetClassGroupRankV1(
        score_ranking_pb2.ScoreRankingGetClassGroupRankV1.Request(), ctx
    )
    assert len(cg_res.player_class_group_rank_infos) == 1
    assert cg_res.player_class_group_rank_infos[0].player_id == sample_player.id


@pytest.mark.asyncio
async def test_reconstruction_spot_receive_prizes(sample_player: Player) -> None:
    """Test receiving Hakurei Shrine reconstruction prizes into mailbox and claiming them."""
    set_current_player(sample_player.id, "token_recon")
    recon_service = ReconstructionSpotService()
    inv_service = PlayerInventoryService()
    wallet_service = WalletService()
    ctx = DummyContext()

    # Initial balance check
    bal_res = await wallet_service.GetBalancesV2(wallet_pb2.WalletGetBalancesV2.Request(), ctx)
    vc_map = {vc.virtual_currency_name: vc.amount for vc in bal_res.total.virtual_currencies}
    initial_free = vc_map.get("FREE_STONE", 0)

    # Submit reconstruction event logs to ReceivePrizesV1
    req = reconstruction_spot_pb2.ReconstructionSpotReceivePrizesV1.Request()
    log1 = req.player_event_logs.add()
    log1.event_category = "SpotClear"
    log1.event_id = "hakurei_stage_1"
    log2 = req.player_event_logs.add()
    log2.event_category = "SpotUpgrade"
    log2.event_id = "hakurei_torii_upgrade"

    res = await recon_service.ReceivePrizesV1(req, ctx)
    # One milestone prize is issued per call, regardless of how many event logs
    # are reported: the servicer only asks whether any arrived.
    assert len(res.inventories) == 1
    for inv in res.inventories:
        assert inv.player_id == sample_player.id
        assert inv.system_resource_name == "FREE_STONE"
        assert inv.system_resource_num == 100
        assert inv.search_label == "CoopProgressItem"

    # Verify mailbox contains the prize
    box_res = await inv_service.GetInventoriesV1(
        player_inventory_pb2.PlayerInventoryGetInventoriesV1.Request(), ctx
    )
    recon_gifts = [g for g in box_res.inventories if g.search_label == "CoopProgressItem"]
    assert len(recon_gifts) == 1

    # Claim the gifts
    claim_req = player_inventory_pb2.PlayerInventoryReceiveV1.Request(
        delete_inventory_ids=[g.id for g in recon_gifts]
    )
    await inv_service.ReceiveV1(claim_req, ctx)

    # Verify wallet has +100 free stones
    bal_after = await wallet_service.GetBalancesV2(wallet_pb2.WalletGetBalancesV2.Request(), ctx)
    vc_map_after = {
        vc.virtual_currency_name: vc.amount for vc in bal_after.total.virtual_currencies
    }
    assert vc_map_after.get("FREE_STONE", 0) == initial_free + 100


def test_jst_game_day_boundary() -> None:
    """Verify exact 04:00 JST daily boundary calculation."""
    # 2026-10-10 03:59:59 JST = 2026-10-09 18:59:59 UTC
    # Epoch for 2026-10-09 18:59:59 UTC: 1791572399
    # Epoch for 2026-10-09 19:00:00 UTC (04:00:00 JST): 1791572400
    ts_before_4am_jst = 1791572399
    ts_at_4am_jst = 1791572400

    day_before = get_jst_game_day(ts_before_4am_jst)
    day_at = get_jst_game_day(ts_at_4am_jst)

    assert day_at == day_before + 1


@pytest.mark.asyncio
async def test_daily_login_tracking_and_bonus(sample_player: Player) -> None:
    """Test total_login_days progression and login bonus gift generation across 04:00 JST."""
    set_current_player(sample_player.id, "token_login")
    system_service = SystemService()
    inv_service = PlayerInventoryService()
    ctx = DummyContext()

    session_factory = get_sessionmaker()

    # 1. Same-day login record should NOT increment total_login_days
    rec_res = await system_service.RecordLoginHistoryV1(
        system_pb2.SystemRecordLoginHistoryV1.Request(), ctx
    )
    assert rec_res.logged_in_at > 0

    status_res = await system_service.GetPlayerStatusV2(
        system_pb2.SystemGetPlayerStatusV2.Request(), ctx
    )
    assert status_res.total_login_days == 1

    # Check mailbox has no duplicate login bonus
    box_res = await inv_service.GetInventoriesV1(
        player_inventory_pb2.PlayerInventoryGetInventoriesV1.Request(), ctx
    )
    assert all(g.search_label != "LOGIN_BONUS" for g in box_res.inventories)

    # 2. Simulate login 24 hours later (crossing 04:00 JST)
    future_ts = int(time.time()) + 86400 + 3600
    async with session_factory() as session:
        player = await session.get(Player, sample_player.id)
        assert player is not None
        is_new_day = process_player_login(session, player, future_ts)
        await session.commit()

    assert is_new_day is True
    assert player.total_login_days == 2

    # Verify GetPlayerStatusV2 reports total_login_days == 2
    status_res2 = await system_service.GetPlayerStatusV2(
        system_pb2.SystemGetPlayerStatusV2.Request(), ctx
    )
    assert status_res2.total_login_days == 2

    # Verify mailbox received 42 daily cultivation support mails (including 10,000 free Mitama stones)
    box_res2 = await inv_service.GetInventoriesV1(
        player_inventory_pb2.PlayerInventoryGetInventoriesV1.Request(), ctx
    )
    new_login_gifts = [g for g in box_res2.inventories if "【Reborn 育成支援・2日目】" in g.message]
    assert len(new_login_gifts) == 42

    exp_gift = next(g for g in new_login_gifts if g.system_resource_name == "Growth_30005")
    assert exp_gift.system_resource_num == 500

    ticket_gift = next(g for g in new_login_gifts if g.system_resource_name == "GenericItem_60021")
    assert ticket_gift.system_resource_num == 300

    hakoniwa_gift = next(
        g for g in new_login_gifts if g.system_resource_name == "HakoniwaMaterial_50001"
    )
    assert hakoniwa_gift.system_resource_num == 200

    boost_gift = next(g for g in new_login_gifts if g.system_resource_name == "BoostHeal_40002")
    assert boost_gift.system_resource_num == 10

    fragment_gift = next(
        g for g in new_login_gifts if g.system_resource_name == "HeroineFragment_20000"
    )
    # Matches growth.create_daily_reward_mail_items(universal_fragment_count=...).
    assert fragment_gift.system_resource_num == 10000

    coin_gift = next(g for g in new_login_gifts if g.system_resource_name == "Coin_10001")
    assert coin_gift.system_resource_num == 1000000

    stone_gift = next(g for g in new_login_gifts if g.system_resource_name == "FREE_STONE")
    assert stone_gift.system_resource_num == 10000

    wallet_service = WalletService()
    bal_before = await wallet_service.GetBalancesV2(wallet_pb2.WalletGetBalancesV2.Request(), ctx)
    vc_before = {vc.virtual_currency_name: vc.amount for vc in bal_before.total.virtual_currencies}
    free_before = vc_before.get("FREE_STONE", 0)

    # Claim all daily rewards
    claim_req = player_inventory_pb2.PlayerInventoryReceiveV1.Request(
        delete_inventory_ids=[g.id for g in new_login_gifts]
    )
    await inv_service.ReceiveV1(claim_req, ctx)

    bal_after = await wallet_service.GetBalancesV2(wallet_pb2.WalletGetBalancesV2.Request(), ctx)
    vc_after = {vc.virtual_currency_name: vc.amount for vc in bal_after.total.virtual_currencies}
    assert vc_after.get("FREE_STONE", 0) == free_before + 10000

    # Verify storage has growth (500), hakoniwa (200), boost heal (10), and universal heroine fragment (200) persisted
    from dankagu.core.growth import decode_growth_item

    async with session_factory() as session:
        exp_entry = await session.execute(
            select(PlayerStorageEntry).where(
                PlayerStorageEntry.player_id == sample_player.id,
                PlayerStorageEntry.key == "GrowthItemStock--30005",
            )
        )
        row = exp_entry.scalars().first()
        assert row is not None
        _, count = decode_growth_item(row.value)
        assert count == 500

        break_entry = await session.execute(
            select(PlayerStorageEntry).where(
                PlayerStorageEntry.player_id == sample_player.id,
                PlayerStorageEntry.key == "GrowthItemStock--31001",
            )
        )
        row_b = break_entry.scalars().first()
        assert row_b is not None
        _, count_b = decode_growth_item(row_b.value)
        assert count_b == 500

        hako_entry = await session.execute(
            select(PlayerStorageEntry).where(
                PlayerStorageEntry.player_id == sample_player.id,
                PlayerStorageEntry.key == "ItemStockStatus--14050001",
            )
        )
        row_h = hako_entry.scalars().first()
        assert row_h is not None
        _, count_h = decode_growth_item(row_h.value)
        assert count_h == 200

        frag_entry = await session.execute(
            select(PlayerStorageEntry).where(
                PlayerStorageEntry.player_id == sample_player.id,
                PlayerStorageEntry.key == "ItemStockStatus--11020000",
            )
        )
        row_f = frag_entry.scalars().first()
        assert row_f is not None
        _, count_f = decode_growth_item(row_f.value)
        # Same source as the mail count above.
        assert count_f == 10000
