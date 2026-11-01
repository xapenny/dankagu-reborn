"""Takasho Ranking servicers (RegularRanking, ScoreRanking)."""

import functools
import logging
import time

import grpc
from sqlalchemy import func, or_, select

from dankagu.core.database import get_sessionmaker
from dankagu.core.ranking_rewards import (
    check_and_grant_event_ranking_milestones,
    check_and_grant_gpx_milestones,
    check_and_grant_gpx_milestones_for_player,
)
from dankagu.core.session import get_current_player_id
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    ranking_pb2,
    ranking_pb2_grpc,
    regular_ranking_pb2,
    regular_ranking_pb2_grpc,
    score_ranking_pb2,
    score_ranking_pb2_grpc,
)
from dankagu.grpc.generated.takasho.schema.fes.resource.ranking import (
    v1_pb2 as ranking_resource_pb2,
)
from dankagu.models.player import Player
from dankagu.models.ranking import EventLeaderboard, SongLeaderboard
from dankagu.models.storage import PlayerStorageEntry
from dankagu.services.box_loot_box import encode_raw_protobuf

logger = logging.getLogger("dankagu.services.ranking")


class RankingService(ranking_pb2_grpc.RankingServicer):
    """Servicer for event point and highscore rankings (takasho.schema.fes.player_api.Ranking)."""

    async def GetAvailableEventV1(
        self,
        request: ranking_pb2.RankingGetAvailableEventV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> ranking_pb2.RankingGetAvailableEventV1.Response:
        logger.info("🏅 [Ranking] GetAvailableEventV1")
        response = ranking_pb2.RankingGetAvailableEventV1.Response()
        # Active events in DanKagu (38 Medley, 39 Points, 40 Mission, 41 Raid)
        for event_id in ["38", "39", "40", "41"]:
            ev = response.ranking_events.add()
            ev.id = event_id
            ev.opened_at = 1590000000
            ev.finished_at = 4102498799
            ev.closed_at = 4102498799
            ev.status = ranking_resource_pb2.RankingEvent.Status.OPEN
        return response

    async def RegisterV1(
        self,
        request: ranking_pb2.RankingRegisterV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> ranking_pb2.RankingRegisterV1.Response:
        player_id = get_current_player_id() or "dankagu-reborn-user"
        now = int(time.time())
        logger.info(
            "🏅 [Ranking] RegisterV1 (player=%s, event=%s, score=%d)",
            player_id,
            request.ranking_event_id,
            request.score,
        )
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(EventLeaderboard).where(
                EventLeaderboard.ranking_event_id == request.ranking_event_id,
                EventLeaderboard.player_id == player_id,
            )
            res = await session.execute(stmt)
            entry = res.scalars().first()

            if entry is None:
                entry = EventLeaderboard(
                    ranking_event_id=request.ranking_event_id,
                    player_id=player_id,
                    score=request.score,
                    updated_at=now,
                )
                session.add(entry)
                score_val = request.score
            else:
                if request.score > entry.score or "_point" in request.ranking_event_id:
                    entry.score = request.score
                    entry.updated_at = now
                score_val = entry.score
            await session.commit()

            # Calculate rank and check for milestone rewards
            rank_stmt = (
                select(func.count())
                .select_from(EventLeaderboard)
                .where(
                    EventLeaderboard.ranking_event_id == request.ranking_event_id,
                    EventLeaderboard.score > score_val,
                )
            )
            higher_count = (await session.execute(rank_stmt)).scalar() or 0
            rank = higher_count + 1
            granted = await check_and_grant_event_ranking_milestones(
                session, player_id, request.ranking_event_id, rank
            )
            if granted > 0:
                await session.commit()
                logger.info(
                    "🎁 [Ranking] Granted %d milestone reward(s) to %s (rank %d in %s)",
                    granted,
                    player_id,
                    rank,
                    request.ranking_event_id,
                )

        response = ranking_pb2.RankingRegisterV1.Response()
        response.score = score_val
        return response

    async def GetMyRankV1(
        self,
        request: ranking_pb2.RankingGetMyRankV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> ranking_pb2.RankingGetMyRankV1.Response:
        player_id = get_current_player_id() or "dankagu-reborn-user"
        logger.info(
            "🏅 [Ranking] GetMyRankV1 (player=%s, event=%s)",
            player_id,
            request.ranking_event_id,
        )
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(EventLeaderboard).where(
                EventLeaderboard.ranking_event_id == request.ranking_event_id,
                EventLeaderboard.player_id == player_id,
            )
            res = await session.execute(stmt)
            entry = res.scalars().first()

            response = ranking_pb2.RankingGetMyRankV1.Response()
            if entry is None:
                response.is_registered = False
                response.score = 0
                response.rank = 0
            else:
                rank_stmt = (
                    select(func.count())
                    .select_from(EventLeaderboard)
                    .where(
                        EventLeaderboard.ranking_event_id == request.ranking_event_id,
                        EventLeaderboard.score > entry.score,
                    )
                )
                higher_count = (await session.execute(rank_stmt)).scalar() or 0
                rank = higher_count + 1
                response.is_registered = True
                response.score = entry.score
                response.rank = rank

                granted = await check_and_grant_event_ranking_milestones(
                    session, player_id, request.ranking_event_id, rank
                )
                if granted > 0:
                    await session.commit()
                    logger.info(
                        "🎁 [Ranking] GetMyRankV1 granted %d milestone reward(s) to %s (rank %d)",
                        granted,
                        player_id,
                        rank,
                    )
            return response

    async def GetRankInfosV1(
        self,
        request: ranking_pb2.RankingGetRankInfosV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> ranking_pb2.RankingGetRankInfosV1.Response:
        logger.info(
            "🏅 [Ranking] GetRankInfosV1 (event=%s, start=%d, count=%d)",
            request.ranking_event_id,
            request.start,
            request.count,
        )
        response = ranking_pb2.RankingGetRankInfosV1.Response()
        start = max(1, request.start)
        count = request.count if request.count > 0 else 100
        offset = start - 1

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = (
                select(EventLeaderboard, Player.nickname, Player.name)
                .outerjoin(Player, EventLeaderboard.player_id == Player.id)
                .where(EventLeaderboard.ranking_event_id == request.ranking_event_id)
                .order_by(EventLeaderboard.score.desc(), EventLeaderboard.updated_at.asc())
                .offset(offset)
                .limit(count)
            )
            res = await session.execute(stmt)
            rows = res.all()

        for i, (leaderboard, p_nickname, p_name) in enumerate(rows):
            item = response.player_rank_infos.add()
            item.ranking_event_id = leaderboard.ranking_event_id
            item.player_id = leaderboard.player_id
            item.nickname = p_nickname or p_name or "DanKagu"
            item.rank = start + i
            item.score = leaderboard.score

        return response

    async def GetRankInfosAroundMeV1(
        self,
        request: ranking_pb2.RankingGetRankInfosAroundMeV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> ranking_pb2.RankingGetRankInfosAroundMeV1.Response:
        player_id = get_current_player_id() or "dankagu-reborn-user"
        logger.info(
            "🏅 [Ranking] GetRankInfosAroundMeV1 (player=%s, event=%s, count=%d)",
            player_id,
            request.ranking_event_id,
            request.count,
        )
        response = ranking_pb2.RankingGetRankInfosAroundMeV1.Response()
        count = request.count if request.count > 0 else 20
        half = count // 2

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(EventLeaderboard).where(
                EventLeaderboard.ranking_event_id == request.ranking_event_id,
                EventLeaderboard.player_id == player_id,
            )
            res = await session.execute(stmt)
            entry = res.scalars().first()

            if entry is None:
                start_rank = 1
            else:
                rank_stmt = (
                    select(func.count())
                    .select_from(EventLeaderboard)
                    .where(
                        EventLeaderboard.ranking_event_id == request.ranking_event_id,
                        EventLeaderboard.score > entry.score,
                    )
                )
                my_rank = (await session.execute(rank_stmt)).scalar() or 0
                start_rank = max(1, (my_rank + 1) - half)

            offset = start_rank - 1
            fetch_stmt = (
                select(EventLeaderboard, Player.nickname, Player.name)
                .outerjoin(Player, EventLeaderboard.player_id == Player.id)
                .where(EventLeaderboard.ranking_event_id == request.ranking_event_id)
                .order_by(EventLeaderboard.score.desc(), EventLeaderboard.updated_at.asc())
                .offset(offset)
                .limit(count)
            )
            res = await session.execute(fetch_stmt)
            rows = res.all()

        for i, (leaderboard, p_nickname, p_name) in enumerate(rows):
            item = response.player_rank_infos.add()
            item.ranking_event_id = leaderboard.ranking_event_id
            item.player_id = leaderboard.player_id
            item.nickname = p_nickname or p_name or "DanKagu"
            item.rank = start_rank + i
            item.score = leaderboard.score

        return response

    async def GetMyCommittedRankV1(
        self,
        request: ranking_pb2.RankingGetMyCommittedRankV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> ranking_pb2.RankingGetMyCommittedRankV1.Response:
        player_id = get_current_player_id() or "dankagu-reborn-user"
        logger.info(
            "🏅 [Ranking] GetMyCommittedRankV1 (player=%s, event=%s)",
            player_id,
            request.ranking_event_id,
        )
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(EventLeaderboard).where(
                EventLeaderboard.ranking_event_id == request.ranking_event_id,
                EventLeaderboard.player_id == player_id,
            )
            res = await session.execute(stmt)
            entry = res.scalars().first()

            response = ranking_pb2.RankingGetMyCommittedRankV1.Response()
            if entry is None:
                response.is_committed = False
                response.score = 0
                response.rank = 0
            else:
                rank_stmt = (
                    select(func.count())
                    .select_from(EventLeaderboard)
                    .where(
                        EventLeaderboard.ranking_event_id == request.ranking_event_id,
                        EventLeaderboard.score > entry.score,
                    )
                )
                higher_count = (await session.execute(rank_stmt)).scalar() or 0
                response.is_committed = True
                response.score = entry.score
                response.rank = higher_count + 1
            return response

    async def GetTotalRegisteredCount(
        self,
        request: ranking_pb2.RankingGetTotalRegisteredCountV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> ranking_pb2.RankingGetTotalRegisteredCountV1.Response:
        logger.info(
            "🏅 [Ranking] GetTotalRegisteredCount (event=%s)",
            request.ranking_event_id,
        )
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = (
                select(func.count())
                .select_from(EventLeaderboard)
                .where(EventLeaderboard.ranking_event_id == request.ranking_event_id)
            )
            total = (await session.execute(stmt)).scalar() or 0
        response = ranking_pb2.RankingGetTotalRegisteredCountV1.Response()
        response.count = total
        return response


class RegularRankingService(regular_ranking_pb2_grpc.RegularRankingServicer):
    """Servicer for regular score rankings."""

    async def RegisterV1(
        self,
        request: regular_ranking_pb2.RegularRankingRegisterV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> regular_ranking_pb2.RegularRankingRegisterV1.Response:
        player_id = get_current_player_id() or "dankagu-reborn-user"
        now = int(time.time())
        logger.info(
            "🏅 [RegularRanking] RegisterV1 (player=%s, key=%s, score=%d)",
            player_id,
            request.ranking_key,
            request.score,
        )

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # 1. Fetch player nickname
            player = await session.get(Player, player_id)
            nick_name = (player.nickname or player.name) if player else "DanKagu"

            # 2. Check existing score
            stmt = select(SongLeaderboard).where(
                SongLeaderboard.ranking_key == request.ranking_key,
                SongLeaderboard.player_id == player_id,
            )
            res = await session.execute(stmt)
            entry = res.scalars().first()

            if entry is None:
                entry = SongLeaderboard(
                    ranking_key=request.ranking_key,
                    player_id=player_id,
                    score=request.score,
                    updated_at=now,
                )
                session.add(entry)
                score_val = request.score
            else:
                if request.score > entry.score:
                    entry.score = request.score
                    entry.updated_at = now
                score_val = entry.score

            await session.commit()

            # 3. Calculate rank
            rank_stmt = (
                select(func.count())
                .select_from(SongLeaderboard)
                .where(
                    SongLeaderboard.ranking_key == request.ranking_key,
                    SongLeaderboard.score > score_val,
                )
            )
            higher_count = (await session.execute(rank_stmt)).scalar() or 0
            rank = higher_count + 1

        response = regular_ranking_pb2.RegularRankingRegisterV1.Response()
        player_ranking = response.player_ranking
        player_ranking.ranking_key = request.ranking_key
        player_ranking.player_id = player_id
        player_ranking.nick_name = nick_name
        player_ranking.rank = rank
        player_ranking.score = score_val
        return response

    async def GetTopRankingV1(
        self,
        request: regular_ranking_pb2.RegularRankingGetTopRankingV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> regular_ranking_pb2.RegularRankingGetTopRankingV1.Response:
        logger.info(
            "🏅 [RegularRanking] GetTopRankingV1 (key=%s, start=%d, count=%d)",
            request.ranking_key,
            request.start,
            request.count,
        )
        response = regular_ranking_pb2.RegularRankingGetTopRankingV1.Response()

        start = max(1, request.start)
        count = request.count if request.count > 0 else 100
        offset = start - 1

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = (
                select(SongLeaderboard, Player.nickname, Player.name)
                .outerjoin(Player, SongLeaderboard.player_id == Player.id)
                .where(SongLeaderboard.ranking_key == request.ranking_key)
                .order_by(SongLeaderboard.score.desc(), SongLeaderboard.updated_at.asc())
                .offset(offset)
                .limit(count)
            )
            res = await session.execute(stmt)
            rows = res.all()

        for i, (leaderboard, p_nickname, p_name) in enumerate(rows):
            item = response.player_rankings.add()
            item.ranking_key = leaderboard.ranking_key
            item.player_id = leaderboard.player_id
            item.nick_name = p_nickname or p_name or "DanKagu"
            item.rank = start + i
            item.score = leaderboard.score

        return response


class ScoreRankingService(score_ranking_pb2_grpc.ScoreRankingServicer):
    """Servicer for Danmaku Grand Prix and Class score rankings with real database persistence."""

    DEFAULT_GROUP_ID = "d5c0c1906e8e91ec6e027edfecb9eca9"
    DEFAULT_STORAGE_KEYS = ("PlayerBase--1", "PlayerStatus--1", "PlayerPublicStatus--1")

    # Fast in-memory caches to eliminate redundant roundtrips to remote PostgreSQL
    _known_player_ids: set[str] = set()
    _participant_counts: dict[str, tuple[int, float]] = {}
    _COUNT_CACHE_TTL = 15.0  # seconds

    _EXP_BREAKPOINTS = (
        (1, 5),
        (2, 55),
        (3, 60),
        (4, 65),
        (5, 70),
        (6, 75),
        (7, 80),
        (8, 85),
        (9, 90),
        (10, 95),
        (11, 100),
        (12, 110),
        (13, 120),
        (14, 130),
        (15, 140),
        (16, 150),
        (17, 160),
        (18, 170),
        (19, 200),
        (29, 300),
        (39, 400),
        (49, 500),
        (59, 600),
        (69, 700),
        (79, 800),
        (89, 900),
        (99, 1000),
        (108, 1030),
        (124, 1060),
        (139, 1090),
        (155, 1120),
        (170, 1150),
        (186, 1180),
        (201, 1210),
        (216, 1240),
        (231, 1270),
        (246, 1300),
        (261, 1400),
        (276, 1480),
        (291, 1560),
        (305, 1650),
        (320, 1740),
        (335, 1820),
        (349, 1910),
        (364, 2000),
        (378, 2080),
        (393, 2170),
        (407, 2260),
        (422, 2350),
        (436, 2430),
        (451, 2520),
        (465, 2610),
        (479, 2690),
        (493, 2780),
    )

    @classmethod
    @functools.lru_cache(maxsize=1024)
    def _calc_exp_for_level(cls, level: int) -> int:
        """Calculate cumulative player exp required for a given level (Master PlayerExp compliant)."""
        if level <= 1:
            return 0
        bps = cls._EXP_BREAKPOINTS

        def step_at(lv: int) -> int:
            low, high = 0, len(bps) - 1
            while low <= high:
                mid = (low + high) // 2
                if bps[mid][0] <= lv:
                    low = mid + 1
                else:
                    high = mid - 1
            return bps[high][1]

        total = sum(step_at(lv) for lv in range(1, min(level, 500)))
        if level > 500:
            total += (level - 500) * 2780
        return total

    @classmethod
    def _synthesize_player_status(cls, level: int) -> bytes:
        exp = cls._calc_exp_for_level(level)
        return encode_raw_protobuf(
            {
                1: [(0, 1)],
                4: [(0, exp)],
            }
        )

    @classmethod
    def _synthesize_player_base(cls, nickname: str) -> bytes:
        return encode_raw_protobuf(
            {
                1: [(0, 1)],
                3: [(2, nickname.encode("utf-8"))],
            }
        )

    async def _attach_leaderboard_data(
        self,
        session,
        rows: list,
        start_rank: int,
        target_container,
        is_grand_prix: bool = True,
    ) -> None:
        """Batch-load PlayerStorage entries and GPX class IDs in a single query, populating rank infos."""
        if not rows:
            return

        player_ids = [entry.player_id for entry, _, _, _ in rows]

        # Single batched SQL query to get both DEFAULT_STORAGE_KEYS and GpxInfoStatus%
        stmt = select(PlayerStorageEntry).where(
            PlayerStorageEntry.player_id.in_(player_ids),
            or_(
                PlayerStorageEntry.key.in_(self.DEFAULT_STORAGE_KEYS),
                PlayerStorageEntry.key.like("GpxInfoStatus%"),
            ),
        )
        storage_rows = (await session.execute(stmt)).scalars().all()

        storage_by_player: dict[str, dict[str, PlayerStorageEntry]] = {}
        class_by_player: dict[str, str] = {}

        for s_entry in storage_rows:
            if s_entry.key.startswith("GpxInfoStatus"):
                cid = self._extract_last_class_id(s_entry.value)
                if cid is not None and 1 <= cid <= 4:
                    class_by_player[s_entry.player_id] = str(cid)
            else:
                storage_by_player.setdefault(s_entry.player_id, {})[s_entry.key] = s_entry

        now = int(time.time())
        for i, (entry, nickname, name, level) in enumerate(rows, start=start_rank):
            info = target_container.add()
            info.player_id = entry.player_id
            info.nickname = nickname or name or "Player"
            info.level = level or 1
            info.rank = i

            if is_grand_prix:
                info.point = entry.score // 100
            else:
                info.score = entry.score

            # Resolve class_id
            cid = class_by_player.get(entry.player_id)
            if not cid:
                cid = "4" if entry.score > 0 else "0"
            info.class_id = cid if cid != "0" else "4"

            # Attach PlayerStorage entries
            p_entries = storage_by_player.get(entry.player_id, {})
            player_name = nickname or name or "Player"
            player_level = level or 1

            # 1. PlayerBase--1
            if "PlayerBase--1" in p_entries:
                base_s = p_entries["PlayerBase--1"]
                pe = info.player_storage_entries.add()
                pe.player_id = base_s.player_id
                pe.key = base_s.key
                pe.value = base_s.value
                pe.created_at = base_s.created_at
                pe.updated_at = base_s.updated_at
            else:
                pe = info.player_storage_entries.add()
                pe.player_id = entry.player_id
                pe.key = "PlayerBase--1"
                pe.value = self._synthesize_player_base(player_name)
                pe.created_at = now
                pe.updated_at = now

            # 2. PlayerStatus--1
            if "PlayerStatus--1" in p_entries:
                status_s = p_entries["PlayerStatus--1"]
                pe = info.player_storage_entries.add()
                pe.player_id = status_s.player_id
                pe.key = status_s.key
                pe.value = status_s.value
                pe.created_at = status_s.created_at
                pe.updated_at = status_s.updated_at
            else:
                pe = info.player_storage_entries.add()
                pe.player_id = entry.player_id
                pe.key = "PlayerStatus--1"
                pe.value = self._synthesize_player_status(player_level)
                pe.created_at = now
                pe.updated_at = now

            # 3. PlayerPublicStatus--1 (if present in DB)
            if "PlayerPublicStatus--1" in p_entries:
                pub_s = p_entries["PlayerPublicStatus--1"]
                pe = info.player_storage_entries.add()
                pe.player_id = pub_s.player_id
                pe.key = pub_s.key
                pe.value = pub_s.value
                pe.created_at = pub_s.created_at
                pe.updated_at = pub_s.updated_at

    async def _attach_leaderboard_storage_entries(
        self,
        session,
        rows: list,
        infos: list,
    ) -> None:
        """Batch-load and attach PlayerStorage entries for leaderboard entries (legacy adapter)."""
        if not rows:
            return

        player_ids = [entry.player_id for entry, _, _, _ in rows]
        stmt = select(PlayerStorageEntry).where(
            PlayerStorageEntry.player_id.in_(player_ids),
            PlayerStorageEntry.key.in_(self.DEFAULT_STORAGE_KEYS),
        )
        storage_rows = (await session.execute(stmt)).scalars().all()

        storage_by_player: dict[str, dict[str, PlayerStorageEntry]] = {}
        for s_entry in storage_rows:
            storage_by_player.setdefault(s_entry.player_id, {})[s_entry.key] = s_entry

        now = int(time.time())
        for (entry, nickname, name, level), info in zip(rows, infos, strict=False):
            p_entries = storage_by_player.get(entry.player_id, {})
            player_name = nickname or name or "Player"
            player_level = level or 1

            if "PlayerBase--1" in p_entries:
                base_s = p_entries["PlayerBase--1"]
                pe = info.player_storage_entries.add()
                pe.player_id = base_s.player_id
                pe.key = base_s.key
                pe.value = base_s.value
                pe.created_at = base_s.created_at
                pe.updated_at = base_s.updated_at
            else:
                pe = info.player_storage_entries.add()
                pe.player_id = entry.player_id
                pe.key = "PlayerBase--1"
                pe.value = self._synthesize_player_base(player_name)
                pe.created_at = now
                pe.updated_at = now

            if "PlayerStatus--1" in p_entries:
                status_s = p_entries["PlayerStatus--1"]
                pe = info.player_storage_entries.add()
                pe.player_id = status_s.player_id
                pe.key = status_s.key
                pe.value = status_s.value
                pe.created_at = status_s.created_at
                pe.updated_at = status_s.updated_at
            else:
                pe = info.player_storage_entries.add()
                pe.player_id = entry.player_id
                pe.key = "PlayerStatus--1"
                pe.value = self._synthesize_player_status(player_level)
                pe.created_at = now
                pe.updated_at = now

            if "PlayerPublicStatus--1" in p_entries:
                pub_s = p_entries["PlayerPublicStatus--1"]
                pe = info.player_storage_entries.add()
                pe.player_id = pub_s.player_id
                pe.key = pub_s.key
                pe.value = pub_s.value
                pe.created_at = pub_s.created_at
                pe.updated_at = pub_s.updated_at

    @staticmethod
    def _extract_last_class_id(val: bytes) -> int | None:
        """Extract lastClassID (field 6) from GpxInfoStatusProto bytes."""
        try:
            i = 0
            n = len(val)
            while i < n:
                tag = 0
                shift = 0
                while i < n:
                    b = val[i]
                    i += 1
                    tag |= (b & 0x7F) << shift
                    shift += 7
                    if not (b & 0x80):
                        break
                field_num = tag >> 3
                wire_type = tag & 7
                if wire_type == 0:  # Varint
                    v = 0
                    shift = 0
                    while i < n:
                        b = val[i]
                        i += 1
                        v |= (b & 0x7F) << shift
                        shift += 7
                        if not (b & 0x80):
                            break
                    if field_num == 6:
                        return v
                elif wire_type == 2:  # Length-delimited
                    length = 0
                    shift = 0
                    while i < n:
                        b = val[i]
                        i += 1
                        length |= (b & 0x7F) << shift
                        shift += 7
                        if not (b & 0x80):
                            break
                    i += length
                elif wire_type == 1:  # 64-bit
                    i += 8
                elif wire_type == 5:  # 32-bit
                    i += 4
                else:
                    break
        except Exception:
            pass
        return None

    async def _get_player_class_id(self, session, player_id: str, score: int) -> str:
        """Fetch player's GPX class_id ("1"=Platinum, "2"=Gold, "3"=Silver, "4"=Bronze, "0"=None)."""
        stmt = select(PlayerStorageEntry.value).where(
            PlayerStorageEntry.player_id == player_id,
            PlayerStorageEntry.key.like("GpxInfoStatus%"),
        )
        val = (await session.execute(stmt)).scalars().first()
        if val:
            last_class_id = self._extract_last_class_id(val)
            if last_class_id is not None and 1 <= last_class_id <= 4:
                return str(last_class_id)

        # If no GpxInfoStatus saved, check if player has participated in GPX
        if score > 0:
            return "4"  # Default initial tier Bronze

        return "0"  # NONE (no frame)

    async def _resolve_player_id(self, session, player_id: str | None) -> str:
        """Resolve player_id ensuring it exists in the players table to satisfy FK constraints."""
        if player_id:
            if player_id in self._known_player_ids:
                return player_id
            exists_stmt = select(Player.id).where(Player.id == player_id)
            if (await session.execute(exists_stmt)).scalar() is not None:
                self._known_player_ids.add(player_id)
                return player_id

        # Fall back to an existing player in DB
        fallback_stmt = select(Player.id).limit(1)
        fallback_id = (await session.execute(fallback_stmt)).scalar()
        if fallback_id:
            self._known_player_ids.add(fallback_id)
            return fallback_id

        return player_id or "dankagu-reborn-user"

    async def _get_total_participants(self, session, ranking_key: str) -> int:
        now = time.monotonic()
        cached = self._participant_counts.get(ranking_key)
        if cached and cached[1] > now:
            return cached[0]

        count_stmt = (
            select(func.count())
            .select_from(EventLeaderboard)
            .where(
                EventLeaderboard.ranking_event_id == ranking_key,
                EventLeaderboard.score > 0,
            )
        )
        total = (await session.execute(count_stmt)).scalar() or 0
        self._participant_counts[ranking_key] = (total, now + self._COUNT_CACHE_TTL)
        return total

    async def _get_player_gpx_score(
        self, session, player_id: str, period_id: str = "1005"
    ) -> tuple[int, int]:
        """Fetch player's recorded highscore and rank for the given period.

        Returns (score, rank).
        """
        ranking_key = f"gpx_s1_p{period_id}"
        stmt = select(EventLeaderboard).where(
            EventLeaderboard.ranking_event_id == ranking_key,
            EventLeaderboard.player_id == player_id,
        )
        entry = (await session.execute(stmt)).scalars().first()
        if not entry or entry.score <= 0:
            return 0, 0

        higher_stmt = (
            select(func.count())
            .select_from(EventLeaderboard)
            .where(
                EventLeaderboard.ranking_event_id == ranking_key,
                EventLeaderboard.score > entry.score,
            )
        )
        higher_count = (await session.execute(higher_stmt)).scalar() or 0
        return entry.score, higher_count + 1

    async def _batch_get_gpx_scores_and_ranks(
        self, session, player_ids: list[str], ranking_key: str
    ) -> dict[str, tuple[int, int]]:
        """Batch-fetch (score, rank) for a list of player_ids."""
        if not player_ids:
            return {}

        unique_pids = list(dict.fromkeys(player_ids))
        stmt = select(EventLeaderboard.player_id, EventLeaderboard.score).where(
            EventLeaderboard.ranking_event_id == ranking_key,
            EventLeaderboard.player_id.in_(unique_pids),
        )
        score_rows = (await session.execute(stmt)).all()
        scores_map = dict(score_rows)

        result: dict[str, tuple[int, int]] = {}
        for pid in unique_pids:
            score = scores_map.get(pid, 0)
            if score <= 0:
                result[pid] = (0, 0)
            else:
                higher_stmt = (
                    select(func.count())
                    .select_from(EventLeaderboard)
                    .where(
                        EventLeaderboard.ranking_event_id == ranking_key,
                        EventLeaderboard.score > score,
                    )
                )
                rank = ((await session.execute(higher_stmt)).scalar() or 0) + 1
                result[pid] = (score, rank)

        return result

    async def _batch_get_player_class_ids(
        self, session, player_ids: list[str], scores_map: dict[str, int]
    ) -> dict[str, str]:
        """Batch-fetch GPX class IDs for player_ids."""
        if not player_ids:
            return {}

        unique_pids = list(dict.fromkeys(player_ids))
        stmt = select(PlayerStorageEntry.player_id, PlayerStorageEntry.value).where(
            PlayerStorageEntry.player_id.in_(unique_pids),
            PlayerStorageEntry.key.like("GpxInfoStatus%"),
        )
        storage_rows = (await session.execute(stmt)).all()

        class_map: dict[str, str] = {}
        for pid, val in storage_rows:
            last_class_id = self._extract_last_class_id(val)
            if last_class_id is not None and 1 <= last_class_id <= 4:
                class_map[pid] = str(last_class_id)

        result: dict[str, str] = {}
        for pid in unique_pids:
            if pid in class_map:
                result[pid] = class_map[pid]
            elif scores_map.get(pid, 0) > 0:
                result[pid] = "4"
            else:
                result[pid] = "0"
        return result

    async def GetAvailableV1(
        self,
        request: score_ranking_pb2.ScoreRankingGetAvailableV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingGetAvailableV1.Response:
        logger.info("🏅 [ScoreRanking] GetAvailableV1")
        response = score_ranking_pb2.ScoreRankingGetAvailableV1.Response()
        season = response.season
        season.season_id = "1"
        season.opened_at = 1659074400
        season.finished_at = 3390000000
        season.closed_at = 3400000000
        season.entry_opened_at = 1659074400

        period = season.period
        period.period_id = "1005"
        period.opened_at = 1664604000
        period.finished_at = 3390000000
        period.closed_at = 3400000000
        return response

    async def EntryV1(
        self,
        request: score_ranking_pb2.ScoreRankingEntryV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingEntryV1.Response:
        logger.info("🏅 [ScoreRanking] EntryV1")
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            current_pid = await self._resolve_player_id(session, get_current_player_id())
            score, _ = await self._get_player_gpx_score(session, current_pid, "1005")
            class_id = await self._get_player_class_id(session, current_pid, score)
            if class_id == "0":
                class_id = "4"

        response = score_ranking_pb2.ScoreRankingEntryV1.Response()
        response.player_class.player_id = current_pid
        response.player_class.class_id = class_id
        response.player_class.group_id = self.DEFAULT_GROUP_ID
        response.player_class.score = score
        return response

    async def GetGrandPrixLeaderBoardV1(
        self,
        request: score_ranking_pb2.ScoreRankingGetGrandPrixLeaderBoardV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingGetGrandPrixLeaderBoardV1.Response:
        logger.info(
            "🏅 [ScoreRanking] GetGrandPrixLeaderBoardV1: start=%d count=%d",
            request.start,
            request.count,
        )
        response = score_ranking_pb2.ScoreRankingGetGrandPrixLeaderBoardV1.Response()
        response.is_during_aggregation = False

        start = max(1, request.start)
        count = request.count if request.count > 0 else 50
        offset = start - 1
        ranking_key = "gpx_s1_p1005"

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # Fast check: If offset exceeds total registered participants, return immediately
            total = await self._get_total_participants(session, ranking_key)
            if offset >= total:
                return response

            stmt = (
                select(EventLeaderboard, Player.nickname, Player.name, Player.level)
                .outerjoin(Player, EventLeaderboard.player_id == Player.id)
                .where(
                    EventLeaderboard.ranking_event_id == ranking_key,
                    EventLeaderboard.score > 0,
                )
                .order_by(EventLeaderboard.score.desc(), EventLeaderboard.updated_at.asc())
                .offset(offset)
                .limit(count)
            )
            rows = (await session.execute(stmt)).all()
            if not rows:
                return response

            # Unified batched storage and class_id fetch in ONE query
            await self._attach_leaderboard_data(
                session, rows, start, response.player_rank_infos, is_grand_prix=True
            )

        return response

    async def GetClassGroupLeaderBoardV1(
        self,
        request: score_ranking_pb2.ScoreRankingGetClassGroupLeaderBoardV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingGetClassGroupLeaderBoardV1.Response:
        logger.info(
            "🏅 [ScoreRanking] GetClassGroupLeaderBoardV1: start=%d count=%d",
            request.start,
            request.count,
        )
        response = score_ranking_pb2.ScoreRankingGetClassGroupLeaderBoardV1.Response()

        start = max(1, request.start)
        count = request.count if request.count > 0 else 50
        offset = start - 1
        ranking_key = "gpx_s1_p1005"

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            current_pid = await self._resolve_player_id(session, get_current_player_id())
            # Determine caller's class_id to filter the group leaderboard
            score, _ = await self._get_player_gpx_score(session, current_pid, "1005")
            caller_class_id = await self._get_player_class_id(session, current_pid, score)
            if caller_class_id == "0":
                caller_class_id = "4"

            stmt = (
                select(EventLeaderboard, Player.nickname, Player.name, Player.level)
                .outerjoin(Player, EventLeaderboard.player_id == Player.id)
                .where(
                    EventLeaderboard.ranking_event_id == ranking_key,
                    EventLeaderboard.score > 0,
                )
                .order_by(EventLeaderboard.score.desc(), EventLeaderboard.updated_at.asc())
            )
            all_rows = (await session.execute(stmt)).all()
            if not all_rows:
                return response

            all_pids = [entry.player_id for entry, _, _, _ in all_rows]
            scores_map = {entry.player_id: entry.score for entry, _, _, _ in all_rows}
            class_map = await self._batch_get_player_class_ids(session, all_pids, scores_map)

            # Filter rows to only players in caller's class
            class_rows = [
                row for row in all_rows if class_map.get(row[0].player_id, "4") == caller_class_id
            ]

            if offset >= len(class_rows):
                return response

            page_rows = class_rows[offset : offset + count]
            await self._attach_leaderboard_data(
                session, page_rows, start, response.player_rank_infos, is_grand_prix=False
            )

        return response

    async def GetGrandPrixRankV1(
        self,
        request: score_ranking_pb2.ScoreRankingGetGrandPrixRankV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingGetGrandPrixRankV1.Response:
        logger.info("🏅 [ScoreRanking] GetGrandPrixRankV1")
        response = score_ranking_pb2.ScoreRankingGetGrandPrixRankV1.Response()

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            current_pid = await self._resolve_player_id(session, get_current_player_id())
            target_pids = list(request.player_ids) or [current_pid]
            data = await self._batch_get_gpx_scores_and_ranks(session, target_pids, "gpx_s1_p1005")

            for pid in target_pids:
                score, rank = data.get(pid, (0, 0))
                info = response.player_grand_prix_rank_infos.add()
                info.player_id = pid
                info.rank = rank if score > 0 else 0
                info.point = score // 100 if score > 0 else 0

        return response

    async def GetClassGroupRankV1(
        self,
        request: score_ranking_pb2.ScoreRankingGetClassGroupRankV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingGetClassGroupRankV1.Response:
        period_id = request.period_id or "1005"
        logger.info("🏅 [ScoreRanking] GetClassGroupRankV1: period=%s", period_id)
        response = score_ranking_pb2.ScoreRankingGetClassGroupRankV1.Response()

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            current_pid = await self._resolve_player_id(session, get_current_player_id())
            target_pids = list(request.player_ids) or [current_pid]
            ranking_key = f"gpx_s1_p{period_id}"

            scores_and_ranks = await self._batch_get_gpx_scores_and_ranks(
                session, target_pids, ranking_key
            )
            scores_map = {pid: sr[0] for pid, sr in scores_and_ranks.items()}
            target_classes = await self._batch_get_player_class_ids(
                session, target_pids, scores_map
            )

            # Fetch all participants to compute accurate rank within the class
            all_stmt = (
                select(EventLeaderboard.player_id, EventLeaderboard.score)
                .where(
                    EventLeaderboard.ranking_event_id == ranking_key,
                    EventLeaderboard.score > 0,
                )
                .order_by(EventLeaderboard.score.desc(), EventLeaderboard.updated_at.asc())
            )
            all_participants = (await session.execute(all_stmt)).all()
            all_pids = [p[0] for p in all_participants]
            all_scores_map = dict(all_participants)
            all_classes = await self._batch_get_player_class_ids(session, all_pids, all_scores_map)

            for pid in target_pids:
                score = scores_map.get(pid, 0)
                cid = target_classes.get(pid, "4")
                if score <= 0:
                    rank = 0
                else:
                    higher_in_class = sum(
                        1
                        for other_pid, other_score in all_participants
                        if other_score > score and all_classes.get(other_pid, "4") == cid
                    )
                    class_rank = higher_in_class + 1
                    rank = min(class_rank, 20)

                info = response.player_class_group_rank_infos.add()
                info.player_id = pid
                info.rank = rank
                info.score = score

        return response

    async def GetClassV1(
        self,
        request: score_ranking_pb2.ScoreRankingGetClassV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingGetClassV1.Response:
        period_id = request.period_id or "1005"
        logger.info("🏅 [ScoreRanking] GetClassV1: period=%s", period_id)
        response = score_ranking_pb2.ScoreRankingGetClassV1.Response()

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            current_pid = await self._resolve_player_id(session, get_current_player_id())
            target_pids = list(request.player_ids) or [current_pid]
            ranking_key = f"gpx_s1_p{period_id}"

            unique_pids = list(dict.fromkeys(target_pids))
            stmt = select(EventLeaderboard.player_id, EventLeaderboard.score).where(
                EventLeaderboard.ranking_event_id == ranking_key,
                EventLeaderboard.player_id.in_(unique_pids),
            )
            score_rows = (await session.execute(stmt)).all()
            scores_map = dict(score_rows)

            class_map = await self._batch_get_player_class_ids(session, target_pids, scores_map)

            for pid in target_pids:
                score = scores_map.get(pid, 0)
                class_id = class_map.get(pid, "0")
                pclass = response.player_classes.add()
                pclass.player_id = pid
                pclass.class_id = class_id
                pclass.score = score
                pclass.group_id = self.DEFAULT_GROUP_ID

        return response

    async def GetClassGroupElevatingRankV1(
        self,
        request: score_ranking_pb2.ScoreRankingGetClassGroupElevatingRankV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingGetClassGroupElevatingRankV1.Response:
        logger.info(
            "🏅 [ScoreRanking] GetClassGroupElevatingRankV1: period=%s class=%s group=%s",
            request.period_id,
            request.class_id,
            request.group_id,
        )
        response = score_ranking_pb2.ScoreRankingGetClassGroupElevatingRankV1.Response()
        response.promotion_rank = 3
        response.demotion_rank = 15
        return response

    async def RegisterV1(
        self,
        request: score_ranking_pb2.ScoreRankingRegisterV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingRegisterV1.Response:
        now = int(time.time())
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            player_id = await self._resolve_player_id(session, get_current_player_id())
            logger.info(
                "🏅 [ScoreRanking] RegisterV1: player=%s, score=%d", player_id, request.score
            )
            ranking_key = "gpx_s1_p1005"
            stmt = select(EventLeaderboard).where(
                EventLeaderboard.ranking_event_id == ranking_key,
                EventLeaderboard.player_id == player_id,
            )
            entry = (await session.execute(stmt)).scalars().first()

            if entry is None:
                entry = EventLeaderboard(
                    ranking_event_id=ranking_key,
                    player_id=player_id,
                    score=request.score,
                    updated_at=now,
                )
                session.add(entry)
                final_score = request.score
            else:
                if request.score > entry.score:
                    entry.score = request.score
                    entry.updated_at = now
                final_score = entry.score
            await session.commit()

            # Invalidate cached participant count on score update
            self._participant_counts.pop(ranking_key, None)

            higher_stmt = (
                select(func.count())
                .select_from(EventLeaderboard)
                .where(
                    EventLeaderboard.ranking_event_id == ranking_key,
                    EventLeaderboard.score > final_score,
                )
            )
            higher_count = (await session.execute(higher_stmt)).scalar() or 0
            rank = higher_count + 1

            # Check and grant GPX class and group milestone rewards
            class_map = await self._batch_get_player_class_ids(
                session, [player_id], {player_id: final_score}
            )
            cid_str = class_map.get(player_id, "1")
            class_id = int(cid_str) if str(cid_str).isdigit() and int(cid_str) > 0 else 1
            group_rank = min(rank, 20) if rank > 0 else 1

            granted = await check_and_grant_gpx_milestones(
                session,
                player_id,
                grand_prix_rank=rank,
                group_rank=group_rank,
                class_id=class_id,
            )
            if granted > 0:
                await session.commit()
                logger.info(
                    "🎁 [ScoreRanking] Granted %d GPX milestone reward(s) to %s (rank %d, group %d, class %d)",
                    granted,
                    player_id,
                    rank,
                    group_rank,
                    class_id,
                )

        response = score_ranking_pb2.ScoreRankingRegisterV1.Response()
        response.score = final_score
        response.rank = rank
        return response

    async def ReceiveClassPrizeV1(
        self,
        request: score_ranking_pb2.ScoreRankingReceiveClassPrizeV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingReceiveClassPrizeV1.Response:
        logger.info("🏅 [ScoreRanking] ReceiveClassPrizeV1")
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            player_id = await self._resolve_player_id(session, get_current_player_id())
            ranking_key = "gpx_s1_p1005"
            granted = await check_and_grant_gpx_milestones_for_player(
                session, player_id=player_id, ranking_key=ranking_key
            )
            if granted > 0:
                await session.commit()
        return score_ranking_pb2.ScoreRankingReceiveClassPrizeV1.Response()

    async def ReceiveGrandPrixPrizeV1(
        self,
        request: score_ranking_pb2.ScoreRankingReceiveGrandPrixPrizeV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> score_ranking_pb2.ScoreRankingReceiveGrandPrixPrizeV1.Response:
        logger.info("🏅 [ScoreRanking] ReceiveGrandPrixPrizeV1")
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            player_id = await self._resolve_player_id(session, get_current_player_id())
            ranking_key = "gpx_s1_p1005"
            granted = await check_and_grant_gpx_milestones_for_player(
                session, player_id=player_id, ranking_key=ranking_key
            )
            if granted > 0:
                await session.commit()
        return score_ranking_pb2.ScoreRankingReceiveGrandPrixPrizeV1.Response()


def register_ranking_servicers(server: grpc.aio.Server) -> None:
    regular_servicer = RegularRankingService()
    regular_handlers = {
        "RegisterV1": takasho_unary_handler(
            regular_servicer.RegisterV1,
            regular_ranking_pb2.RegularRankingRegisterV1.Request,
            regular_ranking_pb2.RegularRankingRegisterV1.Response,
        ),
        "GetTopRankingV1": takasho_unary_handler(
            regular_servicer.GetTopRankingV1,
            regular_ranking_pb2.RegularRankingGetTopRankingV1.Request,
            regular_ranking_pb2.RegularRankingGetTopRankingV1.Response,
        ),
    }
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "takasho.schema.fes.player_api.RegularRanking", regular_handlers
            ),
        )
    )

    score_servicer = ScoreRankingService()
    score_handlers = {
        "GetAvailableV1": takasho_unary_handler(
            score_servicer.GetAvailableV1,
            score_ranking_pb2.ScoreRankingGetAvailableV1.Request,
            score_ranking_pb2.ScoreRankingGetAvailableV1.Response,
        ),
        "EntryV1": takasho_unary_handler(
            score_servicer.EntryV1,
            score_ranking_pb2.ScoreRankingEntryV1.Request,
            score_ranking_pb2.ScoreRankingEntryV1.Response,
        ),
        "GetGrandPrixLeaderBoardV1": takasho_unary_handler(
            score_servicer.GetGrandPrixLeaderBoardV1,
            score_ranking_pb2.ScoreRankingGetGrandPrixLeaderBoardV1.Request,
            score_ranking_pb2.ScoreRankingGetGrandPrixLeaderBoardV1.Response,
        ),
        "GetClassGroupLeaderBoardV1": takasho_unary_handler(
            score_servicer.GetClassGroupLeaderBoardV1,
            score_ranking_pb2.ScoreRankingGetClassGroupLeaderBoardV1.Request,
            score_ranking_pb2.ScoreRankingGetClassGroupLeaderBoardV1.Response,
        ),
        "GetGrandPrixRankV1": takasho_unary_handler(
            score_servicer.GetGrandPrixRankV1,
            score_ranking_pb2.ScoreRankingGetGrandPrixRankV1.Request,
            score_ranking_pb2.ScoreRankingGetGrandPrixRankV1.Response,
        ),
        "GetClassGroupRankV1": takasho_unary_handler(
            score_servicer.GetClassGroupRankV1,
            score_ranking_pb2.ScoreRankingGetClassGroupRankV1.Request,
            score_ranking_pb2.ScoreRankingGetClassGroupRankV1.Response,
        ),
        "GetClassGroupElevatingRankV1": takasho_unary_handler(
            score_servicer.GetClassGroupElevatingRankV1,
            score_ranking_pb2.ScoreRankingGetClassGroupElevatingRankV1.Request,
            score_ranking_pb2.ScoreRankingGetClassGroupElevatingRankV1.Response,
        ),
        "GetClassV1": takasho_unary_handler(
            score_servicer.GetClassV1,
            score_ranking_pb2.ScoreRankingGetClassV1.Request,
            score_ranking_pb2.ScoreRankingGetClassV1.Response,
        ),
        "RegisterV1": takasho_unary_handler(
            score_servicer.RegisterV1,
            score_ranking_pb2.ScoreRankingRegisterV1.Request,
            score_ranking_pb2.ScoreRankingRegisterV1.Response,
        ),
        "ReceiveClassPrizeV1": takasho_unary_handler(
            score_servicer.ReceiveClassPrizeV1,
            score_ranking_pb2.ScoreRankingReceiveClassPrizeV1.Request,
            score_ranking_pb2.ScoreRankingReceiveClassPrizeV1.Response,
        ),
        "ReceiveGrandPrixPrizeV1": takasho_unary_handler(
            score_servicer.ReceiveGrandPrixPrizeV1,
            score_ranking_pb2.ScoreRankingReceiveGrandPrixPrizeV1.Request,
            score_ranking_pb2.ScoreRankingReceiveGrandPrixPrizeV1.Response,
        ),
    }
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "takasho.schema.fes.player_api.ScoreRanking", score_handlers
            ),
            grpc.method_handlers_generic_handler(
                "takasho.schema.fes.player_api.score_ranking.ScoreRanking", score_handlers
            ),
        )
    )

    ranking_servicer = RankingService()
    ranking_handlers = {
        "GetAvailableEventV1": takasho_unary_handler(
            ranking_servicer.GetAvailableEventV1,
            ranking_pb2.RankingGetAvailableEventV1.Request,
            ranking_pb2.RankingGetAvailableEventV1.Response,
        ),
        "RegisterV1": takasho_unary_handler(
            ranking_servicer.RegisterV1,
            ranking_pb2.RankingRegisterV1.Request,
            ranking_pb2.RankingRegisterV1.Response,
        ),
        "GetMyRankV1": takasho_unary_handler(
            ranking_servicer.GetMyRankV1,
            ranking_pb2.RankingGetMyRankV1.Request,
            ranking_pb2.RankingGetMyRankV1.Response,
        ),
        "GetRankInfosV1": takasho_unary_handler(
            ranking_servicer.GetRankInfosV1,
            ranking_pb2.RankingGetRankInfosV1.Request,
            ranking_pb2.RankingGetRankInfosV1.Response,
        ),
        "GetRankInfosAroundMeV1": takasho_unary_handler(
            ranking_servicer.GetRankInfosAroundMeV1,
            ranking_pb2.RankingGetRankInfosAroundMeV1.Request,
            ranking_pb2.RankingGetRankInfosAroundMeV1.Response,
        ),
        "GetMyCommittedRankV1": takasho_unary_handler(
            ranking_servicer.GetMyCommittedRankV1,
            ranking_pb2.RankingGetMyCommittedRankV1.Request,
            ranking_pb2.RankingGetMyCommittedRankV1.Response,
        ),
        "GetTotalRegisteredCount": takasho_unary_handler(
            ranking_servicer.GetTotalRegisteredCount,
            ranking_pb2.RankingGetTotalRegisteredCountV1.Request,
            ranking_pb2.RankingGetTotalRegisteredCountV1.Response,
        ),
    }
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "takasho.schema.fes.player_api.Ranking", ranking_handlers
            ),
        )
    )
