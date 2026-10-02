"""Regression tests for the storage upsert paths.

Every "save player storage" handler in this codebase uses the same SQLite
``INSERT ... ON CONFLICT (player_id, key) DO UPDATE`` shape:

* ``services/player_storage.py``        - SetEntriesV2
* ``services/player_key_value_store.py`` - Increment/Update...AndSavePlayerStorageV1
* ``services/player_preference.py``     - SetAndSavePlayerStorageV1

That shape is not a stylistic choice. The original implementation did a SELECT
and then a plain INSERT, so the second write of an existing key raised
``sqlite3.IntegrityError: UNIQUE constraint failed``. The client surfaced that
as error code ``20027-2`` and dropped the player back to the title screen. The
code was fixed in several places; these tests pin the behaviour so it cannot
regress silently again.
"""

import pytest

from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_key_value_store_pb2,
    player_storage_pb2,
)
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_storage.v2_pb2 import (
    Criterion,
    Entry,
)
from dankagu.services.player_key_value_store import PlayerKeyValueStoreService
from dankagu.services.player_storage import PlayerStorageService


class MockContext:
    """Minimal stand-in for grpc.aio.ServicerContext."""

    def __init__(self) -> None:
        self.initial_metadata: list[tuple[str, str]] = []
        self.trailing_metadata: list[tuple[str, str]] = []

    async def send_initial_metadata(self, metadata: tuple[tuple[str, str], ...]) -> None:
        self.initial_metadata.extend(metadata)

    def set_trailing_metadata(self, metadata: tuple[tuple[str, str], ...]) -> None:
        self.trailing_metadata.extend(metadata)


async def _read_back(service: PlayerStorageService, key: str) -> bytes | None:
    """Fetch a single key back through the service's own read path."""
    request = player_storage_pb2.PlayerStorageGetEntriesV2.Request(
        criteria=[Criterion(key=key, matching_type=Criterion.MatchingType.EXACT)]
    )
    response = await service.GetEntriesV2(request, MockContext())  # type: ignore[arg-type]
    for entry in response.entries:
        if entry.key == key:
            return entry.value
    return None


@pytest.mark.asyncio
async def test_set_entries_v2_twice_with_same_key_updates_in_place() -> None:
    """Writing the same key twice must update, not raise a UNIQUE violation."""
    service = PlayerStorageService()
    context = MockContext()
    key = "UpsertRegression--1"

    first = player_storage_pb2.PlayerStorageSetEntriesV2.Request(
        entries=[Entry(player_id="upsert-player", key=key, value=b"\x01\x02")],
        next_revision="rev-1",
    )
    await service.SetEntriesV2(first, context)  # type: ignore[arg-type]

    second = player_storage_pb2.PlayerStorageSetEntriesV2.Request(
        entries=[Entry(player_id="upsert-player", key=key, value=b"\x09\x09\x09")],
        next_revision="rev-2",
    )
    response = await service.SetEntriesV2(second, context)  # type: ignore[arg-type]

    assert response.revision == "rev-2"
    assert await _read_back(service, key) == b"\x09\x09\x09"


@pytest.mark.asyncio
async def test_set_entries_v2_defaults_blank_player_id() -> None:
    """The client sends an empty player_id; it must be coerced, not stored empty.

    Storing "" was the direct trigger for the original 20027-2 bug: the lookup
    for player_id == "" found nothing while the pre-seeded template row already
    occupied ('default-player', key).
    """
    service = PlayerStorageService()
    request = player_storage_pb2.PlayerStorageSetEntriesV2.Request(
        entries=[Entry(player_id="", key="BlankPlayerId--1", value=b"\x07")],
        next_revision="rev-1",
    )
    response = await service.SetEntriesV2(request, MockContext())  # type: ignore[arg-type]

    assert response.entries[0].player_id == "default-player"


@pytest.mark.asyncio
async def test_pkvs_increment_and_save_is_idempotent_for_entries() -> None:
    """The PKVS save path shares the same upsert contract."""
    service = PlayerKeyValueStoreService()
    context = MockContext()
    key = "PkvsUpsertRegression--1"

    def make_request(value: bytes) -> object:
        return player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesAndSavePlayerStorageV1.Request(
            entries=[Entry(player_id="pkvs-player", key=key, value=value)],
            next_revision="rev-1",
        )

    await service.IncrementPlayerKeyValuesAndSavePlayerStorageV1(make_request(b"\x01"), context)  # type: ignore[arg-type]
    response = await service.IncrementPlayerKeyValuesAndSavePlayerStorageV1(  # type: ignore[arg-type]
        make_request(b"\x02\x02"), context
    )

    assert response.entries[0].value == b"\x02\x02"

    storage = PlayerStorageService()
    assert await _read_back(storage, key) == b"\x02\x02"


@pytest.mark.asyncio
async def test_pkvs_increment_echoes_delta_as_value() -> None:
    service = PlayerKeyValueStoreService()
    request = player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesV1.Request()
    item = request.player_key_values.add()
    item.key = "SomeCounter"
    item.delta = 7

    response = await service.IncrementPlayerKeyValuesV1(request, MockContext())  # type: ignore[arg-type]

    assert len(response.player_key_values) == 1
    assert response.player_key_values[0].key == "SomeCounter"
    # The request carries a delta; the response carries the resulting value.
    assert response.player_key_values[0].value == 7
    assert response.player_key_values[0].expired_at > 0
