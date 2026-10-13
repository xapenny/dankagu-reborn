"""Takasho Friend servicer with PostgreSQL persistence.

Player references reach this service in whichever form the client happens to
hold at that moment: ``players.id`` (the UUID it echoes back from a
``FriendPlayer``) in the follow and list flows, and the 8-digit プレイヤーID
(``players.short_id``) in the search flows. Relations written by older builds may
therefore hold a ``short_id`` instead of a ``players.id``, which silently breaks
both lists - the followed player's follower list matches on ``target_id`` and the
follower's following list joins on ``Player.id``, so neither finds the other.

Every write and lookup therefore goes through :func:`resolve_player_pk` /
:func:`resolve_players`, which normalise to ``players.id``, while the read side
matches *every* id form of the current player so pre-existing rows stay visible
without a data migration.

Player search additionally only publishes accounts that finished the tutorial
(see :func:`_public_profile_clause`): a fresh account has no ``PlayerBase--1``
profile and must not appear in the 検索 tab.
"""

import logging
import time
from collections.abc import Iterable

import grpc
from sqlalchemy import Select, and_, delete, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from dankagu.core.database import get_sessionmaker
from dankagu.core.session import get_current_player_id
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    friend_pb2,
    friend_pb2_grpc,
)
from dankagu.grpc.generated.takasho.schema.fes.resource.friend import (
    v1_pb2 as friend_v1_pb2,
)
from dankagu.models.player import Player
from dankagu.models.social import FriendRelation
from dankagu.models.storage import PlayerStorageEntry
from dankagu.services.fes_player import query_player_storage_by_keys

logger = logging.getLogger("dankagu.services.friend")

#: Storage key the client writes while completing the tutorial (player name,
#: short id, favourites). Its absence is what distinguishes an account that never
#: finished the tutorial from a playable one.
_PROFILE_KEY = "PlayerBase--1"

#: Storage key holding the publicly visible card/heroine/club selection.
_PUBLIC_STATUS_KEY = "PlayerPublicStatus--1"


# ---------------------------------------------------------------------------
# Player reference resolution
# ---------------------------------------------------------------------------


async def resolve_player_pk(session: AsyncSession, raw_id: str) -> tuple[str | None, str]:
    """Map a client-supplied player reference to ``players.id``.

    Returns ``(player_id, matched_form)``; ``matched_form`` is one of
    ``player_id``, ``player_id_ci``, ``short_id``, ``user_id``, ``empty`` or
    ``unresolved`` so callers can log exactly how a reference was interpreted.
    """
    raw = (raw_id or "").strip()
    if not raw:
        return None, "empty"

    for label, condition in (
        ("player_id", Player.id == raw),
        ("player_id_ci", func.lower(Player.id) == raw.lower()),
        ("short_id", Player.short_id == raw),
        ("user_id", Player.user_id == raw),
    ):
        hit = (await session.execute(select(Player.id).where(condition).limit(1))).scalar()
        if hit:
            return hit, label

    return None, "unresolved"


async def resolve_players(
    session: AsyncSession, raw_ids: Iterable[str | None]
) -> dict[str, Player]:
    """Resolve a batch of raw player references to ``Player`` rows.

    The returned mapping is keyed by every form a reference may appear as
    (``id``, lower-cased ``id``, ``short_id``, ``user_id``), so callers can look
    up whatever the client or an old relation row stored.
    """
    candidates = {str(raw).strip() for raw in raw_ids if raw and str(raw).strip()}
    if not candidates:
        return {}

    values = sorted(candidates)
    stmt = select(Player).where(
        or_(
            Player.id.in_(values),
            Player.short_id.in_(values),
            Player.user_id.in_(values),
        )
    )
    players = list((await session.execute(stmt)).scalars().all())

    # Second pass for UUIDs stored in a different case (cheap, and only needed
    # when the exact match found nothing for some candidate).
    lowered = sorted({value.lower() for value in candidates})
    if lowered:
        case_stmt = select(Player).where(func.lower(Player.id).in_(lowered))
        players.extend((await session.execute(case_stmt)).scalars().all())

    lookup: dict[str, Player] = {}
    for player in players:
        for key in (
            player.id,
            player.id.lower() if player.id else None,
            player.short_id,
            player.user_id,
        ):
            if key:
                lookup.setdefault(key, player)
    return lookup


def _lookup_player(lookup: dict[str, Player], raw: str) -> Player | None:
    player = lookup.get(raw)
    if player is None:
        player = lookup.get(raw.lower())
    return player


