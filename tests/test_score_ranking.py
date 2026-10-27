"""Unit tests for ScoreRanking service (Grand Prix and Class rankings)."""

import pytest
from player_factory import ensure_player

from dankagu.core.session import reset_current_player, set_current_player
from dankagu.grpc.generated.takasho.schema.fes.player_api import score_ranking_pb2
from dankagu.services.ranking import ScoreRankingService


class MockContext:
    pass


@pytest.mark.asyncio
async def test_score_ranking_lifecycle_and_batch_queries() -> None:
    pid = "test-gpx-player-1"
    await ensure_player(pid)
    tokens = set_current_player(pid)
    service = ScoreRankingService()
    ctx = MockContext()

    # 1. EntryV1
    entry_req = score_ranking_pb2.ScoreRankingEntryV1.Request()
    entry_res = await service.EntryV1(entry_req, ctx)  # type: ignore[arg-type]
    assert entry_res.player_class.player_id == pid
    assert entry_res.player_class.class_id in ("1", "2", "3", "4")

    # 2. Register score
    reg_req = score_ranking_pb2.ScoreRankingRegisterV1.Request(score=2500000)
    reg_res = await service.RegisterV1(reg_req, ctx)  # type: ignore[arg-type]
    assert reg_res.score == 2500000
    assert reg_res.rank >= 1

    # 3. Leaderboard query
    lb_req = score_ranking_pb2.ScoreRankingGetGrandPrixLeaderBoardV1.Request(start=1, count=10)
    lb_res = await service.GetGrandPrixLeaderBoardV1(lb_req, ctx)  # type: ignore[arg-type]
    assert len(lb_res.player_rank_infos) >= 1
    found = any(p.player_id == pid for p in lb_res.player_rank_infos)
    assert found

    # 4. Probing past end (fast cache path)
    probe_req = score_ranking_pb2.ScoreRankingGetGrandPrixLeaderBoardV1.Request(start=100000, count=1)
    probe_res = await service.GetGrandPrixLeaderBoardV1(probe_req, ctx)  # type: ignore[arg-type]
    assert len(probe_res.player_rank_infos) == 0

    # 5. Batch rank query
    rank_req = score_ranking_pb2.ScoreRankingGetGrandPrixRankV1.Request(
        player_ids=[pid, "nonexistent-pid"]
    )
    rank_res = await service.GetGrandPrixRankV1(rank_req, ctx)  # type: ignore[arg-type]
    assert len(rank_res.player_grand_prix_rank_infos) == 2
    my_info = next(p for p in rank_res.player_grand_prix_rank_infos if p.player_id == pid)
    assert my_info.point == 2500000 // 100
    assert my_info.rank >= 1

    # 6. Batch class query
    class_req = score_ranking_pb2.ScoreRankingGetClassV1.Request(
        player_ids=[pid, "nonexistent-pid"], period_id="1005"
    )
    class_res = await service.GetClassV1(class_req, ctx)  # type: ignore[arg-type]
    assert len(class_res.player_classes) == 2
    my_cls = next(p for p in class_res.player_classes if p.player_id == pid)
    assert my_cls.score == 2500000

    reset_current_player(tokens)
