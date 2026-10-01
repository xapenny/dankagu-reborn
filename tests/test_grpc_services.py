import pytest

from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    ondemand_master_pb2,
    player_storage_pb2,
    system_pb2,
)
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_storage.v2_pb2 import (
    Criterion,
    Entry,
)
from dankagu.services.ondemand_master import OndemandMasterService
from dankagu.services.player_storage import PlayerStorageService
from dankagu.services.system import SystemService


class MockContext:
    def __init__(self) -> None:
        self.initial_metadata: list[tuple[str, str]] = []
        self.trailing_metadata: list[tuple[str, str]] = []

    async def send_initial_metadata(self, metadata: tuple[tuple[str, str], ...]) -> None:
        self.initial_metadata.extend(metadata)

    def set_trailing_metadata(self, metadata: tuple[tuple[str, str], ...]) -> None:
        self.trailing_metadata.extend(metadata)


@pytest.mark.asyncio
async def test_ondemand_master_get_entries() -> None:
    service = OndemandMasterService()
    context = MockContext()

    req = ondemand_master_pb2.OndemandMasterGetEntriesV2.Request(
        keys=["ODMGeneric_Boot", "AssetRootHash"]
    )
    res = await service.GetEntriesV1(req, context)  # type: ignore[arg-type]

    entry_keys = [e.key for e in res.entries]
    assert "ODMGeneric_Boot" in entry_keys
    assert "AssetRootHash" in entry_keys

    root_entry = next(e for e in res.entries if e.key == "AssetRootHash")
    assert b"n5rgXwR19ppZ_client" in root_entry.value


@pytest.mark.asyncio
async def test_system_authorize() -> None:
    service = SystemService()
    context = MockContext()

    req = system_pb2.SystemAuthorizeV3.Request(
        device_account="test-device-uuid-1234",
        device_password="secret-password",
    )
    res = await service.AuthorizeV3(req, context)  # type: ignore[arg-type]

    assert res.session_token != ""
    assert res.player_id != ""


@pytest.mark.asyncio
async def test_player_storage_roundtrip() -> None:
    service = PlayerStorageService()
    context = MockContext()

    # 1. SetEntries
    set_req = player_storage_pb2.PlayerStorageSetEntriesV2.Request(
        entries=[
            Entry(
                player_id="player-101",
                key="DeckStatus--0",
                value=b"\x01\x02\x03\x04",
            ),
            Entry(
                player_id="player-101",
                key="DeckStatus--1",
                value=b"\x05\x06\x07\x08",
            ),
        ],
        next_revision="rev-2",
    )
    set_res = await service.SetEntriesV2(set_req, context)  # type: ignore[arg-type]
    assert set_res.revision == "rev-2"
    assert len(set_res.entries) == 2

    # 2. GetEntries with FORWARD matching
    get_req = player_storage_pb2.PlayerStorageGetEntriesV2.Request(
        criteria=[
            Criterion(
                key="DeckStatus--",
                matching_type=Criterion.MatchingType.FORWARD,
            )
        ]
    )
    get_res = await service.GetEntriesV2(get_req, context)  # type: ignore[arg-type]
    retrieved_keys = [e.key for e in get_res.entries]
    assert "DeckStatus--0" in retrieved_keys
    assert "DeckStatus--1" in retrieved_keys

    # 3. GetEntries with EXACT matching
    get_exact_req = player_storage_pb2.PlayerStorageGetEntriesV2.Request(
        criteria=[
            Criterion(
                key="DeckStatus--0",
                matching_type=Criterion.MatchingType.EXACT,
            )
        ]
    )
    get_exact_res = await service.GetEntriesV2(get_exact_req, context)  # type: ignore[arg-type]
    assert len(get_exact_res.entries) == 1
    assert get_exact_res.entries[0].value == b"\x01\x02\x03\x04"
