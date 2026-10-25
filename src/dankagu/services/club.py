"""Takasho Club servicers (Club, ClubPlayer, ClubPlayerJoinRequest, ClubChat, ClubAchievement).

Supports Danmaku Kagura's 3-slot simultaneous club membership system.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid

import grpc
from google.protobuf.internal.decoder import _DecodeVarint
from google.protobuf.internal.encoder import _VarintBytes
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.core.database import get_sessionmaker
from dankagu.core.session import get_current_player_id
from dankagu.core.storage_state import StorageRevisionManager
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_storage import (
    v2_pb2 as storage_v2_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    club_achievement_pb2,
    club_achievement_pb2_grpc,
    club_chat_pb2,
    club_chat_pb2_grpc,
    club_pb2,
    club_pb2_grpc,
    club_player_join_request_pb2,
    club_player_join_request_pb2_grpc,
    club_player_pb2,
    club_player_pb2_grpc,
    club_scalar_pb2,
    club_scalar_pb2_grpc,
    club_space_storage_pb2,
    club_space_storage_pb2_grpc,
    club_storage_pb2,
    club_storage_pb2_grpc,
)
from dankagu.grpc.generated.takasho.schema.fes.resource.club import (
    v1_pb2 as club_v1_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.resource.club_chat import (
    v1_pb2 as club_chat_v1_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.resource.club_player import (
    v1_pb2 as club_player_v1_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.resource.club_player_join_request import (
    v1_pb2 as club_join_req_v1_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.resource.club_preference import (
    v1_pb2 as club_pref_v1_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.resource.club_scalar import (
    v1_pb2 as club_scalar_v1_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.resource.club_space_storage import (
    v1_pb2 as club_space_storage_v1_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.resource.club_storage import (
    v1_pb2 as club_storage_v1_pb2,
)
from dankagu.models.club import (
    Club,
    ClubChatMessage,
    ClubJoinRequest,
    ClubMember,
    ClubScalar,
    ClubStorage,
)
from dankagu.models.player import Player
from dankagu.models.storage import PlayerStorageEntry
from dankagu.services.fes_player import query_player_storage_by_keys

logger = logging.getLogger("dankagu.services.club")


async def query_player_storage_entries(
    session: AsyncSession,
    player_id: str,
    keys: list[str],
) -> list[storage_v2_pb2.Entry]:
    """Query a player's storage entries and convert to protobuf Entry messages."""
    if not keys:
        return []
    rows = await query_player_storage_by_keys(session, player_id, keys)
    return [
        storage_v2_pb2.Entry(
            player_id=r.player_id,
            key=r.key,
            value=r.value,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


def format_chat_message_content(content: str) -> str:
    """Ensure club chat message content is valid JSON conforming to MessageJson schema.

    In the Danmaku Kagura client, MessageInfo..ctor (RVA 0x24158D0) executes:
        JsonUtility.FromJson<MessageJson>(content)
    If this returns null (because content is not JSON or is malformed),
    the client throws an immediate NullReferenceException and returns to the Title/Login screen!
    """
    if not content:
        return json.dumps(
            {
                "Message": "",
                "CardID": 0,
                "HeroineID": 1,
                "IconType": 1,
                "IsContainBannedWord": False,
            },
            ensure_ascii=False,
        )
    try:
        data = json.loads(content)
        if isinstance(data, dict) and "Message" in data:
            return content
    except Exception:
        pass
    return json.dumps(
        {
            "Message": str(content),
            "CardID": 0,
            "HeroineID": 1,
            "IconType": 1,
            "IsContainBannedWord": False,
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# JoinClubStatus Codec & Storage Helpers (Supports 3 Slots: 0, 1, 2)
# ---------------------------------------------------------------------------


def encode_join_club_status(
    slot_id: int,
    status: int,  # 1=Lock, 2=ApplyWait, 3=Join, 4=Withdrawn
    club_id: str = "",
    readed_message_id: str = "",
    withdrawed_at: int = 0,
    last_position_id: int = 0,
    leader_short_id: str = "",
    club_name: str = "",
    created_at: int | None = None,
    last_update: int | None = None,
) -> bytes:
    """Encode client JoinClubStatusProto wire bytes."""
    now = int(time.time())
    c_at = created_at or now
    l_up = last_update or now
    buf = bytearray()
    if slot_id != 0:
        buf.extend(_VarintBytes((1 << 3) | 0))
        buf.extend(_VarintBytes(slot_id))
    if c_at != 0:
        buf.extend(_VarintBytes((2 << 3) | 0))
        buf.extend(_VarintBytes(c_at))
    if l_up != 0:
        buf.extend(_VarintBytes((3 << 3) | 0))
        buf.extend(_VarintBytes(l_up))
    if status != 0:
        buf.extend(_VarintBytes((4 << 3) | 0))
        buf.extend(_VarintBytes(status))
    if club_id:
        b = club_id.encode("utf-8")
        buf.extend(_VarintBytes((5 << 3) | 2))
        buf.extend(_VarintBytes(len(b)))
        buf.extend(b)
    if readed_message_id:
        b = readed_message_id.encode("utf-8")
        buf.extend(_VarintBytes((6 << 3) | 2))
        buf.extend(_VarintBytes(len(b)))
        buf.extend(b)
    if withdrawed_at != 0:
        buf.extend(_VarintBytes((8 << 3) | 0))
        buf.extend(_VarintBytes(withdrawed_at))
    if last_position_id != 0:
        buf.extend(_VarintBytes((9 << 3) | 0))
        buf.extend(_VarintBytes(last_position_id))
    if leader_short_id:
        b = leader_short_id.encode("utf-8")
        buf.extend(_VarintBytes((10 << 3) | 2))
        buf.extend(_VarintBytes(len(b)))
        buf.extend(b)
    if club_name:
        b = club_name.encode("utf-8")
        buf.extend(_VarintBytes((11 << 3) | 2))
        buf.extend(_VarintBytes(len(b)))
        buf.extend(b)
    return bytes(buf)


def decode_join_club_status(data: bytes) -> dict:
    """Decode client JoinClubStatusProto wire bytes into a dictionary."""
    res = {
        "slot_id": 0,
        "created_at": 0,
        "last_update": 0,
        "status": 0,
        "club_id": "",
        "readed_message_id": "",
        "withdrawed_at": 0,
        "last_position_id": 0,
        "leader_short_id": "",
        "club_name": "",
    }
    pos = 0
    n = len(data)
    while pos < n:
        tag, pos = _DecodeVarint(data, pos)
        field = tag >> 3
        wire = tag & 7
        if wire == 0:
            val, pos = _DecodeVarint(data, pos)
            if field == 1:
                res["slot_id"] = val
            elif field == 2:
                res["created_at"] = val
            elif field == 3:
                res["last_update"] = val
            elif field == 4:
                res["status"] = val
            elif field == 8:
                res["withdrawed_at"] = val
            elif field == 9:
                res["last_position_id"] = val
        elif wire == 2:
            length, pos = _DecodeVarint(data, pos)
            s_val = data[pos : pos + length].decode("utf-8", errors="replace")
            pos += length
            if field == 5:
                res["club_id"] = s_val
            elif field == 6:
                res["readed_message_id"] = s_val
            elif field == 10:
                res["leader_short_id"] = s_val
            elif field == 11:
                res["club_name"] = s_val
        else:
            break
    return res


async def update_player_club_status(
    session: AsyncSession,
    player_id: str,
    slot_id: int,
    status: int,
    club_id: str = "",
    club_name: str = "",
    leader_short_id: str = "",
    last_position_id: int = 0,
    withdrawed_at: int = 0,
) -> tuple[str, bytes]:
    """Upsert JoinClubStatus--{slot_id} in player_storage for the given player."""
    now = int(time.time())
    key = f"JoinClubStatus--{slot_id}"
    value = encode_join_club_status(
        slot_id=slot_id,
        status=status,
        club_id=club_id,
        club_name=club_name,
        leader_short_id=leader_short_id,
        last_position_id=last_position_id,
        withdrawed_at=withdrawed_at,
        last_update=now,
    )
    stmt = (
        pg_insert(PlayerStorageEntry)
        .values(
            player_id=player_id,
            key=key,
            value=value,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[PlayerStorageEntry.player_id, PlayerStorageEntry.key],
            set_={
                "value": value,
                "updated_at": now,
            },
        )
    )
    await session.execute(stmt)
    return key, value


#: Smallest valid PNG (1x1, transparent). The client decodes ClubSpace-*-Thumb as an image, so
#: an empty value there aborts the club-space scene and drops the player back to the title.
DEFAULT_CLUB_SPACE_THUMB = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def get_default_club_space_body(variant_id: int = 1) -> bytes:
    """Default JSON payload for ClubSpace-{variant_id}-Body (Hakoniwa 3D preset space).

    The client reads this into ``ClubStorageProvider.ClubSpaceData``, which is
    ``{ Status: MySpotStatusModel, Layout: List<HakoniwaObjectLayoutStatusModel> }``. Emitting the
    bare status object left ``Status`` null, and building the club space then aborted the whole
    scene back to the title. The flat fields are kept alongside the wrapper so either reader
    finds what it needs.
    """
    now = int(time.time())
    status = {
        "ID": variant_id,
        "PresetName": f"プリセット{variant_id}",
        "StageID": 0,
        "ThemeID": 1,
        "SpotCharacterIDs": [],
        "CreatedAt": now,
        "LastUpdate": now,
        "ThumbnailData": [],
        "MusicID": 0,
    }
    data = {
        "Status": status,
        "Layout": [],
        # legacy flat shape, harmless when unused
        **status,
        "LayoutData": [],
    }
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


async def find_available_slot(session: AsyncSession, player_id: str) -> int | None:
    """Find the first available slot (0, 1, or 2) not occupied by an active club membership."""
    stmt = select(ClubMember.slot_id).where(ClubMember.player_id == player_id)
    res = await session.execute(stmt)
    used_slots = set(res.scalars().all())
    for s in [0, 1, 2]:
        if s not in used_slots:
            return s
    return None


async def query_club_storage(
    session: AsyncSession,
    club_id: str,
    criteria: list[club_storage_v1_pb2.Criterion] | None = None,
    exclude_space_storage: bool = False,
) -> list[club_storage_v1_pb2.ClubStorageEntry]:
    """Query club_storage entries matching criteria."""
    if not criteria:
        stmt = select(ClubStorage).where(ClubStorage.club_id == club_id)
        if exclude_space_storage:
            stmt = stmt.where(~ClubStorage.key.startswith("ClubSpace-"))
        rows = (await session.execute(stmt)).scalars().all()
    else:
        conditions = []
        for c in criteria:
            if c.matching_type == club_storage_v1_pb2.Criterion.MatchingType.FORWARD:
                conditions.append(ClubStorage.key.startswith(c.key))
            else:
                conditions.append(ClubStorage.key == c.key)
        if not conditions:
            return []
        stmt = select(ClubStorage).where(
            ClubStorage.club_id == club_id,
            or_(*conditions),
        )
        if exclude_space_storage:
            stmt = stmt.where(~ClubStorage.key.startswith("ClubSpace-"))
        rows = (await session.execute(stmt)).scalars().all()

    entries = []
    for r in rows:
        e = club_storage_v1_pb2.ClubStorageEntry(
            club_id=r.club_id,
            key=r.key,
            value=r.value,
        )
        entries.append(e)
    return entries


async def build_club_proto(
    session: AsyncSession,
    club_row: Club,
    criteria: list[club_storage_v1_pb2.Criterion] | None = None,
) -> club_v1_pb2.Club:
    """Construct protobuf Club object from ORM model."""
    storage_entries = await query_club_storage(
        session, club_row.id, criteria, exclude_space_storage=True
    )
    pref = club_pref_v1_pb2.ClubPreference(
        name=club_row.name,
        profile=club_row.profile,
        play_style=club_row.play_style,
        approval_policy=club_row.approval_policy,
        searchable=club_row.searchable,
        character_id=club_row.character_id,
    )
    return club_v1_pb2.Club(
        club_id=club_row.id,
        organized_at=club_row.created_at,
        preference=pref,
        club_storage_entries=storage_entries,
    )


# ---------------------------------------------------------------------------
# 1. ClubService (Core Guild Management & Search)
# ---------------------------------------------------------------------------


class ClubService(club_pb2_grpc.ClubServicer):
    """Servicer for guild creation, lookup, preferences, and searches."""

    async def Create(
        self,
        request: club_pb2.ClubCreate.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_pb2.ClubCreate.Response:
        current_pid = get_current_player_id()
        if not current_pid:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "No player context")

        logger.info(
            "🏰 [Club] Create requested by player %s (name=%s)",
            current_pid,
            request.preference.name,
        )
        now = int(time.time())

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # 1. Check slot availability
            slot_id = await find_available_slot(session, current_pid)
            if slot_id is None:
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, "All 3 club slots are occupied")

            # 2. Get leader player details
            leader_res = await session.execute(select(Player).where(Player.id == current_pid))
            leader = leader_res.scalar_one_or_none()
            leader_short_id = leader.short_id if leader else ""

            # 3. Create Club record
            new_club = Club(
                name=request.preference.name or "New Club",
                profile=request.preference.profile or "",
                character_id=request.preference.character_id or "1011001",
                play_style=request.preference.play_style or 1,
                approval_policy=request.preference.approval_policy or 2,
                searchable=request.preference.searchable,
                frame=request.frame or 1,
                leader_player_id=current_pid,
                member_count=1,
                created_at=now,
                updated_at=now,
            )
            session.add(new_club)
            await session.flush()

            # 4. Create Leader membership
            member = ClubMember(
                club_id=new_club.id,
                player_id=current_pid,
                slot_id=slot_id,
                role=1,  # LEADER
                joined_at=now,
                last_logged_in_at=now,
            )
            session.add(member)

            # Seed 100,000 initial ClubPoint currency (クラブエネルギー) for every newly created club
            session.add(
                ClubScalar(
                    club_id=new_club.id,
                    key="ClubPoint",
                    value=100000,
                    updated_at=now,
                )
            )

            # 5. Save initial club_storage entries
            for entry in request.entries:
                cs = ClubStorage(
                    club_id=new_club.id,
                    key=entry.key,
                    value=entry.value,
                    revision=1,
                    updated_at=now,
                )
                session.add(cs)

            # Seed default 3D Hakoniwa space storage (preset 1)
            default_body = get_default_club_space_body(1)
            session.add(
                ClubStorage(
                    club_id=new_club.id,
                    key="ClubSpace-1-Body",
                    value=default_body,
                    revision=1,
                    updated_at=now,
                )
            )
            session.add(
                ClubStorage(
                    club_id=new_club.id,
                    key="ClubSpace-1-Thumb",
                    value=DEFAULT_CLUB_SPACE_THUMB,
                    revision=1,
                    updated_at=now,
                )
            )

            # 6. Apply any player_storage changes sent with request (e.g. gold deduction)
            written_storage_entries = []
            for pe in request.player_storage_entries:
                if pe.value:
                    stmt = (
                        pg_insert(PlayerStorageEntry)
                        .values(
                            player_id=current_pid,
                            key=pe.key,
                            value=pe.value,
                            created_at=now,
                            updated_at=now,
                        )
                        .on_conflict_do_update(
                            index_elements=[PlayerStorageEntry.player_id, PlayerStorageEntry.key],
                            set_={"value": pe.value, "updated_at": now},
                        )
                    )
                    await session.execute(stmt)
                    written_storage_entries.append(pe)

            # 7. Update player's JoinClubStatus--{slot_id} to Join (3)
            key_join, val_join = await update_player_club_status(
                session=session,
                player_id=current_pid,
                slot_id=slot_id,
                status=3,  # Join
                club_id=new_club.id,
                club_name=new_club.name,
                leader_short_id=leader_short_id,
                last_position_id=1,  # Leader
            )
            # Ensure the client's local cache immediately receives the updated JoinClubStatus
            written_storage_entries.append(
                storage_v2_pb2.Entry(
                    player_id=current_pid,
                    key=key_join,
                    value=val_join,
                    created_at=now,
                    updated_at=now,
                )
            )

            await session.commit()

            proto_club = await build_club_proto(session, new_club)

            final_rev = await StorageRevisionManager.set_revision_and_save(
                request.player_storage_next_revision, player_id=current_pid
            )

            response = club_pb2.ClubCreate.Response(
                club=proto_club,
                club_storage_revision="1",
                player_storage_entries=written_storage_entries,
                player_storage_revision=final_rev,
            )
            return response

    async def GetByIDs(
        self,
        request: club_pb2.ClubGetByIDs.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_pb2.ClubGetByIDs.Response:
        logger.info("🏰 [Club] GetByIDs: %s", request.club_ids)
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(Club).where(Club.id.in_(request.club_ids))
            rows = (await session.execute(stmt)).scalars().all()
            clubs = []
            for r in rows:
                c = await build_club_proto(session, r, list(request.criteria))
                clubs.append(c)
            return club_pb2.ClubGetByIDs.Response(clubs=clubs)

    async def Search(
        self,
        request: club_pb2.ClubSearch.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_pb2.ClubSearch.Response:
        logger.info("🔍 [Club] Search")
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(Club).where(Club.searchable.is_(True))
            if request.criterion.approval_policy != 0:
                stmt = stmt.where(Club.approval_policy == request.criterion.approval_policy)
            if request.criterion.play_style != 0:
                stmt = stmt.where(Club.play_style == request.criterion.play_style)
            elif request.criterion.play_styles:
                stmt = stmt.where(Club.play_style.in_(list(request.criterion.play_styles)))

            limit = request.max_results or 20
            stmt = stmt.order_by(Club.created_at.desc()).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()

            results = []
            for r in rows:
                c_proto = await build_club_proto(session, r, list(request.criteria))
                results.append(club_pb2.SearchResult(club=c_proto, member_count=r.member_count))

            return club_pb2.ClubSearch.Response(search_results=results)

    async def SearchByName(
        self,
        request: club_pb2.ClubSearchByName.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_pb2.ClubSearchByName.Response:
        logger.info("🔍 [Club] SearchByName: text=%r", request.text)
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(Club).where(Club.searchable.is_(True))
            if request.text:
                stmt = stmt.where(Club.name.ilike(f"%{request.text}%"))

            limit = request.max_results or 20
            stmt = stmt.order_by(Club.created_at.desc()).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()

            results = []
            for r in rows:
                c_proto = await build_club_proto(session, r, list(request.criteria))
                results.append(club_pb2.SearchResult(club=c_proto, member_count=r.member_count))

            return club_pb2.ClubSearchByName.Response(search_results=results)

    async def SearchByCharacterIDs(
        self,
        request: club_pb2.ClubSearchByCharacterIDs.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_pb2.ClubSearchByCharacterIDs.Response:
        logger.info("🔍 [Club] SearchByCharacterIDs: %s", request.character_ids)
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(Club).where(Club.searchable.is_(True))
            if request.character_ids:
                stmt = stmt.where(Club.character_id.in_(list(request.character_ids)))

            limit = request.max_results or 20
            stmt = stmt.order_by(Club.created_at.desc()).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()

            results = []
            for r in rows:
                c_proto = await build_club_proto(session, r, list(request.criteria))
                results.append(club_pb2.SearchResult(club=c_proto, member_count=r.member_count))

            return club_pb2.ClubSearchByCharacterIDs.Response(search_results=results)

    async def SetPreference(
        self,
        request: club_pb2.ClubSetPreference.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_pb2.ClubSetPreference.Response:
        logger.info("🏰 [Club] SetPreference: club_id=%s", request.club_id)
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(Club).where(Club.id == request.club_id)
            club_row = (await session.execute(stmt)).scalar_one_or_none()
            if not club_row:
                context.abort(grpc.StatusCode.NOT_FOUND, "Club not found")

            p = request.preference
            if p.name:
                club_row.name = p.name
            if p.profile:
                club_row.profile = p.profile
            if p.character_id:
                club_row.character_id = p.character_id
            if p.play_style != 0:
                club_row.play_style = p.play_style
            if p.approval_policy != 0:
                club_row.approval_policy = p.approval_policy
            club_row.searchable = p.searchable
            club_row.updated_at = int(time.time())

            await session.commit()
            return club_pb2.ClubSetPreference.Response(preference=request.preference)


# ---------------------------------------------------------------------------
# 2. ClubPlayerService (Roster, Multi-Slot Status, Roles)
# ---------------------------------------------------------------------------


class ClubPlayerService(club_player_pb2_grpc.ClubPlayerServicer):
    """Servicer for club membership and roster across 3 slots."""

    async def GetClubs(
        self,
        request: club_player_pb2.ClubPlayerGetClubs.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_pb2.ClubPlayerGetClubs.Response:
        target_pid = request.player_id or get_current_player_id()
        logger.info("🏰 [ClubPlayer] GetClubs for player=%s", target_pid)
        if not target_pid:
            return club_player_pb2.ClubPlayerGetClubs.Response()

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # Query all clubs the player is currently a member of (up to 3)
            stmt = (
                select(Club, ClubMember)
                .join(ClubMember, ClubMember.club_id == Club.id)
                .where(ClubMember.player_id == target_pid)
                .order_by(ClubMember.slot_id.asc())
            )
            rows = (await session.execute(stmt)).all()

            clubs_with_frame = []
            for club_row, member_row in rows:
                c_proto = await build_club_proto(session, club_row, list(request.criteria))
                clubs_with_frame.append(
                    club_player_pb2.ClubPlayerGetClubs.ClubWithFrame(
                        club=c_proto,
                        frame=member_row.slot_id,
                    )
                )

            return club_player_pb2.ClubPlayerGetClubs.Response(clubs=clubs_with_frame)

    async def GetPlayers(
        self,
        request: club_player_pb2.ClubPlayerGetPlayers.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_pb2.ClubPlayerGetPlayers.Response:
        logger.info("🏰 [ClubPlayer] GetPlayers for club_id=%s", request.club_id)
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = (
                select(ClubMember, Player)
                .join(Player, Player.id == ClubMember.player_id)
                .where(ClubMember.club_id == request.club_id)
                .order_by(ClubMember.role.asc(), ClubMember.joined_at.asc())
            )
            rows = (await session.execute(stmt)).all()

            club_players = []
            keys = list(request.player_storage_keys)
            for member, player in rows:
                entries = []
                if keys:
                    entries = await query_player_storage_entries(session, player.id, keys)

                cp = club_player_v1_pb2.ClubPlayer(
                    player_id=player.id,
                    role=member.role,
                    joined_at=member.joined_at,
                    last_logged_in_at=member.last_logged_in_at,
                    nickname=player.nickname or player.name,
                    player_short_id=player.short_id,
                    frame=1,
                    player_storage_entries=entries,
                )
                club_players.append(cp)

            return club_player_pb2.ClubPlayerGetPlayers.Response(club_players=club_players)

    async def GetPlayersCount(
        self,
        request: club_player_pb2.ClubPlayerGetPlayersCount.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_pb2.ClubPlayerGetPlayersCount.Response:
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(func.count(ClubMember.id)).where(ClubMember.club_id == request.club_id)
            count = (await session.execute(stmt)).scalar() or 0
            return club_player_pb2.ClubPlayerGetPlayersCount.Response(players_count=count)

    async def KickPlayer(
        self,
        request: club_player_pb2.ClubPlayerKickPlayer.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_pb2.ClubPlayerKickPlayer.Response:
        current_pid = get_current_player_id()
        logger.info(
            "🏰 [ClubPlayer] KickPlayer: caller=%s, target=%s, club=%s",
            current_pid,
            request.player_id,
            request.club_id,
        )

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # 1. Verify caller has permission (LEADER=1 or SUB_LEADER=2)
            caller_m = (
                await session.execute(
                    select(ClubMember).where(
                        ClubMember.club_id == request.club_id,
                        ClubMember.player_id == current_pid,
                    )
                )
            ).scalar_one_or_none()

            if not caller_m or caller_m.role > 2:
                context.abort(
                    grpc.StatusCode.PERMISSION_DENIED,
                    "Only leaders or sub-leaders can kick members",
                )

            target_m = (
                await session.execute(
                    select(ClubMember).where(
                        ClubMember.club_id == request.club_id,
                        ClubMember.player_id == request.player_id,
                    )
                )
            ).scalar_one_or_none()

            if not target_m:
                return club_player_pb2.ClubPlayerKickPlayer.Response()

            slot_id = target_m.slot_id
            await session.delete(target_m)

            # Update member count
            club_row = (
                await session.execute(select(Club).where(Club.id == request.club_id))
            ).scalar_one_or_none()
            if club_row and club_row.member_count > 1:
                club_row.member_count -= 1

            # Reset kicked player's slot storage
            now = int(time.time())
            await update_player_club_status(
                session=session,
                player_id=request.player_id,
                slot_id=slot_id,
                status=1,  # Open / Withdrawn
                withdrawed_at=now,
            )

            await session.commit()
            return club_player_pb2.ClubPlayerKickPlayer.Response()

    async def Leave(
        self,
        request: club_player_pb2.ClubPlayerLeave.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_pb2.ClubPlayerLeave.Response:
        current_pid = get_current_player_id()
        logger.info("🏰 [ClubPlayer] Leave: player=%s, club=%s", current_pid, request.club_id)

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            target_m = (
                await session.execute(
                    select(ClubMember).where(
                        ClubMember.club_id == request.club_id,
                        ClubMember.player_id == current_pid,
                    )
                )
            ).scalar_one_or_none()

            if not target_m:
                return club_player_pb2.ClubPlayerLeave.Response()

            slot_id = target_m.slot_id
            role = target_m.role
            await session.delete(target_m)

            club_row = (
                await session.execute(select(Club).where(Club.id == request.club_id))
            ).scalar_one_or_none()
            if club_row:
                if club_row.member_count > 1:
                    club_row.member_count -= 1
                    # If leader left, promote oldest remaining member to leader
                    if role == 1:
                        next_m = (
                            (
                                await session.execute(
                                    select(ClubMember)
                                    .where(ClubMember.club_id == request.club_id)
                                    .order_by(ClubMember.role.asc(), ClubMember.joined_at.asc())
                                )
                            )
                            .scalars()
                            .first()
                        )
                        if next_m:
                            next_m.role = 1
                            club_row.leader_player_id = next_m.player_id
                else:
                    # Last member left, remove club
                    await session.delete(club_row)

            # Reset leaver's slot
            now = int(time.time())
            await update_player_club_status(
                session=session,
                player_id=current_pid,
                slot_id=slot_id,
                status=1,  # Open / Withdrawn
                withdrawed_at=now,
            )

            await session.commit()
            return club_player_pb2.ClubPlayerLeave.Response()

    async def UpdateRole(
        self,
        request: club_player_pb2.ClubPlayerUpdateRole.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_pb2.ClubPlayerUpdateRole.Response:
        current_pid = get_current_player_id()
        logger.info(
            "🏰 [ClubPlayer] UpdateRole: caller=%s, target=%s, club=%s, type=%s",
            current_pid,
            request.player_id,
            request.club_id,
            request.type,
        )

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            target_m = (
                await session.execute(
                    select(ClubMember).where(
                        ClubMember.club_id == request.club_id,
                        ClubMember.player_id == request.player_id,
                    )
                )
            ).scalar_one_or_none()

            if not target_m:
                context.abort(grpc.StatusCode.NOT_FOUND, "Member not found")

            # 1 = ASSIGN_SUBLEADER, 2 = UNASSIGN_SUBLEADER, 3 = REPLACE_LEADER
            if request.type == 1:
                target_m.role = 2  # SUB_LEADER
            elif request.type == 2:
                target_m.role = 3  # MEMBER
            elif request.type == 3:
                # Leader replacement
                caller_m = (
                    await session.execute(
                        select(ClubMember).where(
                            ClubMember.club_id == request.club_id,
                            ClubMember.player_id == current_pid,
                        )
                    )
                ).scalar_one_or_none()
                if caller_m:
                    caller_m.role = 2  # Caller steps down to Sub-Leader
                target_m.role = 1  # Target becomes Leader

                club_row = (
                    await session.execute(select(Club).where(Club.id == request.club_id))
                ).scalar_one_or_none()
                if club_row:
                    club_row.leader_player_id = target_m.player_id

            await session.commit()
            return club_player_pb2.ClubPlayerUpdateRole.Response()


# ---------------------------------------------------------------------------
# 3. ClubPlayerJoinRequestService (Application Flow & Auto-Approval)
# ---------------------------------------------------------------------------


class ClubPlayerJoinRequestService(club_player_join_request_pb2_grpc.ClubPlayerJoinRequestServicer):
    """Servicer for club join applications."""

    async def Create(
        self,
        request: club_player_join_request_pb2.ClubPlayerJoinRequestCreate.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_join_request_pb2.ClubPlayerJoinRequestCreate.Response:
        current_pid = get_current_player_id()
        if not current_pid:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "No player context")

        logger.info("📝 [ClubJoinRequest] Create: player=%s, club=%s", current_pid, request.club_id)
        now = int(time.time())

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # 1. Check if already in club
            existing_m = (
                await session.execute(
                    select(ClubMember).where(
                        ClubMember.club_id == request.club_id,
                        ClubMember.player_id == current_pid,
                    )
                )
            ).scalar_one_or_none()
            if existing_m:
                context.abort(grpc.StatusCode.ALREADY_EXISTS, "Already a member of this club")

            # 2. Find available slot
            slot_id = await find_available_slot(session, current_pid)
            if slot_id is None:
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, "All 3 club slots are occupied")

            # 3. Query target club
            club_row = (
                await session.execute(select(Club).where(Club.id == request.club_id))
            ).scalar_one_or_none()
            if not club_row:
                context.abort(grpc.StatusCode.NOT_FOUND, "Club not found")

            # 4. If AUTO approval policy -> Join immediately!
            if club_row.approval_policy == 2:  # AUTO
                member = ClubMember(
                    club_id=club_row.id,
                    player_id=current_pid,
                    slot_id=slot_id,
                    role=3,  # MEMBER
                    joined_at=now,
                    last_logged_in_at=now,
                )
                session.add(member)
                club_row.member_count += 1

                # Update applicant JoinClubStatus--{slot_id} to Join (3)
                leader = (
                    await session.execute(
                        select(Player).where(Player.id == club_row.leader_player_id)
                    )
                ).scalar_one_or_none()
                leader_short_id = leader.short_id if leader else ""
                await update_player_club_status(
                    session=session,
                    player_id=current_pid,
                    slot_id=slot_id,
                    status=3,  # Join
                    club_id=club_row.id,
                    club_name=club_row.name,
                    leader_short_id=leader_short_id,
                    last_position_id=3,
                )

                # Post chat notification
                applicant = (
                    await session.execute(select(Player).where(Player.id == current_pid))
                ).scalar_one_or_none()
                nickname = applicant.nickname or applicant.name if applicant else "Player"
                welcome_msg = ClubChatMessage(
                    club_id=club_row.id,
                    player_id=current_pid,
                    message_type=2,  # NOTIFICATION
                    content=format_chat_message_content(f"{nickname} がクラブに参加しました！"),
                    created_at=now,
                )
                session.add(welcome_msg)
                await session.commit()

                proto_club = await build_club_proto(session, club_row)
                req_proto = club_join_req_v1_pb2.ClubPlayerJoinRequest(
                    request_id=f"req_{uuid.uuid4().hex[:12]}",
                    club=proto_club,
                    player_id=current_pid,
                    nickname=nickname,
                    requested_at=now,
                    frame=request.frame or 1,
                    club_players_count=club_row.member_count,
                )
                return club_player_join_request_pb2.ClubPlayerJoinRequestCreate.Response(
                    request=req_proto
                )

            # 5. MANUAL approval policy -> Save pending request
            new_req = ClubJoinRequest(
                club_id=club_row.id,
                player_id=current_pid,
                slot_id=slot_id,
                frame=request.frame or 1,
                status=0,  # PENDING
                created_at=now,
                updated_at=now,
            )
            session.add(new_req)

            # Update applicant JoinClubStatus--{slot_id} to ApplyWait (2)
            await update_player_club_status(
                session=session,
                player_id=current_pid,
                slot_id=slot_id,
                status=2,  # ApplyWait
                club_id=club_row.id,
                club_name=club_row.name,
            )
            await session.commit()

            applicant = (
                await session.execute(select(Player).where(Player.id == current_pid))
            ).scalar_one_or_none()
            nickname = applicant.nickname or applicant.name if applicant else "Player"
            proto_club = await build_club_proto(session, club_row)

            req_proto = club_join_req_v1_pb2.ClubPlayerJoinRequest(
                request_id=new_req.id,
                club=proto_club,
                player_id=current_pid,
                nickname=nickname,
                requested_at=now,
                frame=request.frame or 1,
                club_players_count=club_row.member_count,
            )
            return club_player_join_request_pb2.ClubPlayerJoinRequestCreate.Response(
                request=req_proto
            )

    async def GetMultiByClub(
        self,
        request: club_player_join_request_pb2.ClubPlayerJoinRequestGetMultiByClub.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_join_request_pb2.ClubPlayerJoinRequestGetMultiByClub.Response:
        logger.info("📝 [ClubJoinRequest] GetMultiByClub for club_id=%s", request.club_id)
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = (
                select(ClubJoinRequest, Player)
                .join(Player, Player.id == ClubJoinRequest.player_id)
                .where(
                    ClubJoinRequest.club_id == request.club_id,
                    ClubJoinRequest.status == 0,  # PENDING
                )
                .order_by(ClubJoinRequest.created_at.asc())
            )
            rows = (await session.execute(stmt)).all()

            club_row = (
                await session.execute(select(Club).where(Club.id == request.club_id))
            ).scalar_one_or_none()
            proto_club = await build_club_proto(session, club_row) if club_row else None

            requests_list = []
            keys = list(request.player_storage_keys)
            for req, player in rows:
                entries = []
                if keys:
                    entries = await query_player_storage_entries(session, player.id, keys)

                r_proto = club_join_req_v1_pb2.ClubPlayerJoinRequest(
                    request_id=req.id,
                    club=proto_club,
                    player_id=player.id,
                    nickname=player.nickname or player.name,
                    requested_at=req.created_at,
                    frame=req.frame,
                    club_players_count=club_row.member_count if club_row else 1,
                    player_storage_entries=entries,
                )
                requests_list.append(r_proto)

            return club_player_join_request_pb2.ClubPlayerJoinRequestGetMultiByClub.Response(
                requests=requests_list
            )

    async def GetMyRequests(
        self,
        request: club_player_join_request_pb2.ClubPlayerJoinRequestGetMyRequests.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_join_request_pb2.ClubPlayerJoinRequestGetMyRequests.Response:
        current_pid = get_current_player_id()
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = (
                select(ClubJoinRequest, Club)
                .join(Club, Club.id == ClubJoinRequest.club_id)
                .where(
                    ClubJoinRequest.player_id == current_pid,
                    ClubJoinRequest.status == 0,
                )
            )
            rows = (await session.execute(stmt)).all()

            requests_list = []
            for req, club_row in rows:
                proto_club = await build_club_proto(session, club_row, list(request.criteria))
                r_proto = club_join_req_v1_pb2.ClubPlayerJoinRequest(
                    request_id=req.id,
                    club=proto_club,
                    player_id=current_pid,
                    requested_at=req.created_at,
                    frame=req.frame,
                    club_players_count=club_row.member_count,
                )
                requests_list.append(r_proto)

            return club_player_join_request_pb2.ClubPlayerJoinRequestGetMyRequests.Response(
                requests=requests_list
            )

    async def Approve(
        self,
        request: club_player_join_request_pb2.ClubPlayerJoinRequestApprove.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_join_request_pb2.ClubPlayerJoinRequestApprove.Response:
        logger.info("📝 [ClubJoinRequest] Approve: req_id=%s", request.request_id)
        now = int(time.time())
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            req = (
                await session.execute(
                    select(ClubJoinRequest).where(ClubJoinRequest.id == request.request_id)
                )
            ).scalar_one_or_none()
            if not req or req.status != 0:
                context.abort(grpc.StatusCode.NOT_FOUND, "Request not found or already processed")

            req.status = 1  # APPROVED
            req.updated_at = now

            # Check if club still exists and has space
            club_row = (
                await session.execute(select(Club).where(Club.id == req.club_id))
            ).scalar_one_or_none()
            if club_row and club_row.member_count < club_row.max_members:
                # Add to club_members
                member = ClubMember(
                    club_id=req.club_id,
                    player_id=req.player_id,
                    slot_id=req.slot_id,
                    role=3,  # MEMBER
                    joined_at=now,
                    last_logged_in_at=now,
                )
                session.add(member)
                club_row.member_count += 1

                # Update applicant JoinClubStatus--{slot_id} to Join (3)
                leader = (
                    await session.execute(
                        select(Player).where(Player.id == club_row.leader_player_id)
                    )
                ).scalar_one_or_none()
                leader_short_id = leader.short_id if leader else ""
                await update_player_club_status(
                    session=session,
                    player_id=req.player_id,
                    slot_id=req.slot_id,
                    status=3,  # Join
                    club_id=club_row.id,
                    club_name=club_row.name,
                    leader_short_id=leader_short_id,
                    last_position_id=3,
                )

                # Post welcome chat
                applicant = (
                    await session.execute(select(Player).where(Player.id == req.player_id))
                ).scalar_one_or_none()
                nickname = applicant.nickname or applicant.name if applicant else "Player"
                welcome_msg = ClubChatMessage(
                    club_id=req.club_id,
                    player_id=req.player_id,
                    message_type=2,
                    content=format_chat_message_content(f"{nickname} がクラブに参加しました！"),
                    created_at=now,
                )
                session.add(welcome_msg)

            await session.commit()
            return club_player_join_request_pb2.ClubPlayerJoinRequestApprove.Response(
                request_id=request.request_id
            )

    async def Reject(
        self,
        request: club_player_join_request_pb2.ClubPlayerJoinRequestReject.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_join_request_pb2.ClubPlayerJoinRequestReject.Response:
        logger.info("📝 [ClubJoinRequest] Reject: req_id=%s", request.request_id)
        now = int(time.time())
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            req = (
                await session.execute(
                    select(ClubJoinRequest).where(ClubJoinRequest.id == request.request_id)
                )
            ).scalar_one_or_none()
            if not req:
                return club_player_join_request_pb2.ClubPlayerJoinRequestReject.Response()

            req.status = 2  # REJECTED
            req.updated_at = now

            # Reset applicant JoinClubStatus--{slot_id} to Open (1)
            await update_player_club_status(
                session=session,
                player_id=req.player_id,
                slot_id=req.slot_id,
                status=1,  # Open / Withdrawn
                withdrawed_at=now,
            )

            await session.commit()
            return club_player_join_request_pb2.ClubPlayerJoinRequestReject.Response(
                club_id=req.club_id
            )

    async def Cancel(
        self,
        request: club_player_join_request_pb2.ClubPlayerJoinRequestCancel.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_join_request_pb2.ClubPlayerJoinRequestCancel.Response:
        logger.info("📝 [ClubJoinRequest] Cancel: req_id=%s", request.request_id)
        now = int(time.time())
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            req = (
                await session.execute(
                    select(ClubJoinRequest).where(ClubJoinRequest.id == request.request_id)
                )
            ).scalar_one_or_none()
            if req:
                req.status = 3  # CANCELLED
                req.updated_at = now
                await update_player_club_status(
                    session=session,
                    player_id=req.player_id,
                    slot_id=req.slot_id,
                    status=1,  # Open / Withdrawn
                    withdrawed_at=now,
                )
                await session.commit()
            return club_player_join_request_pb2.ClubPlayerJoinRequestCancel.Response()

    async def RejectAll(
        self,
        request: club_player_join_request_pb2.ClubPlayerJoinRequestRejectAll.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_player_join_request_pb2.ClubPlayerJoinRequestRejectAll.Response:
        now = int(time.time())
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(ClubJoinRequest).where(
                ClubJoinRequest.club_id == request.club_id,
                ClubJoinRequest.status == 0,
            )
            rows = (await session.execute(stmt)).scalars().all()
            for r in rows:
                r.status = 2
                r.updated_at = now
                await update_player_club_status(
                    session=session,
                    player_id=r.player_id,
                    slot_id=r.slot_id,
                    status=1,  # Open / Withdrawn
                    withdrawed_at=now,
                )
            await session.commit()
            return club_player_join_request_pb2.ClubPlayerJoinRequestRejectAll.Response()


# ---------------------------------------------------------------------------
# 4. ClubChatService (Messages, Stamps, Badges)
# ---------------------------------------------------------------------------


class ClubChatService(club_chat_pb2_grpc.ClubChatServicer):
    """Servicer for club chat."""

    async def CreateMessage(
        self,
        request: club_chat_pb2.ClubChatCreateMessage.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_chat_pb2.ClubChatCreateMessage.Response:
        current_pid = get_current_player_id() or ""
        logger.info(
            "💬 [ClubChat] CreateMessage by player=%s (count=%d)",
            current_pid,
            len(request.messages),
        )
        now = int(time.time())

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            sender = (
                await session.execute(select(Player).where(Player.id == current_pid))
            ).scalar_one_or_none()
            nickname = sender.nickname or sender.name if sender else "Player"

            created_protos = []
            for m in request.messages:
                safe_content = format_chat_message_content(m.content)
                msg_row = ClubChatMessage(
                    id=m.message_id or f"msg_{uuid.uuid4().hex[:16]}",
                    club_id=m.club_id,
                    player_id=current_pid,
                    message_type=m.message_type or 1,
                    content=safe_content,
                    created_at=now,
                )
                session.add(msg_row)

                sender_proto = club_chat_v1_pb2.ClubChatMessagePlayer(
                    player_id=current_pid,
                    nickname=nickname,
                )
                proto_msg = club_chat_v1_pb2.ClubChatMessage(
                    club_id=m.club_id,
                    message_id=msg_row.id,
                    message_type=msg_row.message_type,
                    content=msg_row.content,
                    player=sender_proto,
                    posted_at=now,
                )
                created_protos.append(proto_msg)

            await session.commit()
            return club_chat_pb2.ClubChatCreateMessage.Response(message=created_protos)

    async def Get(
        self,
        request: club_chat_pb2.ClubChatGet.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_chat_pb2.ClubChatGet.Response:
        logger.info("💬 [ClubChat] Get: club_id=%s", request.club_id)
        limit = request.max_results or 50

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = (
                select(ClubChatMessage, Player)
                .outerjoin(Player, Player.id == ClubChatMessage.player_id)
                .where(ClubChatMessage.club_id == request.club_id)
                .order_by(ClubChatMessage.created_at.desc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).all()

            messages = []
            keys = list(request.player_storage_keys)
            for msg, player in rows:
                entries = []
                if keys and player:
                    entries = await query_player_storage_entries(session, player.id, keys)

                sender_proto = club_chat_v1_pb2.ClubChatMessagePlayer(
                    player_id=player.id if player else msg.player_id,
                    nickname=player.nickname or player.name if player else "Player",
                    player_storage_entries=entries,
                )
                safe_content = format_chat_message_content(msg.content)
                proto_msg = club_chat_v1_pb2.ClubChatMessage(
                    club_id=msg.club_id,
                    message_id=msg.id,
                    message_type=msg.message_type,
                    content=safe_content,
                    player=sender_proto,
                    posted_at=msg.created_at,
                )
                messages.append(proto_msg)

            # Return in chronological order
            messages.reverse()
            return club_chat_pb2.ClubChatGet.Response(message=messages)

    async def GetMessagesCount(
        self,
        request: club_chat_pb2.ClubChatGetMessagesCount.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_chat_pb2.ClubChatGetMessagesCount.Response:
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            counts = []
            for pair in request.message_ids:
                stmt = select(func.count(ClubChatMessage.id)).where(
                    ClubChatMessage.club_id == pair.club_id
                )
                if pair.message_id:
                    # Find created_at of pair.message_id
                    target_msg = (
                        await session.execute(
                            select(ClubChatMessage.created_at).where(
                                ClubChatMessage.id == pair.message_id
                            )
                        )
                    ).scalar()
                    if target_msg:
                        stmt = stmt.where(ClubChatMessage.created_at > target_msg)

                cnt = (await session.execute(stmt)).scalar() or 0
                counts.append(
                    club_chat_pb2.ClubChatGetMessagesCount.MessageCount(
                        club_id=pair.club_id,
                        messages_count=cnt,
                    )
                )

            return club_chat_pb2.ClubChatGetMessagesCount.Response(message_counts=counts)


# ---------------------------------------------------------------------------
# 5. ClubAchievementService
# ---------------------------------------------------------------------------


class ClubAchievementService(club_achievement_pb2_grpc.ClubAchievementServicer):
    """Servicer for club achievements."""

    async def GetAvailable(
        self,
        request: club_achievement_pb2.ClubAchievementGetAvailable.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_achievement_pb2.ClubAchievementGetAvailable.Response:
        logger.info("🏆 [ClubAchievement] GetAvailable")
        return club_achievement_pb2.ClubAchievementGetAvailable.Response()

    async def UnlockAndIncrementClubScalar(
        self,
        request: club_achievement_pb2.ClubAchievementUnlockAndIncrementClubScalar.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_achievement_pb2.ClubAchievementUnlockAndIncrementClubScalar.Response:
        player_id = get_current_player_id() or ""
        scalar_key = request.scalar_key or "ClubPoint"
        logger.info(
            "🏆 [ClubAchievement] UnlockAndIncrementClubScalar: player=%s, ach=%s, key=%s",
            player_id,
            request.achievement_id,
            scalar_key,
        )
        if player_id:
            session_factory = get_sessionmaker()
            async with session_factory() as session:
                stmt_member = select(ClubMember.club_id).where(ClubMember.player_id == player_id)
                club_id = (await session.execute(stmt_member)).scalars().first()
                if club_id:
                    inc = 100
                    now = int(time.time())
                    scalar_row = (
                        await session.execute(
                            select(ClubScalar).where(
                                ClubScalar.club_id == club_id, ClubScalar.key == scalar_key
                            )
                        )
                    ).scalar_one_or_none()
                    if scalar_row:
                        scalar_row.value += inc
                        scalar_row.updated_at = now
                    else:
                        session.add(
                            ClubScalar(club_id=club_id, key=scalar_key, value=inc, updated_at=now)
                        )
                    await session.commit()
                    logger.info(
                        "🏆 [ClubAchievement] Incremented scalar %s for club %s by %d",
                        scalar_key,
                        club_id,
                        inc,
                    )
        return club_achievement_pb2.ClubAchievementUnlockAndIncrementClubScalar.Response()


# ---------------------------------------------------------------------------
# 6. ClubStorageService
# ---------------------------------------------------------------------------


class ClubStorageService(club_storage_pb2_grpc.ClubStorageServicer):
    """Servicer for club shared KV storage (Settings, Items, Layouts, Purchases)."""

    async def GetMulti(
        self,
        request: club_storage_pb2.ClubStorageGetMulti.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_storage_pb2.ClubStorageGetMulti.Response:
        logger.info(
            "💾 [ClubStorage] GetMulti: club_id=%s, criteria_count=%d",
            request.club_id,
            len(request.criteria),
        )
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # CRITICAL: Exclude ClubSpace-* keys because ClubStorage on the client expects
            # strictly 4-part keys ({Prefix}-{VarientID}-{Suffix}-{SubID}). ClubSpace-*
            # belongs to ClubSpaceStorage; returning them in ClubStorage crashes the client.
            entries = await query_club_storage(
                session, request.club_id, list(request.criteria), exclude_space_storage=True
            )
            # Calculate a stable revision
            revision = "1"
            if entries:
                stmt = select(func.max(ClubStorage.revision)).where(
                    ClubStorage.club_id == request.club_id
                )
                max_rev = (await session.execute(stmt)).scalar_one_or_none()
                if max_rev:
                    revision = str(max_rev)
            return club_storage_pb2.ClubStorageGetMulti.Response(
                revision=revision,
                entries=entries,
            )

    async def SetMulti(
        self,
        request: club_storage_pb2.ClubStorageSetMulti.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_storage_pb2.ClubStorageSetMulti.Response:
        logger.info(
            "💾 [ClubStorage] SetMulti: club_id=%s, entries_count=%d, prev_rev=%s, next_rev=%s",
            request.club_id,
            len(request.entries),
            request.prev_revision,
            request.next_revision,
        )
        now = int(time.time())
        next_rev_int = 1
        try:
            next_rev_int = int(request.next_revision)
        except (ValueError, TypeError):
            next_rev_int = int(now)

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            saved_entries = []
            for e in request.entries:
                stmt = (
                    pg_insert(ClubStorage)
                    .values(
                        club_id=request.club_id,
                        key=e.key,
                        value=e.value,
                        revision=next_rev_int,
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=[ClubStorage.club_id, ClubStorage.key],
                        set_={
                            "value": e.value,
                            "revision": next_rev_int,
                            "updated_at": now,
                        },
                    )
                )
                await session.execute(stmt)
                saved_entries.append(
                    club_storage_v1_pb2.ClubStorageEntry(
                        club_id=request.club_id,
                        key=e.key,
                        value=e.value,
                    )
                )
            await session.commit()

            return club_storage_pb2.ClubStorageSetMulti.Response(
                revision=request.next_revision or str(next_rev_int),
                entries=saved_entries,
            )


# ---------------------------------------------------------------------------
# 7. ClubSpaceStorageService
# ---------------------------------------------------------------------------


class ClubSpaceStorageService(club_space_storage_pb2_grpc.ClubSpaceStorageServicer):
    """Servicer for club room/space layout, theme, and concurrent edit locks."""

    async def GetMulti(
        self,
        request: club_space_storage_pb2.ClubSpaceStorageGetMulti.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_space_storage_pb2.ClubSpaceStorageGetMulti.Response:
        logger.info(
            "🏠 [ClubSpaceStorage] GetMulti: club_id=%s, criteria_count=%d",
            request.club_id,
            len(request.criteria),
        )
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # Query club storage for space keys (e.g. ClubSpace-*)
            storage_criteria = [
                club_storage_v1_pb2.Criterion(
                    key=c.key,
                    matching_type=c.matching_type,
                )
                for c in request.criteria
            ]
            entries = await query_club_storage(session, request.club_id, storage_criteria)

            # If no ClubSpace body entry exists, synthesize and seed default space
            has_body = any(e.key.endswith("-Body") for e in entries)
            if not has_body:
                variants = set()
                for c in request.criteria:
                    if c.key.startswith("ClubSpace-"):
                        parts = c.key.split("-")
                        if len(parts) >= 2 and parts[1].isdigit():
                            variants.add(int(parts[1]))
                if not variants:
                    variants.add(1)

                now = int(time.time())
                for v_id in variants:
                    body_key = f"ClubSpace-{v_id}-Body"
                    thumb_key = f"ClubSpace-{v_id}-Thumb"
                    body_val = get_default_club_space_body(v_id)

                    stmt_body = (
                        pg_insert(ClubStorage)
                        .values(
                            club_id=request.club_id,
                            key=body_key,
                            value=body_val,
                            revision=1,
                            updated_at=now,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[ClubStorage.club_id, ClubStorage.key]
                        )
                    )
                    await session.execute(stmt_body)

                    stmt_thumb = (
                        pg_insert(ClubStorage)
                        .values(
                            club_id=request.club_id,
                            key=thumb_key,
                            value=DEFAULT_CLUB_SPACE_THUMB,
                            revision=1,
                            updated_at=now,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[ClubStorage.club_id, ClubStorage.key]
                        )
                    )
                    await session.execute(stmt_thumb)

                await session.commit()
                entries = await query_club_storage(session, request.club_id, storage_criteria)

            space_entries = [
                club_space_storage_v1_pb2.ClubSpaceStorageEntry(
                    club_id=e.club_id,
                    key=e.key,
                    value=e.value,
                )
                for e in entries
            ]
            return club_space_storage_pb2.ClubSpaceStorageGetMulti.Response(
                revision="1",
                entries=space_entries,
            )

    async def GetMultiWithLock(
        self,
        request: club_space_storage_pb2.ClubSpaceStorageGetMultiWithLock.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_space_storage_pb2.ClubSpaceStorageGetMultiWithLock.Response:
        current_pid = get_current_player_id() or "unknown"
        logger.info(
            "🏠 [ClubSpaceStorage] GetMultiWithLock: club_id=%s, ttl=%d, by=%s",
            request.club_id,
            request.ttl,
            current_pid,
        )
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            storage_criteria = [
                club_storage_v1_pb2.Criterion(
                    key=c.key,
                    matching_type=c.matching_type,
                )
                for c in request.criteria
            ]
            entries = await query_club_storage(session, request.club_id, storage_criteria)

            # If no ClubSpace body entry exists, synthesize and seed default space
            has_body = any(e.key.endswith("-Body") for e in entries)
            if not has_body:
                variants = set()
                for c in request.criteria:
                    if c.key.startswith("ClubSpace-"):
                        parts = c.key.split("-")
                        if len(parts) >= 2 and parts[1].isdigit():
                            variants.add(int(parts[1]))
                if not variants:
                    variants.add(1)

                now = int(time.time())
                for v_id in variants:
                    body_key = f"ClubSpace-{v_id}-Body"
                    thumb_key = f"ClubSpace-{v_id}-Thumb"
                    body_val = get_default_club_space_body(v_id)

                    stmt_body = (
                        pg_insert(ClubStorage)
                        .values(
                            club_id=request.club_id,
                            key=body_key,
                            value=body_val,
                            revision=1,
                            updated_at=now,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[ClubStorage.club_id, ClubStorage.key]
                        )
                    )
                    await session.execute(stmt_body)

                    stmt_thumb = (
                        pg_insert(ClubStorage)
                        .values(
                            club_id=request.club_id,
                            key=thumb_key,
                            value=DEFAULT_CLUB_SPACE_THUMB,
                            revision=1,
                            updated_at=now,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[ClubStorage.club_id, ClubStorage.key]
                        )
                    )
                    await session.execute(stmt_thumb)

                await session.commit()
                entries = await query_club_storage(session, request.club_id, storage_criteria)

            space_entries = [
                club_space_storage_v1_pb2.ClubSpaceStorageEntry(
                    club_id=e.club_id,
                    key=e.key,
                    value=e.value,
                )
                for e in entries
            ]
            # Grant lock to current player
            now = int(time.time())
            caller_p = (
                await session.execute(select(Player).where(Player.id == current_pid))
            ).scalar_one_or_none()
            caller_name = (
                caller_p.nickname
                if caller_p and caller_p.nickname
                else (caller_p.name if caller_p else "Player")
            )
            lock_status = club_space_storage_v1_pb2.ClubSpaceStorageStatus(
                locked=True,
                locked_by=club_space_storage_v1_pb2.ClubSpaceStorageStatusPlayer(
                    player_id=current_pid,
                    nickname=caller_name,
                ),
                unlocked_at=now + (request.ttl or 300),
            )
            return club_space_storage_pb2.ClubSpaceStorageGetMultiWithLock.Response(
                revision="1",
                entries=space_entries,
                status=lock_status,
            )

    async def SetMulti(
        self,
        request: club_space_storage_pb2.ClubSpaceStorageSetMulti.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_space_storage_pb2.ClubSpaceStorageSetMulti.Response:
        logger.info(
            "🏠 [ClubSpaceStorage] SetMulti: club_id=%s, entries_count=%d",
            request.club_id,
            len(request.entries),
        )
        now = int(time.time())
        next_rev_int = 1
        try:
            next_rev_int = int(request.next_revision)
        except (ValueError, TypeError):
            next_rev_int = int(now)

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            saved_entries = []
            for e in request.entries:
                stmt = (
                    pg_insert(ClubStorage)
                    .values(
                        club_id=request.club_id,
                        key=e.key,
                        value=e.value,
                        revision=next_rev_int,
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=[ClubStorage.club_id, ClubStorage.key],
                        set_={
                            "value": e.value,
                            "revision": next_rev_int,
                            "updated_at": now,
                        },
                    )
                )
                await session.execute(stmt)
                saved_entries.append(
                    club_space_storage_v1_pb2.ClubSpaceStorageEntry(
                        club_id=request.club_id,
                        key=e.key,
                        value=e.value,
                    )
                )
            await session.commit()

            return club_space_storage_pb2.ClubSpaceStorageSetMulti.Response(
                revision=request.next_revision or str(next_rev_int),
                entries=saved_entries,
            )

    async def Unlock(
        self,
        request: club_space_storage_pb2.ClubSpaceStorageUnlock.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_space_storage_pb2.ClubSpaceStorageUnlock.Response:
        logger.info("🏠 [ClubSpaceStorage] Unlock: club_id=%s", request.club_id)
        status = club_space_storage_v1_pb2.ClubSpaceStorageStatus(
            locked=False,
            unlocked_at=int(time.time()),
        )
        return club_space_storage_pb2.ClubSpaceStorageUnlock.Response(status=status)

    async def ExtendLock(
        self,
        request: club_space_storage_pb2.ClubSpaceStorageExtendLock.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_space_storage_pb2.ClubSpaceStorageExtendLock.Response:
        current_pid = get_current_player_id() or "unknown"
        logger.info(
            "🏠 [ClubSpaceStorage] ExtendLock: club_id=%s, ttl=%d", request.club_id, request.ttl
        )
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            caller_p = (
                await session.execute(select(Player).where(Player.id == current_pid))
            ).scalar_one_or_none()
            caller_name = (
                caller_p.nickname
                if caller_p and caller_p.nickname
                else (caller_p.name if caller_p else "Player")
            )
            status = club_space_storage_v1_pb2.ClubSpaceStorageStatus(
                locked=True,
                locked_by=club_space_storage_v1_pb2.ClubSpaceStorageStatusPlayer(
                    player_id=current_pid,
                    nickname=caller_name,
                ),
                unlocked_at=int(time.time()) + (request.ttl or 300),
            )
            return club_space_storage_pb2.ClubSpaceStorageExtendLock.Response(status=status)


# ---------------------------------------------------------------------------
# 8. ClubScalarService
# ---------------------------------------------------------------------------


class ClubScalarService(club_scalar_pb2_grpc.ClubScalarServicer):
    """Servicer for club scalar counters (ClubPoint)."""

    async def Get(
        self,
        request: club_scalar_pb2.ClubScalarGet.Request,
        context: grpc.aio.ServicerContext,
    ) -> club_scalar_pb2.ClubScalarGet.Response:
        key = request.key or "ClubPoint"
        logger.info("💎 [ClubScalar] Get: club_id=%s, key=%s", request.club_id, key)
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(ClubScalar.value).where(
                ClubScalar.club_id == request.club_id,
                ClubScalar.key == key,
            )
            val = (await session.execute(stmt)).scalar_one_or_none() or 0
            return club_scalar_pb2.ClubScalarGet.Response(
                scalar=club_scalar_v1_pb2.ClubScalar(
                    club_id=request.club_id,
                    key=key,
                    value=int(val),
                )
            )


# ---------------------------------------------------------------------------
# Registration Entry Point
# ---------------------------------------------------------------------------


def register_club_servicers(server: grpc.aio.Server) -> None:
    """Register all Club servicers with the gRPC server."""
    # 1. Club (Core Management)
    club_servicer = ClubService()
    club_handlers = {
        "Create": takasho_unary_handler(
            club_servicer.Create,
            club_pb2.ClubCreate.Request,
            club_pb2.ClubCreate.Response,
        ),
        "GetByIDs": takasho_unary_handler(
            club_servicer.GetByIDs,
            club_pb2.ClubGetByIDs.Request,
            club_pb2.ClubGetByIDs.Response,
        ),
        "Search": takasho_unary_handler(
            club_servicer.Search,
            club_pb2.ClubSearch.Request,
            club_pb2.ClubSearch.Response,
        ),
        "SearchByName": takasho_unary_handler(
            club_servicer.SearchByName,
            club_pb2.ClubSearchByName.Request,
            club_pb2.ClubSearchByName.Response,
        ),
        "SearchByCharacterIDs": takasho_unary_handler(
            club_servicer.SearchByCharacterIDs,
            club_pb2.ClubSearchByCharacterIDs.Request,
            club_pb2.ClubSearchByCharacterIDs.Response,
        ),
        "SetPreference": takasho_unary_handler(
            club_servicer.SetPreference,
            club_pb2.ClubSetPreference.Request,
            club_pb2.ClubSetPreference.Response,
        ),
    }
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "takasho.schema.fes.player_api.club.Club", club_handlers
            ),
        )
    )

    # 2. ClubPlayer (Membership & Slots)
    player_servicer = ClubPlayerService()
    player_handlers = {
        "GetClubs": takasho_unary_handler(
            player_servicer.GetClubs,
            club_player_pb2.ClubPlayerGetClubs.Request,
            club_player_pb2.ClubPlayerGetClubs.Response,
        ),
        "GetPlayers": takasho_unary_handler(
            player_servicer.GetPlayers,
            club_player_pb2.ClubPlayerGetPlayers.Request,
            club_player_pb2.ClubPlayerGetPlayers.Response,
        ),
        "GetPlayersCount": takasho_unary_handler(
            player_servicer.GetPlayersCount,
            club_player_pb2.ClubPlayerGetPlayersCount.Request,
            club_player_pb2.ClubPlayerGetPlayersCount.Response,
        ),
        "KickPlayer": takasho_unary_handler(
            player_servicer.KickPlayer,
            club_player_pb2.ClubPlayerKickPlayer.Request,
            club_player_pb2.ClubPlayerKickPlayer.Response,
        ),
        "Leave": takasho_unary_handler(
            player_servicer.Leave,
            club_player_pb2.ClubPlayerLeave.Request,
            club_player_pb2.ClubPlayerLeave.Response,
        ),
        "UpdateRole": takasho_unary_handler(
            player_servicer.UpdateRole,
            club_player_pb2.ClubPlayerUpdateRole.Request,
            club_player_pb2.ClubPlayerUpdateRole.Response,
        ),
    }
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "takasho.schema.fes.player_api.club_player.ClubPlayer", player_handlers
            ),
        )
    )

    # 3. ClubPlayerJoinRequest
    join_req_servicer = ClubPlayerJoinRequestService()
    join_req_handlers = {
        "Create": takasho_unary_handler(
            join_req_servicer.Create,
            club_player_join_request_pb2.ClubPlayerJoinRequestCreate.Request,
            club_player_join_request_pb2.ClubPlayerJoinRequestCreate.Response,
        ),
        "GetMultiByClub": takasho_unary_handler(
            join_req_servicer.GetMultiByClub,
            club_player_join_request_pb2.ClubPlayerJoinRequestGetMultiByClub.Request,
            club_player_join_request_pb2.ClubPlayerJoinRequestGetMultiByClub.Response,
        ),
        "GetMyRequests": takasho_unary_handler(
            join_req_servicer.GetMyRequests,
            club_player_join_request_pb2.ClubPlayerJoinRequestGetMyRequests.Request,
            club_player_join_request_pb2.ClubPlayerJoinRequestGetMyRequests.Response,
        ),
        "Approve": takasho_unary_handler(
            join_req_servicer.Approve,
            club_player_join_request_pb2.ClubPlayerJoinRequestApprove.Request,
            club_player_join_request_pb2.ClubPlayerJoinRequestApprove.Response,
        ),
        "Reject": takasho_unary_handler(
            join_req_servicer.Reject,
            club_player_join_request_pb2.ClubPlayerJoinRequestReject.Request,
            club_player_join_request_pb2.ClubPlayerJoinRequestReject.Response,
        ),
        "Cancel": takasho_unary_handler(
            join_req_servicer.Cancel,
            club_player_join_request_pb2.ClubPlayerJoinRequestCancel.Request,
            club_player_join_request_pb2.ClubPlayerJoinRequestCancel.Response,
        ),
        "RejectAll": takasho_unary_handler(
            join_req_servicer.RejectAll,
            club_player_join_request_pb2.ClubPlayerJoinRequestRejectAll.Request,
            club_player_join_request_pb2.ClubPlayerJoinRequestRejectAll.Response,
        ),
    }
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "takasho.schema.fes.player_api.club_player_join_request.ClubPlayerJoinRequest",
                join_req_handlers,
            ),
        )
    )

    # 4. ClubChat
    chat_servicer = ClubChatService()
    chat_handlers = {
        "GetMessagesCount": takasho_unary_handler(
            chat_servicer.GetMessagesCount,
            club_chat_pb2.ClubChatGetMessagesCount.Request,
            club_chat_pb2.ClubChatGetMessagesCount.Response,
        ),
        "Get": takasho_unary_handler(
            chat_servicer.Get,
            club_chat_pb2.ClubChatGet.Request,
            club_chat_pb2.ClubChatGet.Response,
        ),
        "CreateMessage": takasho_unary_handler(
            chat_servicer.CreateMessage,
            club_chat_pb2.ClubChatCreateMessage.Request,
            club_chat_pb2.ClubChatCreateMessage.Response,
        ),
    }
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "takasho.schema.fes.player_api.club_chat.ClubChat", chat_handlers
            ),
        )
    )

    # 5. ClubAchievement
    ach_servicer = ClubAchievementService()
    ach_handlers = {
        "GetAvailable": takasho_unary_handler(
            ach_servicer.GetAvailable,
            club_achievement_pb2.ClubAchievementGetAvailable.Request,
            club_achievement_pb2.ClubAchievementGetAvailable.Response,
        ),
        "UnlockAndIncrementClubScalar": takasho_unary_handler(
            ach_servicer.UnlockAndIncrementClubScalar,
            club_achievement_pb2.ClubAchievementUnlockAndIncrementClubScalar.Request,
            club_achievement_pb2.ClubAchievementUnlockAndIncrementClubScalar.Response,
        ),
    }
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "takasho.schema.fes.player_api.club_achievement.ClubAchievement", ach_handlers
            ),
        )
    )

    # 6. ClubStorage
    storage_servicer = ClubStorageService()
    storage_handlers = {
        "GetMulti": takasho_unary_handler(
            storage_servicer.GetMulti,
            club_storage_pb2.ClubStorageGetMulti.Request,
            club_storage_pb2.ClubStorageGetMulti.Response,
        ),
        "SetMulti": takasho_unary_handler(
            storage_servicer.SetMulti,
            club_storage_pb2.ClubStorageSetMulti.Request,
            club_storage_pb2.ClubStorageSetMulti.Response,
        ),
    }
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "takasho.schema.fes.player_api.club_storage.ClubStorage", storage_handlers
            ),
        )
    )

    # 7. ClubSpaceStorage
    space_storage_servicer = ClubSpaceStorageService()
    space_storage_handlers = {
        "GetMulti": takasho_unary_handler(
            space_storage_servicer.GetMulti,
            club_space_storage_pb2.ClubSpaceStorageGetMulti.Request,
            club_space_storage_pb2.ClubSpaceStorageGetMulti.Response,
        ),
        "GetMultiWithLock": takasho_unary_handler(
            space_storage_servicer.GetMultiWithLock,
            club_space_storage_pb2.ClubSpaceStorageGetMultiWithLock.Request,
            club_space_storage_pb2.ClubSpaceStorageGetMultiWithLock.Response,
        ),
        "SetMulti": takasho_unary_handler(
            space_storage_servicer.SetMulti,
            club_space_storage_pb2.ClubSpaceStorageSetMulti.Request,
            club_space_storage_pb2.ClubSpaceStorageSetMulti.Response,
        ),
        "Unlock": takasho_unary_handler(
            space_storage_servicer.Unlock,
            club_space_storage_pb2.ClubSpaceStorageUnlock.Request,
            club_space_storage_pb2.ClubSpaceStorageUnlock.Response,
        ),
        "ExtendLock": takasho_unary_handler(
            space_storage_servicer.ExtendLock,
            club_space_storage_pb2.ClubSpaceStorageExtendLock.Request,
            club_space_storage_pb2.ClubSpaceStorageExtendLock.Response,
        ),
    }
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "takasho.schema.fes.player_api.club_space_storage.ClubSpaceStorage",
                space_storage_handlers,
            ),
        )
    )

    # 8. ClubScalar
    scalar_servicer = ClubScalarService()
    scalar_handlers = {
        "Get": takasho_unary_handler(
            scalar_servicer.Get,
            club_scalar_pb2.ClubScalarGet.Request,
            club_scalar_pb2.ClubScalarGet.Response,
        ),
    }
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "takasho.schema.fes.player_api.club_scalar.ClubScalar",
                scalar_handlers,
            ),
        )
    )

    logger.info(
        "🏰 Registered Takasho Club servicers "
        "(Club, ClubPlayer, ClubPlayerJoinRequest, ClubChat, ClubAchievement, ClubStorage, ClubSpaceStorage, ClubScalar)"
    )
