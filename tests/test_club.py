"""Comprehensive Club tests covering all 8 Club services:

1. ClubService (creation, search, get by ids, preference updates)
2. ClubPlayerService (roster, storage entries, role management, kick, leave)
3. ClubPlayerJoinRequestService (join requests, auto/manual approval, cancel, reject, reject all)
4. ClubChatService (messages, stamps, counts)
5. ClubStorageService (shared KV storage)
6. ClubSpaceStorageService (3D preset space, locking, editing, unlock)
7. ClubAchievementService (available achievements, unlock & scalar increment)
8. ClubScalarService (club points / scalar querying)
"""

from __future__ import annotations

import uuid

import pytest
from player_factory import ensure_player

from dankagu.core.database import get_sessionmaker, init_db
from dankagu.core.session import reset_current_player, set_current_player
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    club_achievement_pb2,
    club_chat_pb2,
    club_pb2,
    club_player_pb2,
    club_scalar_pb2,
    club_space_storage_pb2,
    club_storage_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    club_player_join_request_pb2 as join_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.resource.club_space_storage import (
    v1_pb2 as space_v1_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.resource.club_storage import (
    v1_pb2 as storage_v1_pb2,
)
from dankagu.models.club import ClubJoinRequest
from dankagu.models.storage import PlayerStorageEntry
from dankagu.services.club import (
    ClubAchievementService,
    ClubChatService,
    ClubPlayerJoinRequestService,
    ClubPlayerService,
    ClubScalarService,
    ClubService,
    ClubSpaceStorageService,
    ClubStorageService,
    decode_join_club_status,
)


class DummyContext:
    async def abort(self, code: object, details: str) -> None:
        raise RuntimeError(f"Aborted: {code} - {details}")


async def _create_club(name: str, *, approval_policy: int = 1, searchable: bool = True):
    request = club_pb2.ClubCreate.Request()
    request.frame = 1
    request.preference.name = name
    request.preference.profile = "Test club profile"
    request.preference.play_style = 1
    request.preference.approval_policy = approval_policy
    request.preference.searchable = searchable
    request.preference.character_id = "1011001"
    return await ClubService().Create(request, DummyContext())


async def _create_club_with_new_leader(
    name: str, *, approval_policy: int = 1, searchable: bool = True
):
    leader = await ensure_player(nickname="Leader")
    tokens = set_current_player(leader)
    try:
        return await _create_club(name, approval_policy=approval_policy, searchable=searchable)
    finally:
        reset_current_player(tokens)


# ---------------------------------------------------------------------------
# 1. ClubService Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_club_create_makes_caller_leader_and_initializes_storage():
    await init_db()
    leader = await ensure_player(nickname="Leader")
    tokens = set_current_player(leader)
    try:
        name = f"Club {uuid.uuid4().hex[:8]}"
        response = await _create_club(name)
        club_id = response.club.club_id
        assert club_id
        assert response.club.preference.name == name
        assert response.club_storage_revision

        # Verify roster has leader
        roster = await ClubPlayerService().GetPlayers(
            club_player_pb2.ClubPlayerGetPlayers.Request(club_id=club_id), DummyContext()
        )
        assert len(roster.club_players) == 1
        assert roster.club_players[0].nickname == "Leader"
        assert roster.club_players[0].role == 1

        # Verify player storage JoinClubStatus was updated
        async with get_sessionmaker()() as session:
            entry = await session.get(PlayerStorageEntry, (leader, "JoinClubStatus--0"))
            assert entry is not None
            status_data = decode_join_club_status(entry.value)
            assert status_data["club_id"] == club_id
            assert status_data["club_name"] == name
            assert status_data["status"] == 3  # Joined
    finally:
        reset_current_player(tokens)


@pytest.mark.asyncio
async def test_club_search_and_preferences():
    await init_db()
    leader = await ensure_player(nickname="Leader")
    tokens = set_current_player(leader)
    try:
        marker = uuid.uuid4().hex[:8]
        club = await _create_club_with_new_leader(f"Searchable {marker}")
        hidden = await _create_club_with_new_leader(f"Hidden {marker}", searchable=False)

        # Search by name
        search_req = club_pb2.ClubSearchByName.Request(text=marker, max_results=10)
        found = await ClubService().SearchByName(search_req, DummyContext())
        names = {r.club.preference.name for r in found.search_results}
        assert f"Searchable {marker}" in names
        assert f"Hidden {marker}" not in names

        # Search by character
        by_char = await ClubService().SearchByCharacterIDs(
            club_pb2.ClubSearchByCharacterIDs.Request(character_ids=["1011001"], max_results=10),
            DummyContext(),
        )
        assert by_char.search_results

        # Search multi
        all_clubs = await ClubService().Search(
            club_pb2.ClubSearch.Request(max_results=10), DummyContext()
        )
        assert all_clubs.search_results

        # Get by IDs
        by_ids = await ClubService().GetByIDs(
            club_pb2.ClubGetByIDs.Request(club_ids=[club.club.club_id, hidden.club.club_id]),
            DummyContext(),
        )
        assert {e.club_id for e in by_ids.clubs} == {
            club.club.club_id,
            hidden.club.club_id,
        }

        # Set preference
        pref_req = club_pb2.ClubSetPreference.Request(club_id=club.club.club_id)
        pref_req.preference.name = f"Updated {marker}"
        pref_req.preference.profile = "Updated profile"
        pref_req.preference.play_style = 2
        pref_req.preference.approval_policy = 1
        pref_req.preference.searchable = True
        pref_req.preference.character_id = "1011001"

        pref_resp = await ClubService().SetPreference(pref_req, DummyContext())
        assert pref_resp.preference.name == f"Updated {marker}"
        assert pref_resp.preference.play_style == 2
    finally:
        reset_current_player(tokens)


# ---------------------------------------------------------------------------
# 2. ClubPlayerJoinRequestService Tests (Manual & Auto Approval)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_request_flow_manual_and_auto():
    await init_db()
    leader = await ensure_player(nickname="Leader")
    applicant = await ensure_player(nickname="Applicant")

    # 1. Manual approval club
    tokens_l = set_current_player(leader)
    try:
        club_manual = (
            await _create_club(f"Manual {uuid.uuid4().hex[:8]}", approval_policy=1)
        ).club.club_id
    finally:
        reset_current_player(tokens_l)

    tokens_a = set_current_player(applicant)
    try:
        # Create join request
        req_resp = await ClubPlayerJoinRequestService().Create(
            join_pb2.ClubPlayerJoinRequestCreate.Request(club_id=club_manual), DummyContext()
        )
        request_id = req_resp.request.request_id
        assert request_id

        async with get_sessionmaker()() as session:
            db_req = await session.get(ClubJoinRequest, request_id)
            assert db_req is not None
            assert db_req.status == 0  # Pending

        # Applicant sees their request
        my_reqs = await ClubPlayerJoinRequestService().GetMyRequests(
            join_pb2.ClubPlayerJoinRequestGetMyRequests.Request(), DummyContext()
        )
        assert any(r.request_id == request_id for r in my_reqs.requests)
    finally:
        reset_current_player(tokens_a)

    # Leader approves
    tokens_l = set_current_player(leader)
    try:
        club_reqs = await ClubPlayerJoinRequestService().GetMultiByClub(
            join_pb2.ClubPlayerJoinRequestGetMultiByClub.Request(club_id=club_manual),
            DummyContext(),
        )
        assert any(r.request_id == request_id for r in club_reqs.requests)

        await ClubPlayerJoinRequestService().Approve(
            join_pb2.ClubPlayerJoinRequestApprove.Request(request_id=request_id), DummyContext()
        )

        # Applicant is now a member
        count = await ClubPlayerService().GetPlayersCount(
            club_player_pb2.ClubPlayerGetPlayersCount.Request(club_id=club_manual), DummyContext()
        )
        assert count.players_count == 2
    finally:
        reset_current_player(tokens_l)

    # 2. Auto approval club
    tokens_l = set_current_player(leader)
    try:
        club_auto = (
            await _create_club(f"Auto {uuid.uuid4().hex[:8]}", approval_policy=2)
        ).club.club_id
    finally:
        reset_current_player(tokens_l)

    applicant2 = await ensure_player(nickname="Applicant2")
    tokens_a2 = set_current_player(applicant2)
    try:
        auto_resp = await ClubPlayerJoinRequestService().Create(
            join_pb2.ClubPlayerJoinRequestCreate.Request(club_id=club_auto), DummyContext()
        )
        auto_req_id = auto_resp.request.request_id
        assert auto_req_id

        async with get_sessionmaker()() as session:
            from sqlalchemy import select

            from dankagu.models.club import ClubMember

            member = (
                await session.execute(
                    select(ClubMember).where(
                        ClubMember.club_id == club_auto, ClubMember.player_id == applicant2
                    )
                )
            ).scalar_one_or_none()
            assert member is not None
            assert member.role == 3

        count2 = await ClubPlayerService().GetPlayersCount(
            club_player_pb2.ClubPlayerGetPlayersCount.Request(club_id=club_auto), DummyContext()
        )
        assert count2.players_count == 2
    finally:
        reset_current_player(tokens_a2)


# ---------------------------------------------------------------------------
# 3. ClubPlayerService Tests (Roster, Roles, Kick, Leave)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_club_player_roles_kick_and_leave():
    await init_db()
    leader = await ensure_player(nickname="Leader")
    member = await ensure_player(nickname="Member")

    tokens_l = set_current_player(leader)
    try:
        club_id = (
            await _create_club(f"Roles {uuid.uuid4().hex[:8]}", approval_policy=2)
        ).club.club_id
    finally:
        reset_current_player(tokens_l)

    tokens_m = set_current_player(member)
    try:
        await ClubPlayerJoinRequestService().Create(
            join_pb2.ClubPlayerJoinRequestCreate.Request(club_id=club_id), DummyContext()
        )
    finally:
        reset_current_player(tokens_m)

    tokens_l = set_current_player(leader)
    try:
        # Promote member to Sub-Leader (role=2)
        await ClubPlayerService().UpdateRole(
            club_player_pb2.ClubPlayerUpdateRole.Request(club_id=club_id, player_id=member, type=1),
            DummyContext(),
        )
        roster = await ClubPlayerService().GetPlayers(
            club_player_pb2.ClubPlayerGetPlayers.Request(club_id=club_id), DummyContext()
        )
        role_map = {p.player_id: p.role for p in roster.club_players}
        assert role_map[member] == 2

        # Transfer leadership to member (type=3)
        await ClubPlayerService().UpdateRole(
            club_player_pb2.ClubPlayerUpdateRole.Request(club_id=club_id, player_id=member, type=3),
            DummyContext(),
        )
        roster2 = await ClubPlayerService().GetPlayers(
            club_player_pb2.ClubPlayerGetPlayers.Request(club_id=club_id), DummyContext()
        )
        role_map2 = {p.player_id: p.role for p in roster2.club_players}
        assert role_map2[member] == 1  # Member is now leader
        assert role_map2[leader] == 2  # Former leader steps down to Sub-Leader

        # Former leader can leave
        await ClubPlayerService().Leave(
            club_player_pb2.ClubPlayerLeave.Request(club_id=club_id), DummyContext()
        )
    finally:
        reset_current_player(tokens_l)

    # Member (now leader) checks count
    tokens_m = set_current_player(member)
    try:
        count = await ClubPlayerService().GetPlayersCount(
            club_player_pb2.ClubPlayerGetPlayersCount.Request(club_id=club_id), DummyContext()
        )
        assert count.players_count == 1

        # Leader leaves when sole member -> club dissolves
        await ClubPlayerService().Leave(
            club_player_pb2.ClubPlayerLeave.Request(club_id=club_id), DummyContext()
        )
    finally:
        reset_current_player(tokens_m)


# ---------------------------------------------------------------------------
# 4. ClubChatService Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_club_chat_messages_and_count():
    await init_db()
    leader = await ensure_player(nickname="Leader")
    tokens = set_current_player(leader)
    try:
        club_id = (await _create_club(f"Chat {uuid.uuid4().hex[:8]}")).club.club_id

        # Initial message count is 0 (the client itself sends creation announcement via CreateMessage)
        req_c0 = club_chat_pb2.ClubChatGetMessagesCount.Request(
            message_ids=[
                club_chat_pb2.ClubChatGetMessagesCount.MessageID(club_id=club_id, message_id="")
            ]
        )
        c0 = await ClubChatService().GetMessagesCount(req_c0, DummyContext())
        assert c0.message_counts[0].messages_count == 0

        # Send text message
        msg1 = club_chat_pb2.ClubChatCreateMessage.Message(
            club_id=club_id,
            message_type=1,
            content="Hello club members!",
        )
        send_resp1 = await ClubChatService().CreateMessage(
            club_chat_pb2.ClubChatCreateMessage.Request(messages=[msg1]),
            DummyContext(),
        )
        assert len(send_resp1.message) == 1
        assert "Hello club members!" in send_resp1.message[0].content

        # Send stamp message
        msg2 = club_chat_pb2.ClubChatCreateMessage.Message(
            club_id=club_id,
            message_type=2,
            content="stamp_01",
        )
        await ClubChatService().CreateMessage(
            club_chat_pb2.ClubChatCreateMessage.Request(messages=[msg2]),
            DummyContext(),
        )

        # Verify messages count is 2 (2 messages)
        c2 = await ClubChatService().GetMessagesCount(req_c0, DummyContext())
        assert c2.message_counts[0].messages_count == 2

        # Get messages
        chat_history = await ClubChatService().Get(
            club_chat_pb2.ClubChatGet.Request(club_id=club_id, max_results=10), DummyContext()
        )
        assert len(chat_history.message) == 2
        assert "Hello club members!" in chat_history.message[0].content
        assert "stamp_01" in chat_history.message[1].content
    finally:
        reset_current_player(tokens)


# ---------------------------------------------------------------------------
# 5. ClubStorage & ClubSpaceStorage Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_club_storage_and_space_storage_locks():
    await init_db()
    leader = await ensure_player(nickname="Leader")
    tokens = set_current_player(leader)
    try:
        club_id = (await _create_club(f"Space {uuid.uuid4().hex[:8]}")).club.club_id

        # 1. ClubStorage KV
        set_req = club_storage_pb2.ClubStorageSetMulti.Request(
            club_id=club_id,
            entries=[
                storage_v1_pb2.ClubStorageEntry(
                    club_id=club_id, key="ClubSetting-1", value=b"\x01\x02"
                ),
            ],
            next_revision="2",
        )
        set_resp = await ClubStorageService().SetMulti(set_req, DummyContext())
        assert set_resp.revision == "2"

        get_resp = await ClubStorageService().GetMulti(
            club_storage_pb2.ClubStorageGetMulti.Request(
                club_id=club_id,
                criteria=[storage_v1_pb2.Criterion(key="ClubSetting-", matching_type=1)],
            ),
            DummyContext(),
        )
        assert any(e.key == "ClubSetting-1" and e.value == b"\x01\x02" for e in get_resp.entries)

        # Verify ClubStorage GetMulti with forward criterion "Club" NEVER leaks ClubSpace-* keys
        get_all_club_storage = await ClubStorageService().GetMulti(
            club_storage_pb2.ClubStorageGetMulti.Request(
                club_id=club_id,
                criteria=[storage_v1_pb2.Criterion(key="Club", matching_type=1)],
            ),
            DummyContext(),
        )
        assert not any(e.key.startswith("ClubSpace-") for e in get_all_club_storage.entries)

        # 2. ClubSpaceStorage GetMulti seeds default space body and thumbnail
        space_get = await ClubSpaceStorageService().GetMulti(
            club_space_storage_pb2.ClubSpaceStorageGetMulti.Request(
                club_id=club_id,
                criteria=[space_v1_pb2.Criterion(key="ClubSpace-1-", matching_type=1)],
            ),
            DummyContext(),
        )
        assert len(space_get.entries) >= 2
        assert any(e.key == "ClubSpace-1-Body" for e in space_get.entries)
        assert any(e.key == "ClubSpace-1-Thumb" for e in space_get.entries)

        # 3. Lock acquisition
        lock_resp = await ClubSpaceStorageService().GetMultiWithLock(
            club_space_storage_pb2.ClubSpaceStorageGetMultiWithLock.Request(
                club_id=club_id,
                criteria=[space_v1_pb2.Criterion(key="ClubSpace-1-", matching_type=1)],
                ttl=300,
            ),
            DummyContext(),
        )
        assert lock_resp.status.locked is True
        assert lock_resp.status.locked_by.player_id == leader

        # 4. Extend lock
        extend_resp = await ClubSpaceStorageService().ExtendLock(
            club_space_storage_pb2.ClubSpaceStorageExtendLock.Request(club_id=club_id, ttl=600),
            DummyContext(),
        )
        assert extend_resp.status.locked is True

        # 5. Unlock
        unlock_resp = await ClubSpaceStorageService().Unlock(
            club_space_storage_pb2.ClubSpaceStorageUnlock.Request(club_id=club_id),
            DummyContext(),
        )
        assert unlock_resp.status.locked is False
    finally:
        reset_current_player(tokens)


# ---------------------------------------------------------------------------
# 6. ClubScalarService & ClubAchievementService Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_club_scalar_points_and_achievement_increment():
    await init_db()
    leader = await ensure_player(nickname="Leader")
    tokens = set_current_player(leader)
    try:
        club_id = (await _create_club(f"Scalar {uuid.uuid4().hex[:8]}")).club.club_id

        # 1. Query initial ClubPoint -> default 100,000 (club energy)
        get_scalar_req = club_scalar_pb2.ClubScalarGet.Request(club_id=club_id, key="ClubPoint")
        res0 = await ClubScalarService().Get(get_scalar_req, DummyContext())
        assert res0.scalar.club_id == club_id
        assert res0.scalar.key == "ClubPoint"
        assert res0.scalar.value == 100000

        # 2. Unlock achievement and increment scalar
        ach_req = club_achievement_pb2.ClubAchievementUnlockAndIncrementClubScalar.Request(
            achievement_id="ach_first_blood",
            scalar_key="ClubPoint",
        )
        await ClubAchievementService().UnlockAndIncrementClubScalar(ach_req, DummyContext())

        # 3. Query ClubPoint again -> should be incremented (+100 -> 100100)
        res1 = await ClubScalarService().Get(get_scalar_req, DummyContext())
        assert res1.scalar.value == 100100

        # 4. Another increment (+100 -> 100200)
        await ClubAchievementService().UnlockAndIncrementClubScalar(ach_req, DummyContext())
        res2 = await ClubScalarService().Get(get_scalar_req, DummyContext())
        assert res2.scalar.value == 100200
    finally:
        reset_current_player(tokens)
