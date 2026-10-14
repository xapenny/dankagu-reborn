"""Integration tests for Phase 2: Player profile, preferences, starter seed, and account transfer.

Verifies:
1. Fresh player cold-start onboarding flow (empty ToS -> age confirmation -> nickname setting).
2. PlayerPreference persistence: Preferences and nickname update in database and persist across restarts.
3. FesPlayerPreference: Level read/write and MySpace unlock threshold (level >= 10).
4. Starter seed & storage isolation: New players receive clean starter seed, not 2022 full-compendium cards.
5. Complete account transfer cycle (Suggest -> Register -> Execute -> Single-use invalidation -> Device B login).
"""

import time
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from dankagu.core.database import get_sessionmaker
from dankagu.core.session import (
    reset_current_player,
    set_current_player,
)
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_preference_pb2,
    player_storage_pb2,
    restoration_pb2,
    system_pb2,
)
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_storage.v2_pb2 import (
    Criterion,
)
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    player_preference_pb2 as fes_player_preference_pb2,
)
from dankagu.lcx.app import create_lcx_app
from dankagu.models.player import Player
from dankagu.services.player_preference import (
    FesPlayerPreferenceService,
    PlayerPreferenceService,
)
from dankagu.services.player_storage import PlayerStorageService
from dankagu.services.restoration import RestorationService
from dankagu.services.system import SystemService


class MockContext:
    def __init__(self, metadata: list[tuple[str, str]] | None = None) -> None:
        self._metadata = metadata or []

    def invocation_metadata(self) -> list[tuple[str, str]]:
        return self._metadata

    async def send_initial_metadata(self, metadata: tuple[tuple[str, str], ...]) -> None:
        pass

    def set_trailing_metadata(self, metadata: tuple[tuple[str, str], ...]) -> None:
        pass


@pytest.mark.asyncio
async def test_fresh_player_onboarding_preference_flow() -> None:
    """A fresh player has blank preferences, triggering ToS -> Age -> Nickname onboarding."""
    pref_service = PlayerPreferenceService()
    fes_service = FesPlayerPreferenceService()
    context = MockContext()

    now = int(time.time())
    pid = str(uuid.uuid4())
    uid = str(uuid.uuid4())

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        player = Player(
            id=pid,
            user_id=uid,
            registered_at=now,
            nickname="",
            birth_year=0,
            birth_month=0,
            consented_tos_version="",
            level=1,
        )
        session.add(player)
        await session.commit()

    tokens = set_current_player(pid)
    try:
        # Step 1: Client queries preference on boot -> empty values trigger onboarding modals
        get_res = await pref_service.GetPreferenceV1(
            player_preference_pb2.PlayerPreferenceGetPreferenceV1.Request(),
            context,  # type: ignore[arg-type]
        )
        assert get_res.preference.consented_tos_version == ""
        assert get_res.preference.birth_year == 0
        assert get_res.preference.nickname == ""
        assert len(get_res.player_short_id) > 0

        # Step 2: Player consents to Terms of Service
        set_tos_req = player_preference_pb2.PlayerPreferenceSetPreferenceV1.Request()
        set_tos_req.preference.consented_tos_version = "v2.1.0"
        set_tos_res = await pref_service.SetPreferenceV1(set_tos_req, context)  # type: ignore[arg-type]
        assert set_tos_res.preference.consented_tos_version == "v2.1.0"

        # Step 3: Player inputs birth date
        set_age_req = player_preference_pb2.PlayerPreferenceSetPreferenceV1.Request()
        set_age_req.preference.birth_year = 2002
        set_age_req.preference.birth_month = 7
        set_age_res = await pref_service.SetPreferenceV1(set_age_req, context)  # type: ignore[arg-type]
        assert set_age_res.preference.birth_year == 2002
        assert set_age_res.preference.birth_month == 7

        # Step 4: Player inputs custom nickname
        set_nick_req = player_preference_pb2.PlayerPreferenceSetPreferenceV1.Request()
        set_nick_req.preference.nickname = "HakureiShrineMaiden"
        set_nick_res = await pref_service.SetPreferenceV1(set_nick_req, context)  # type: ignore[arg-type]
        assert set_nick_res.preference.nickname == "HakureiShrineMaiden"

        # Step 5: Simulate client restart -> verify preferences persisted in DB
        reboot_res = await pref_service.GetPreferenceV1(
            player_preference_pb2.PlayerPreferenceGetPreferenceV1.Request(),
            context,  # type: ignore[arg-type]
        )
        assert reboot_res.preference.consented_tos_version == "v2.1.0"
        assert reboot_res.preference.birth_year == 2002
        assert reboot_res.preference.birth_month == 7
        assert reboot_res.preference.nickname == "HakureiShrineMaiden"

        # Step 6: Verify FesPlayerPreference starts at level 1 without MySpace unlocked
        fes_res = await fes_service.GetV1(
            fes_player_preference_pb2.FesPlayerPreferenceGetV1.Request(),
            context,  # type: ignore[arg-type]
        )
        assert fes_res.player_preference.player_level == 1
        assert not fes_res.player_preference.is_my_space_released
    finally:
        reset_current_player(tokens)


