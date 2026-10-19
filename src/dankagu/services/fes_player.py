"""Takasho FesPlayer servicer for player profile card and showcase inspection."""

import logging
import time

import grpc
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.core.database import get_sessionmaker
from dankagu.core.session import get_current_player_id
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_storage import (
    v2_pb2 as storage_v2_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    player_pb2,
    player_pb2_grpc,
)
from dankagu.models.player import Player
from dankagu.models.social import FriendRelation
from dankagu.models.storage import PlayerStorageEntry

logger = logging.getLogger("dankagu.services.fes_player")


async def query_player_storage_by_criteria(
    session: AsyncSession,
    player_id: str,
    criteria: list[storage_v2_pb2.Criterion],
) -> list[PlayerStorageEntry]:
    """Query a player's storage entries matching the provided criteria."""
    if not criteria:
        stmt = select(PlayerStorageEntry).where(PlayerStorageEntry.player_id == player_id)
        res = await session.execute(stmt)
        return list(res.scalars().all())

    exact_keys: list[str] = []
    prefix_keys: list[str] = []
    for c in criteria:
        if c.matching_type == storage_v2_pb2.Criterion.MatchingType.FORWARD:
            prefix_keys.append(c.key)
        else:
            exact_keys.append(c.key)

    conditions = []
    if exact_keys:
        conditions.append(PlayerStorageEntry.key.in_(exact_keys))
    for prefix in prefix_keys:
        conditions.append(PlayerStorageEntry.key.startswith(prefix))

    if not conditions:
        return []

    stmt = select(PlayerStorageEntry).where(
        PlayerStorageEntry.player_id == player_id,
        or_(*conditions),
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


def _build_key_conditions(keys: list[str]) -> list:
    conditions = []
    exact_keys = []
    for k in keys:
        if k.endswith("--"):
            conditions.append(PlayerStorageEntry.key.startswith(k))
        else:
            exact_keys.append(k)
            conditions.append(PlayerStorageEntry.key.startswith(k))
    if exact_keys:
        conditions.append(PlayerStorageEntry.key.in_(exact_keys))
    return conditions


async def query_player_storage_by_keys(
    session: AsyncSession,
    player_id: str,
    keys: list[str],
) -> list[PlayerStorageEntry]:
    """Query a player's storage entries by key or prefix list (used by Friend/MySpace RPCs)."""
    if not keys:
        return []

    conditions = _build_key_conditions(keys)
    if not conditions:
        return []

    stmt = select(PlayerStorageEntry).where(
        PlayerStorageEntry.player_id == player_id,
        or_(*conditions),
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def query_multi_player_storage_by_keys(
    session: AsyncSession,
    player_ids: list[str],
    keys: list[str],
) -> dict[str, list[PlayerStorageEntry]]:
    """Batch-query storage entries for multiple players in a single SQL round-trip."""
    if not player_ids or not keys:
        return {}

    conditions = _build_key_conditions(keys)
    if not conditions:
        return {}

    stmt = select(PlayerStorageEntry).where(
        PlayerStorageEntry.player_id.in_(player_ids),
        or_(*conditions),
    )
    res = await session.execute(stmt)
    grouped: dict[str, list[PlayerStorageEntry]] = {}
    for row in res.scalars().all():
        grouped.setdefault(row.player_id, []).append(row)
    return grouped


class FesPlayerService(player_pb2_grpc.FesPlayerServicer):
    """Servicer for FesPlayer profile lookup (takasho.schema.fes.player_api.player.FesPlayer)."""

    async def GetV1(
        self,
        request: player_pb2.FesPlayerGetPlayerServiceV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> player_pb2.FesPlayerGetPlayerServiceV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        target_pid = request.player_id or current_pid
        logger.info(
            "🪪 [FesPlayer] GetV1 (viewer=%s, target=%s, criteria=%d)",
            current_pid,
            target_pid,
            len(request.criteria),
        )

        response = player_pb2.FesPlayerGetPlayerServiceV1.Response()
        now = int(time.time())

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            target = await session.get(Player, target_pid)
            p_proto = response.player
            p_proto.player_id = target_pid
            if target:
                p_proto.nickname = target.nickname or target.name or "DanKagu"
                p_proto.player_short_id = target.short_id or "10000000"
                p_proto.last_logged_in_at = target.last_login_at or now
            else:
                p_proto.nickname = "DanKagu"
                p_proto.player_short_id = "10000000"
                p_proto.last_logged_in_at = now

            if current_pid != target_pid:
                rels_res = await session.execute(
                    select(FriendRelation.follower_id, FriendRelation.target_id).where(
                        or_(
                            and_(
                                FriendRelation.follower_id == current_pid,
                                FriendRelation.target_id == target_pid,
                            ),
                            and_(
                                FriendRelation.follower_id == target_pid,
                                FriendRelation.target_id == current_pid,
                            ),
                        )
                    )
                )
                rels = rels_res.all()
                p_proto.is_following = any(
                    f_id == current_pid and t_id == target_pid for f_id, t_id in rels
                )
                p_proto.is_followed = any(
                    f_id == target_pid and t_id == current_pid for f_id, t_id in rels
                )
            else:
                p_proto.is_following = False
                p_proto.is_followed = False

            entries = await query_player_storage_by_criteria(
                session, target_pid, list(request.criteria)
            )
            for row in entries:
                entry = p_proto.player_storage_entries.add()
                entry.player_id = row.player_id
                entry.key = row.key
                entry.value = row.value
                entry.created_at = row.created_at
                entry.updated_at = row.updated_at

        return response


def register_fes_player_servicer(server: grpc.aio.Server) -> None:
    servicer = FesPlayerService()
    method_handlers = {
        "GetV1": takasho_unary_handler(
            servicer.GetV1,
            player_pb2.FesPlayerGetPlayerServiceV1.Request,
            player_pb2.FesPlayerGetPlayerServiceV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.fes.player_api.player.FesPlayer", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