async def _resolved_pks(session: AsyncSession, raw_ids: Iterable[str | None]) -> set[str]:
    """Resolve raw relation ids to a set of ``players.id`` values."""
    values = [str(raw).strip() for raw in raw_ids if raw and str(raw).strip()]
    if not values:
        return set()
    lookup = await resolve_players(session, values)
    pks = {player.id for value in values if (player := _lookup_player(lookup, value)) is not None}
    return pks


async def _own_reference_ids(session: AsyncSession, player_pk: str) -> list[str]:
    """Every id form under which the current player may appear in a relation row."""
    refs = {player_pk}
    player = await session.get(Player, player_pk)
    if player is not None:
        if player.short_id:
            refs.add(player.short_id)
        if player.user_id:
            refs.add(player.user_id)
    return sorted(refs)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def _follow_filter(criterion: friend_v1_pb2.Criterion | None) -> int:
    """Requested Follow filter (``ALL_FOLLOW`` when the client sent none)."""
    if criterion is not None and criterion.HasField("filter"):
        return int(criterion.filter.follow)
    return int(friend_v1_pb2.Criterion.Filter.Follow.ALL_FOLLOW)


def _apply_follow_filter(
    candidates: set[str],
    *,
    follow_filter: int,
    related: set[str],
) -> set[str]:
    """Apply the client's mutual-follow filter *before* the LIMIT is applied.

    Filtering after the database LIMIT silently drops rows that should have been
    on the page.
    """
    if follow_filter == friend_v1_pb2.Criterion.Filter.Follow.MUTUAL:
        return {pk for pk in candidates if pk in related}
    if follow_filter == friend_v1_pb2.Criterion.Filter.Follow.NOT_MUTUAL:
        return {pk for pk in candidates if pk not in related}
    return set(candidates)


def _apply_player_ordering(stmt: Select, criterion: friend_v1_pb2.Criterion | None) -> Select:
    """Order a Player query the way the client asked (default: newest login first)."""
    ascending = bool(
        criterion is not None
        and criterion.order_direction == friend_v1_pb2.Criterion.OrderDirection.ASC
    )
    by_level = bool(
        criterion is not None
        and criterion.order_field == friend_v1_pb2.Criterion.OrderField.PLAYER_LEVEL
    )
    column = Player.level if by_level else Player.last_login_at
    primary = column.asc() if ascending else column.desc()
    # Deterministic tie-break: keeps paging and tests stable.
    return stmt.order_by(primary, Player.id)


def _non_empty_storage(key: str) -> ColumnElement[bool]:
    """``EXISTS`` clause: the player has a non-empty entry for ``key``.

    ``octet_length > 0`` matters because builds before the "don't persist empty
    values" fix stored empty blobs, which must not count as a written profile.
    """
    return exists().where(
        PlayerStorageEntry.player_id == Player.id,
        PlayerStorageEntry.key == key,
        func.octet_length(PlayerStorageEntry.value) > 0,
    )


def _public_profile_clause() -> ColumnElement[bool]:
    """SQL predicate for "this account may be shown to other players".

    Requires the tutorial profile (``PlayerBase--1``) plus a display name, and
    hides banned accounts. A freshly created account only has
    ``PlayerBootStatus--1`` / ``PlayerSetting--1`` / ``PlayerStatus--1``, so it
    is excluded - which is exactly the reported bug: the 検索 tab listed players
    who had never finished the tutorial.
    """
    return and_(
        Player.is_banned.is_(False),
        _non_empty_storage(_PROFILE_KEY),
        or_(Player.nickname != "", _non_empty_storage(_PUBLIC_STATUS_KEY)),
    )


def _warn_unresolved(method: str, raw_id: str, current_pid: str) -> None:
    logger.warning(
        "⚠️  [Friend] %s: cannot resolve target id %r for player %s - ignoring. "
        "The client sent a reference that is not a players.id / short_id / user_id.",
        method,
        raw_id,
        current_pid,
    )


async def _populate_friend_player(
    session: AsyncSession,
    proto_fp: friend_v1_pb2.FriendPlayer,
    target_player: Player,
    is_following: bool,
    is_followed: bool,
    storage_keys: list[str],
) -> None:
    """Populate a FriendPlayer protobuf message from database records."""
    proto_fp.player_id = target_player.id
    # Never fall back to ``Player.name``: accounts created by the server carry the
    # column default ("Player"), which would be published as a made-up nickname.
    # The client renders the real name from the PlayerBase--1 entry it requests.
    proto_fp.nickname = target_player.nickname or ""
    proto_fp.is_following = is_following
    proto_fp.is_followed = is_followed
    proto_fp.last_logged_in_at = target_player.last_login_at or int(time.time())

    if storage_keys:
        entries = await query_player_storage_by_keys(session, target_player.id, storage_keys)
        for row in entries:
            entry = proto_fp.player_storage_entries.add()
            entry.player_id = row.player_id
            entry.key = row.key
            entry.value = row.value
            entry.created_at = row.created_at
            entry.updated_at = row.updated_at