@pytest.mark.asyncio
async def test_fes_player_preference_level_progression() -> None:
    """FesPlayerPreference updates level and unlocks MySpace at level 10+."""
    fes_service = FesPlayerPreferenceService()
    context = MockContext()

    now = int(time.time())
    pid = str(uuid.uuid4())
    uid = str(uuid.uuid4())

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        session.add(Player(id=pid, user_id=uid, registered_at=now, level=3))
        await session.commit()

    tokens = set_current_player(pid)
    try:
        # Check initial level
        res1 = await fes_service.GetV1(
            fes_player_preference_pb2.FesPlayerPreferenceGetV1.Request(),
            context,  # type: ignore[arg-type]
        )
        assert res1.player_preference.player_level == 3
        assert not res1.player_preference.is_my_space_released

        # Player levels up to 12
        update_req = (
            fes_player_preference_pb2.FesPlayerPreferenceSetAndSavePlayerStorageV1.Request()
        )
        update_req.player_preference.player_level = 12
        update_req.next_revision = "rev-lvl-12"
        await fes_service.SetAndSavePlayerStorageV1(update_req, context)  # type: ignore[arg-type]

        # Verify updated level and unlocked MySpace
        res2 = await fes_service.GetV1(
            fes_player_preference_pb2.FesPlayerPreferenceGetV1.Request(),
            context,  # type: ignore[arg-type]
        )
        assert res2.player_preference.player_level == 12
        assert res2.player_preference.is_my_space_released
    finally:
        reset_current_player(tokens)


@pytest.mark.asyncio
async def test_starter_seed_isolation_prevents_2022_compendium_leak() -> None:
    """New players receive clean starter seed and do NOT inherit 590 historical cards."""
    system_service = SystemService()
    storage_service = PlayerStorageService()
    context = MockContext()

    device_acc = f"starter-test-{uuid.uuid4()}"
    auth_req = system_pb2.SystemAuthorizeV3.Request(device_account=device_acc)
    auth_res = await system_service.AuthorizeV3(auth_req, context)  # type: ignore[arg-type]
    new_pid = auth_res.player_id

    tokens = set_current_player(new_pid)
    try:
        # Query CardStatus with FORWARD matching
        card_req = player_storage_pb2.PlayerStorageGetEntriesV2.Request(
            criteria=[Criterion(key="CardStatus--", matching_type=Criterion.MatchingType.FORWARD)]
        )
        card_res = await storage_service.GetEntriesV2(card_req, context)  # type: ignore[arg-type]

        # Compendium-leak guard: a fresh account must not inherit the 2022 card
        # collection. Starter cards now ship inside PlayerSetting--1's N-card
        # list, so there are no per-card CardStatus entries at all.
        card_keys = [e.key for e in card_res.entries]
        assert card_keys == []

        # ...and the starter N card must be present in the initial settings.
        setting_req = player_storage_pb2.PlayerStorageGetEntriesV2.Request(
            criteria=[Criterion(key="PlayerSetting--1", matching_type=Criterion.MatchingType.EXACT)]
        )
        setting_res = await storage_service.GetEntriesV2(setting_req, context)  # type: ignore[arg-type]
        assert len(setting_res.entries) == 1
        # Card 100082 is written as a protobuf varint inside that entry.
        assert b"\xb2\x8d\x06" in setting_res.entries[0].value

        # Check PlayerBootStatus--1 exists
        boot_req = player_storage_pb2.PlayerStorageGetEntriesV2.Request(
            criteria=[
                Criterion(key="PlayerBootStatus--1", matching_type=Criterion.MatchingType.EXACT)
            ]
        )
        boot_res = await storage_service.GetEntriesV2(boot_req, context)  # type: ignore[arg-type]
        assert len(boot_res.entries) == 1
        assert boot_res.entries[0].key == "PlayerBootStatus--1"
    finally:
        reset_current_player(tokens)


