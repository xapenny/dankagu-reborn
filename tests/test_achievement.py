"""Tests for Achievement and PlayerAchievement services."""

import time
import uuid

import pytest
from player_factory import ensure_player

from dankagu.core.session import set_current_player
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    achievement_pb2,
    player_achievement_pb2,
)
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_storage.v2_pb2 import (
    Entry,
)
from dankagu.services.achievement import AchievementService, PlayerAchievementService


class MockContext:
    def __init__(self) -> None:
        self.aborted = False

    async def abort(self, code: object, details: str = "") -> None:
        self.aborted = True
        import grpc

        raise grpc.RpcError(f"Aborted: {code} - {details}")


@pytest.mark.asyncio
async def test_legacy_achievement_service() -> None:
    service = AchievementService()
    ctx = MockContext()

    # GetAvailableV1
    req_avail = achievement_pb2.AchievementGetAvailableV1.Request()
    res_avail = await service.GetAvailableV1(req_avail, ctx)  # type: ignore[arg-type]
    assert res_avail is not None

    # GetAvailableByIDsV1
    req_by_ids = achievement_pb2.AchievementGetAvailableByIDsV1.Request(
        achievement_ids=["ach_001", "ach_002"]
    )
    res_by_ids = await service.GetAvailableByIDsV1(req_by_ids, ctx)  # type: ignore[arg-type]
    assert len(res_by_ids.achievements) == 2
    assert res_by_ids.achievements[0].achievement_id == "ach_001"
    assert res_by_ids.achievements[0].unlock is True


@pytest.mark.asyncio
async def test_player_achievement_service() -> None:
    service = PlayerAchievementService()
    ctx = MockContext()
    pid = f"test-player-ach-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid)
    set_current_player(pid)

    # 1. GetAvailableV1
    req_avail = player_achievement_pb2.PlayerAchievementGetAvailableV1.Request()
    res_avail = await service.GetAvailableV1(req_avail, ctx)  # type: ignore[arg-type]
    assert res_avail is not None

    # 2. GetAvailableByIDsV1
    req_by_ids = player_achievement_pb2.PlayerAchievementGetAvailableByIDsV1.Request(
        achievement_ids=["ach_p1", "ach_p2"]
    )
    res_by_ids = await service.GetAvailableByIDsV1(req_by_ids, ctx)  # type: ignore[arg-type]
    assert len(res_by_ids.player_achievements) == 2
    assert res_by_ids.player_achievements[0].achievement.achievement_id == "ach_p1"
    assert res_by_ids.player_achievements[0].achievement.unlock is True

    # 3. UnlockV1
    req_unlock = player_achievement_pb2.PlayerAchievementUnlockV1.Request(
        achievement_ids=["ach_p1"]
    )
    res_unlock = await service.UnlockV1(req_unlock, ctx)  # type: ignore[arg-type]
    assert len(res_unlock.player_achievements) == 1
    assert len(res_unlock.inventories) == 1
    assert res_unlock.inventories[0].player_id == pid

    # 4. UnlockAndSavePlayerStorageV1
    now = int(time.time())
    entry = Entry(
        player_id=pid,
        key="AchievementStatus--1",
        value=b"\x08\x01",
        created_at=now,
        updated_at=now,
    )
    req_unlock_storage = (
        player_achievement_pb2.PlayerAchievementUnlockAndSavePlayerStorageV1.Request(
            achievement_ids=["ach_p2"],
            entries=[entry],
            next_revision="rev-test-1",
        )
    )
    res_unlock_storage = await service.UnlockAndSavePlayerStorageV1(
        req_unlock_storage,
        ctx,  # type: ignore[arg-type]
    )
    assert len(res_unlock_storage.player_achievements) == 1
    assert len(res_unlock_storage.entries) == 1
    assert res_unlock_storage.entries[0].key == "AchievementStatus--1"
    assert len(res_unlock_storage.inventories) == 1
    assert res_unlock_storage.revision is not None