class FriendService(friend_pb2_grpc.FriendServicer):
    """Servicer for friends and followers."""

    async def GetMyFollowersV1(
        self,
        request: friend_pb2.FriendGetMyFollowersV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendGetMyFollowersV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        logger.info("👥 [Friend] GetMyFollowersV1 (player=%s)", current_pid)
        response = friend_pb2.FriendGetMyFollowersV1.Response()
        limit = int(request.max_results) if request.max_results > 0 else 50
        storage_keys = list(request.player_storage_keys)
        criterion = request.criterion if request.HasField("criterion") else None

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            my_refs = await _own_reference_ids(session, current_pid)
            # Who follows me, and whom I follow - both resolved to players.id.
            follower_rows = (
                (
                    await session.execute(
                        select(FriendRelation.follower_id).where(
                            FriendRelation.target_id.in_(my_refs)
                        )
                    )
                )
                .scalars()
                .all()
            )
            following_rows = (
                (
                    await session.execute(
                        select(FriendRelation.target_id).where(
                            FriendRelation.follower_id.in_(my_refs)
                        )
                    )
                )
                .scalars()
                .all()
            )

            follower_pks = await _resolved_pks(session, follower_rows)
            following_pks = await _resolved_pks(session, following_rows)
            candidates = _apply_follow_filter(
                follower_pks,
                follow_filter=_follow_filter(criterion),
                related=following_pks,
            )
            if not candidates:
                return response

            stmt = _apply_player_ordering(
                select(Player).where(Player.id.in_(candidates)), criterion
            ).limit(limit)
            players = list((await session.execute(stmt)).scalars().all())

            for p in players:
                fp = response.players.add()
                await _populate_friend_player(
                    session,
                    fp,
                    p,
                    is_following=p.id in following_pks,
                    is_followed=True,
                    storage_keys=storage_keys,
                )

        return response

    async def GetMyFollowingsV1(
        self,
        request: friend_pb2.FriendGetMyFollowingsV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendGetMyFollowingsV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        logger.info("👥 [Friend] GetMyFollowingsV1 (player=%s)", current_pid)
        response = friend_pb2.FriendGetMyFollowingsV1.Response()
        limit = int(request.max_results) if request.max_results > 0 else 50
        storage_keys = list(request.player_storage_keys)
        criterion = request.criterion if request.HasField("criterion") else None

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            my_refs = await _own_reference_ids(session, current_pid)
            following_rows = (
                (
                    await session.execute(
                        select(FriendRelation.target_id).where(
                            FriendRelation.follower_id.in_(my_refs)
                        )
                    )
                )
                .scalars()
                .all()
            )
            followed_rows = (
                (
                    await session.execute(
                        select(FriendRelation.follower_id).where(
                            FriendRelation.target_id.in_(my_refs)
                        )
                    )
                )
                .scalars()
                .all()
            )

            following_pks = await _resolved_pks(session, following_rows)
            followed_pks = await _resolved_pks(session, followed_rows)
            candidates = _apply_follow_filter(
                following_pks,
                follow_filter=_follow_filter(criterion),
                related=followed_pks,
            )
            if not candidates:
                return response

            stmt = _apply_player_ordering(
                select(Player).where(Player.id.in_(candidates)), criterion
            ).limit(limit)
            players = list((await session.execute(stmt)).scalars().all())

            for p in players:
                fp = response.players.add()
                await _populate_friend_player(
                    session,
                    fp,
                    p,
                    is_following=True,
                    is_followed=p.id in followed_pks,
                    storage_keys=storage_keys,
                )

        return response

    async def GetRecentlyLoggedInPlayersV1(
        self,
        request: friend_pb2.FriendGetRecentlyLoggedInPlayersV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendGetRecentlyLoggedInPlayersV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        logger.info("👥 [Friend] GetRecentlyLoggedInPlayersV1 (player=%s)", current_pid)
        response = friend_pb2.FriendGetRecentlyLoggedInPlayersV1.Response()
        limit = int(request.max_results) if request.max_results > 0 else 20
        storage_keys = list(request.player_storage_keys)
        criterion = request.criterion if request.HasField("criterion") else None

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            my_refs = await _own_reference_ids(session, current_pid)
            # 検索 tab: only accounts that finished the tutorial are published.
            stmt = _apply_player_ordering(
                select(Player).where(
                    Player.id != current_pid,
                    _public_profile_clause(),
                ),
                criterion,
            ).limit(limit)
            players = list((await session.execute(stmt)).scalars().all())
            if not players:
                return response

            following_rows = (
                (
                    await session.execute(
                        select(FriendRelation.target_id).where(
                            FriendRelation.follower_id.in_(my_refs)
                        )
                    )
                )
                .scalars()
                .all()
            )
            followed_rows = (
                (
                    await session.execute(
                        select(FriendRelation.follower_id).where(
                            FriendRelation.target_id.in_(my_refs)
                        )
                    )
                )
                .scalars()
                .all()
            )

            following_pks = await _resolved_pks(session, following_rows)
            followed_pks = await _resolved_pks(session, followed_rows)

            for p in players:
                fp = response.players.add()
                await _populate_friend_player(
                    session,
                    fp,
                    p,
                    is_following=p.id in following_pks,
                    is_followed=p.id in followed_pks,
                    storage_keys=storage_keys,
                )

        return response

    async def GetFollowStatusesV1(
        self,
        request: friend_pb2.FriendGetFollowStatusesV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendGetFollowStatusesV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        target_pids = [pid for pid in request.player_ids if pid]
        logger.info(
            "👥 [Friend] GetFollowStatusesV1 (player=%s, count=%d)",
            current_pid,
            len(target_pids),
        )
        response = friend_pb2.FriendGetFollowStatusesV1.Response()
        if not target_pids:
            return response

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            my_refs = await _own_reference_ids(session, current_pid)
            following_rows = (
                (
                    await session.execute(
                        select(FriendRelation.target_id).where(
                            FriendRelation.follower_id.in_(my_refs)
                        )
                    )
                )
                .scalars()
                .all()
            )
            followed_rows = (
                (
                    await session.execute(
                        select(FriendRelation.follower_id).where(
                            FriendRelation.target_id.in_(my_refs)
                        )
                    )
                )
                .scalars()
                .all()
            )
            following_pks = await _resolved_pks(session, following_rows)
            followed_pks = await _resolved_pks(session, followed_rows)

            # Resolve the requested references, but echo back the string the
            # client sent: it matches the response to its own request by value.
            requested_lookup = await resolve_players(session, target_pids)
            for raw in target_pids:
                target = _lookup_player(requested_lookup, raw)
                st = response.follow_statuses.add()
                st.player_id = raw
                st.is_following = bool(target and target.id in following_pks)
                st.is_followed = bool(target and target.id in followed_pks)

        return response

    async def SearchPlayerByPlayerShortIDV1(
        self,
        request: friend_pb2.FriendSearchPlayerByPlayerShortIDV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendSearchPlayerByPlayerShortIDV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        short_id = request.player_short_id.strip()
        logger.info(
            "👥 [Friend] SearchPlayerByPlayerShortIDV1 (player=%s, short_id=%s)",
            current_pid,
            short_id,
        )
        response = friend_pb2.FriendSearchPlayerByPlayerShortIDV1.Response()
        if not short_id:
            return response

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # Same tutorial gate as the 検索 list: an account that never finished
            # the tutorial must not be discoverable by its player id either.
            res = await session.execute(
                select(Player).where(Player.short_id == short_id, _public_profile_clause())
            )
            target = res.scalars().first()
            if not target:
                return response

            my_refs = await _own_reference_ids(session, current_pid)
            following = (
                await session.execute(
                    select(FriendRelation.id).where(
                        FriendRelation.follower_id.in_(my_refs),
                        FriendRelation.target_id.in_([target.id, target.short_id]),
                    )
                )
            ).scalar()
            followed = (
                await session.execute(
                    select(FriendRelation.id).where(
                        FriendRelation.target_id.in_(my_refs),
                        FriendRelation.follower_id.in_([target.id, target.short_id]),
                    )
                )
            ).scalar()
            await _populate_friend_player(
                session,
                response.player,
                target,
                is_following=following is not None,
                is_followed=followed is not None,
                storage_keys=list(request.player_storage_keys),
            )

        return response

    async def FollowV1(
        self,
        request: friend_pb2.FriendFollowV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendFollowV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        logger.info("👥 [Friend] FollowV1 (follower=%s, target=%s)", current_pid, request.player_id)
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            target_pk, matched = await resolve_player_pk(session, request.player_id)
            if target_pk is None:
                _warn_unresolved("FollowV1", request.player_id, current_pid)
                return friend_pb2.FriendFollowV1.Response()
            if matched not in {"player_id", "player_id_ci"}:
                logger.info(
                    "👥 [Friend] FollowV1 target %r resolved via %s -> %s",
                    request.player_id,
                    matched,
                    target_pk,
                )
            if target_pk == current_pid:
                return friend_pb2.FriendFollowV1.Response()

            # Always store players.id, whatever form the client used.
            stmt = (
                pg_insert(FriendRelation)
                .values(
                    follower_id=current_pid,
                    target_id=target_pk,
                    created_at=int(time.time()),
                )
                .on_conflict_do_nothing(
                    index_elements=[FriendRelation.follower_id, FriendRelation.target_id]
                )
            )
            await session.execute(stmt)
            await session.commit()

        return friend_pb2.FriendFollowV1.Response()

    async def UnfollowV1(
        self,
        request: friend_pb2.FriendUnfollowV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendUnfollowV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        logger.info(
            "👥 [Friend] UnfollowV1 (follower=%s, target=%s)", current_pid, request.player_id
        )
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            target_pk, _ = await resolve_player_pk(session, request.player_id)
            targets = {target_pk} if target_pk else set()
            # Also remove rows written by older builds under a raw reference.
            raw = (request.player_id or "").strip()
            if raw:
                targets.add(raw)
            for value in targets:
                await session.execute(
                    delete(FriendRelation).where(
                        FriendRelation.follower_id == current_pid,
                        FriendRelation.target_id == value,
                    )
                )
            await session.commit()

        return friend_pb2.FriendUnfollowV1.Response()

    async def RemoveMyFollowerV1(
        self,
        request: friend_pb2.FriendRemoveMyFollowerV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> friend_pb2.FriendRemoveMyFollowerV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        logger.info(
            "👥 [Friend] RemoveMyFollowerV1 (player=%s, follower=%s)",
            current_pid,
            request.player_id,
        )
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            follower_pk, _ = await resolve_player_pk(session, request.player_id)
            followers = {follower_pk} if follower_pk else set()
            raw = (request.player_id or "").strip()
            if raw:
                followers.add(raw)
            my_refs = await _own_reference_ids(session, current_pid)
            for value in followers:
                await session.execute(
                    delete(FriendRelation).where(
                        FriendRelation.follower_id == value,
                        FriendRelation.target_id.in_(my_refs),
                    )
                )
            await session.commit()

        return friend_pb2.FriendRemoveMyFollowerV1.Response()


def register_friend_servicer(server: grpc.aio.Server) -> None:
    servicer = FriendService()
    method_handlers = {
        "GetMyFollowersV1": takasho_unary_handler(
            servicer.GetMyFollowersV1,
            friend_pb2.FriendGetMyFollowersV1.Request,
            friend_pb2.FriendGetMyFollowersV1.Response,
        ),
        "GetMyFollowingsV1": takasho_unary_handler(
            servicer.GetMyFollowingsV1,
            friend_pb2.FriendGetMyFollowingsV1.Request,
            friend_pb2.FriendGetMyFollowingsV1.Response,
        ),
        "GetRecentlyLoggedInPlayersV1": takasho_unary_handler(
            servicer.GetRecentlyLoggedInPlayersV1,
            friend_pb2.FriendGetRecentlyLoggedInPlayersV1.Request,
            friend_pb2.FriendGetRecentlyLoggedInPlayersV1.Response,
        ),
        "GetFollowStatusesV1": takasho_unary_handler(
            servicer.GetFollowStatusesV1,
            friend_pb2.FriendGetFollowStatusesV1.Request,
            friend_pb2.FriendGetFollowStatusesV1.Response,
        ),
        "SearchPlayerByPlayerShortIDV1": takasho_unary_handler(
            servicer.SearchPlayerByPlayerShortIDV1,
            friend_pb2.FriendSearchPlayerByPlayerShortIDV1.Request,
            friend_pb2.FriendSearchPlayerByPlayerShortIDV1.Response,
        ),
        "FollowV1": takasho_unary_handler(
            servicer.FollowV1,
            friend_pb2.FriendFollowV1.Request,
            friend_pb2.FriendFollowV1.Response,
        ),
        "UnfollowV1": takasho_unary_handler(
            servicer.UnfollowV1,
            friend_pb2.FriendUnfollowV1.Request,
            friend_pb2.FriendUnfollowV1.Response,
        ),
        "RemoveMyFollowerV1": takasho_unary_handler(
            servicer.RemoveMyFollowerV1,
            friend_pb2.FriendRemoveMyFollowerV1.Request,
            friend_pb2.FriendRemoveMyFollowerV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.fes.player_api.friend.Friend", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