@pytest.mark.asyncio
async def test_account_transfer_full_lifecycle() -> None:
    """Full account transfer lifecycle: suggest, register on device A, execute on device B."""
    app = create_lcx_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://danmakujp4-v1.lcx.tokyo"
    ) as client:
        # 1. Device A signs in
        dev_a_hash = f"device-a-{uuid.uuid4()}"
        res_a = await client.post("/identity/sign-in", json={"storeAccountIdHash": dev_a_hash})
        assert res_a.status_code == 200
        data_a = res_a.json()
        token_a = data_a["accessToken"]
        user_a_id = data_a["user"]["id"]

        # Set player profile on Device A
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            from sqlalchemy import select

            res = await session.execute(select(Player).where(Player.user_id == user_a_id))
            player_a = res.scalars().first()
            assert player_a is not None
            player_a.nickname = "TransferTestUser"
            player_a.level = 8
            await session.commit()

        # 2. Device A requests transfer credential suggestion
        headers_a = {"Authorization": f"Bearer {token_a}"}
        sugg_res = await client.post("/identity/transfer/suggest", headers=headers_a)
        assert sugg_res.status_code == 200
        sugg_data = sugg_res.json()
        transfer_id = sugg_data["id"]
        assert transfer_id.startswith("DK")
        assert len(transfer_id) == 8

        # 3. Device A registers the transfer credential with custom password
        password = "UserTransferPass#2026"
        reg_res = await client.post(
            "/identity/transfer",
            headers=headers_a,
            json={"id": transfer_id, "password": password},
        )
        assert reg_res.status_code == 200
        reg_data = reg_res.json()
        assert reg_data["id"] == transfer_id
        assert reg_data["expiresAt"] > int(time.time())

        # Duplicate registration with same active ID should fail
        dup_res = await client.post(
            "/identity/transfer",
            headers=headers_a,
            json={"id": transfer_id, "password": password},
        )
        assert dup_res.status_code == 400
        assert dup_res.json()["code"] == "LCX_TRANSFER_ID_ALREADY_EXISTS"

        # 4. Device B attempts transfer with WRONG password -> rejected
        wrong_res = await client.post(
            f"/identity/transfer/{transfer_id}/execute",
            json={"password": "WrongPassword"},
        )
        assert wrong_res.status_code == 400
        assert wrong_res.json()["code"] == "LCX_INVALID_TRANSFER_ID_OR_PASSWORD"

        # 5. Device B executes transfer with CORRECT password -> succeeds!
        exec_res = await client.post(
            f"/identity/transfer/{transfer_id}/execute",
            json={"password": password},
        )
        assert exec_res.status_code == 200
        exec_data = exec_res.json()
        assert exec_data["user"]["id"] == user_a_id
        dev_b_id_token = exec_data["lcxUserIdToken"]

        # 6. Re-executing already-used transfer code must be rejected, and say why:
        #    the client shows one generic string, so the server has to be specific.
        reuse_res = await client.post(
            f"/identity/transfer/{transfer_id}/execute",
            json={"password": password},
        )
        assert reuse_res.status_code == 400
        assert reuse_res.json()["code"] == "LCX_TRANSFER_ID_ALREADY_USED"

        # 7. Device B logs into gRPC with the new credentials -> gets original player profile!
        system_service = SystemService()
        pref_service = PlayerPreferenceService()
        fes_service = FesPlayerPreferenceService()
        context = MockContext()

        auth_req = system_pb2.SystemAuthorizeV3.Request(id_token=dev_b_id_token)
        auth_res = await system_service.AuthorizeV3(auth_req, context)  # type: ignore[arg-type]
        assert auth_res.player_id == player_a.id

        tokens_b = set_current_player(player_a.id)
        try:
            profile_res = await pref_service.GetPreferenceV1(
                player_preference_pb2.PlayerPreferenceGetPreferenceV1.Request(),
                context,  # type: ignore[arg-type]
            )
            assert profile_res.preference.nickname == "TransferTestUser"

            fes_res = await fes_service.GetV1(
                fes_player_preference_pb2.FesPlayerPreferenceGetV1.Request(),
                context,  # type: ignore[arg-type]
            )
            assert fes_res.player_preference.player_level == 8
        finally:
            reset_current_player(tokens_b)

    @pytest.mark.asyncio
    async def test_validate_banned_words_allows_valid_nickname(self) -> None:
        """Verify that normal player nicknames pass banned-word validation."""
        service = RestorationService()
        context = MockContext()

        req = restoration_pb2.RestorationValidateBannedWordsV1.Request(
            language_codes=["ja"],
            word="博丽灵梦",
        )
        res = await service.ValidateBannedWordsV1(req, context)  # type: ignore[arg-type]

        # In client disassembly: contains_banned_word = True indicates validation succeeded.
        assert res.contains_banned_word is True
        assert res.word == "博丽灵梦"

    @pytest.mark.asyncio
    async def test_validate_banned_words_rejects_empty_word(self) -> None:
        """Verify that empty words are rejected (contains_banned_word = False)."""
        service = RestorationService()
        context = MockContext()

        req = restoration_pb2.RestorationValidateBannedWordsV1.Request(
            language_codes=["ja"],
            word="   ",
        )
        res = await service.ValidateBannedWordsV1(req, context)  # type: ignore[arg-type]

        assert res.contains_banned_word is False
