import os
import uuid

import asyncpg
import pytest

from dankagu.config import settings
from dankagu.core.database import init_db, reset_engine
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_key_value_store_pb2,
    player_storage_pb2,
)
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_storage.v2_pb2 import (
    Criterion,
    Entry,
)
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    player_preference_pb2 as fes_player_preference_pb2,
)
from dankagu.services.player_key_value_store import PlayerKeyValueStoreService
from dankagu.services.player_preference import FesPlayerPreferenceService
from dankagu.services.player_storage import PlayerStorageService

PG_TEST_URL = os.getenv("DANKAGU_TEST_PG_URL", "")


async def _is_pg_reachable(url: str) -> bool:
    if not url:
        return False
    try:
        clean_url = url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(clean_url, timeout=2.0)
        await conn.close()
        return True
    except Exception:
        return False


class MockContext:
    def __init__(self) -> None:
        self.initial_metadata: list[tuple[str, str]] = []
        self.trailing_metadata: list[tuple[str, str]] = []

    async def send_initial_metadata(self, metadata: tuple[tuple[str, str], ...]) -> None:
        self.initial_metadata.extend(metadata)

    def set_trailing_metadata(self, metadata: tuple[tuple[str, str], ...]) -> None:
        self.trailing_metadata.extend(metadata)


@pytest.mark.asyncio
async def test_postgresql_storage_and_revision_persistence() -> None:
    if not PG_TEST_URL or not await _is_pg_reachable(PG_TEST_URL):
        pytest.skip("DANKAGU_TEST_PG_URL is not set or PostgreSQL server is not reachable")

    original_db_url = settings.db_url
    try:
        # Switch settings to PostgreSQL for this test
        settings.db_url = PG_TEST_URL
        reset_engine()
        await init_db()

        test_player_id = f"pg-test-{uuid.uuid4().hex[:8]}"
        test_key = f"DeckStatus--{uuid.uuid4().hex[:6]}"

        storage_service = PlayerStorageService()
        pkvs_service = PlayerKeyValueStoreService()
        fes_pref_service = FesPlayerPreferenceService()
        ctx = MockContext()

        # 1. Test PlayerStorageService.SetEntriesV2 (Insert)
        req1 = player_storage_pb2.PlayerStorageSetEntriesV2.Request(
            entries=[Entry(player_id=test_player_id, key=test_key, value=b"initial_deck_binary")],
            next_revision="rev-pg-001",
        )
        resp1 = await storage_service.SetEntriesV2(req1, ctx)  # type: ignore[arg-type]
        assert resp1.revision == "rev-pg-001"

        # Read back via GetEntriesV2
        read_req = player_storage_pb2.PlayerStorageGetEntriesV2.Request(
            criteria=[Criterion(key=test_key, matching_type=Criterion.MatchingType.EXACT)]
        )
        read_resp = await storage_service.GetEntriesV2(read_req, ctx)  # type: ignore[arg-type]
        found = [
            e for e in read_resp.entries if e.key == test_key and e.player_id == test_player_id
        ]
        assert len(found) == 1
        assert found[0].value == b"initial_deck_binary"

        # 2. Test PlayerStorageService.SetEntriesV2 (Update / Conflict)
        req2 = player_storage_pb2.PlayerStorageSetEntriesV2.Request(
            entries=[Entry(player_id=test_player_id, key=test_key, value=b"updated_deck_binary")],
            next_revision="rev-pg-002",
        )
        resp2 = await storage_service.SetEntriesV2(req2, ctx)  # type: ignore[arg-type]
        assert resp2.revision == "rev-pg-002"

        read_resp2 = await storage_service.GetEntriesV2(read_req, ctx)  # type: ignore[arg-type]
        found2 = [
            e for e in read_resp2.entries if e.key == test_key and e.player_id == test_player_id
        ]
        assert len(found2) == 1
        assert found2[0].value == b"updated_deck_binary"

        # 3. Test PKVS IncrementPlayerKeyValuesAndSavePlayerStorageV1
        pkvs_key = f"PkvsKey--{uuid.uuid4().hex[:6]}"
        pkvs_req = player_key_value_store_pb2.PlayerKeyValueStoreIncrementPlayerKeyValuesAndSavePlayerStorageV1.Request(
            entries=[Entry(player_id=test_player_id, key=pkvs_key, value=b"pkvs_state")],
            next_revision="rev-pg-003",
        )
        pkvs_resp = await pkvs_service.IncrementPlayerKeyValuesAndSavePlayerStorageV1(pkvs_req, ctx)  # type: ignore[arg-type]
        assert pkvs_resp.revision == "rev-pg-003"

        # 4. Test FesPlayerPreference SetAndSavePlayerStorageV1
        fes_key = f"FesPrefKey--{uuid.uuid4().hex[:6]}"
        fes_req = fes_player_preference_pb2.FesPlayerPreferenceSetAndSavePlayerStorageV1.Request(
            entries=[Entry(player_id=test_player_id, key=fes_key, value=b"fes_pref_data")],
            next_revision="rev-pg-004",
        )
        fes_resp = await fes_pref_service.SetAndSavePlayerStorageV1(fes_req, ctx)  # type: ignore[arg-type]
        assert fes_resp.revision == "rev-pg-004"

    finally:
        # Restore original settings
        settings.db_url = original_db_url
        reset_engine()
