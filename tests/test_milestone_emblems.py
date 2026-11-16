"""Tests for milestone emblem evaluation, automatic grants in SetEntriesV2,
and lazy backfill in GetEntriesV2.
"""

import time
import uuid

import pytest
from player_factory import ensure_player

from dankagu.core.database import get_sessionmaker
from dankagu.core.milestone_emblems import evaluate_milestone_emblems
from dankagu.core.ranking_rewards import extract_unlocked_emblem_ids
from dankagu.core.session import set_current_player
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import player_storage_pb2
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_storage.v2_pb2 import (
    Criterion,
    Entry,
)
from dankagu.models.storage import PlayerStorageEntry
from dankagu.services.box_loot_box import encode_raw_protobuf
from dankagu.services.player_storage import (
    PlayerStorageService,
)


class MockContext:
    def __init__(self) -> None:
        self.aborted = False

    async def abort(self, code: object, details: str = "") -> None:
        self.aborted = True
        import grpc

        raise grpc.RpcError(f"Aborted: {code} - {details}")


def make_player_public_status(
    rate: int = 0, clears: list[int] | None = None, fcs: list[int] | None = None, sb: int = 0
) -> bytes:
    """Encode a dummy PlayerPublicStatus--1 protobuf."""
    clears = clears or [0, 0, 0, 0, 0]
    fcs = fcs or [0, 0, 0, 0, 0]
    from google.protobuf.internal import encoder

    clears_packed = bytearray()
    for c in clears:
        encoder._EncodeVarint(clears_packed.extend, c)

    fcs_packed = bytearray()
    for f in fcs:
        encoder._EncodeVarint(fcs_packed.extend, f)

    root = {
        1: [(0, 1)],
        2: [(2, bytes(clears_packed))],
        3: [(2, bytes(fcs_packed))],
        5: [(0, sb)],
        7: [(0, rate)],  # DanmakuRate
    }
    return encode_raw_protobuf(root)


def make_play_music_record(
    mid: int,
    lunatic_clear: int = 0,
    lunatic_fc: bool = False,
    lunatic_ab: bool = False,
) -> bytes:
    """Encode a dummy PlayMusicRecord--<mid> with 5 levels."""
    levels = []
    for diff in range(5):
        lvl_fields = {1: [(0, diff)]}
        if diff == 4:  # Lunatic
            if lunatic_clear > 0:
                lvl_fields[3] = [(0, lunatic_clear)]
            if lunatic_fc:
                lvl_fields[12] = [(0, 1)]
            if lunatic_ab:
                lvl_fields[13] = [(0, 1)]
        levels.append((2, encode_raw_protobuf(lvl_fields)))

    root = {
        1: [(0, mid)],
        8: levels,
    }
    return encode_raw_protobuf(root)


@pytest.mark.asyncio
async def test_evaluate_milestone_emblems_rate() -> None:
    # Rate 4000 should unlock 500, 1000, 1600, 2000, 3000, 4000
    blob = make_player_public_status(rate=4200)
    emblems = evaluate_milestone_emblems([("PlayerPublicStatus--1", blob)])
    assert 2000005 in emblems  # 500
    assert 2000006 in emblems  # 1000
    assert 2000007 in emblems  # 1600
    assert 2000008 in emblems  # 2000
    assert 2000031 in emblems  # 3000
    assert 2000032 in emblems  # 4000
    assert 2000033 not in emblems  # 5000 not yet


@pytest.mark.asyncio
async def test_evaluate_milestone_emblems_lunatic_all_brilliant() -> None:
    # Music 2 (Bad Apple!!) All Brilliant on Lunatic
    blob = make_play_music_record(mid=2, lunatic_clear=5, lunatic_fc=True, lunatic_ab=True)
    emblems = evaluate_milestone_emblems([("PlayMusicRecord--2", blob)])
    assert 2120021 in emblems  # Bad Apple Lunatic AB
    assert 2120022 in emblems  # Bad Apple Lunatic FC
    assert 2120023 in emblems  # Bad Apple Lunatic Clear


@pytest.mark.asyncio
async def test_evaluate_milestone_emblems_lunatic_fc_only() -> None:
    # Music 133 Lunatic FC only
    blob = make_play_music_record(mid=133, lunatic_clear=1, lunatic_fc=True, lunatic_ab=False)
    emblems = evaluate_milestone_emblems([("PlayMusicRecord--133", blob)])
    assert 2121331 not in emblems  # AB not unlocked
    assert 2121332 in emblems  # FC unlocked
    assert 2121333 in emblems  # Clear unlocked


@pytest.mark.asyncio
async def test_set_entries_auto_grant_rate_and_ab() -> None:
    service = PlayerStorageService()
    ctx = MockContext()
    pid = f"test-milestone-grant-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid)
    set_current_player(pid)

    pub_blob = make_player_public_status(rate=4000)
    music_blob = make_play_music_record(mid=2, lunatic_clear=1, lunatic_fc=True, lunatic_ab=True)

    req = player_storage_pb2.PlayerStorageSetEntriesV2.Request(
        next_revision="rev-1",
        entries=[
            Entry(key="PlayerPublicStatus--1", value=pub_blob),
            Entry(key="PlayMusicRecord--2", value=music_blob),
        ],
    )
    res = await service.SetEntriesV2(req, ctx)  # type: ignore[arg-type]

    # Verify UnlockEntityStatusCollection--8 was generated and returned in response
    keys = {e.key: e.value for e in res.entries}
    assert "UnlockEntityStatusCollection--8" in keys

    unlocked = extract_unlocked_emblem_ids(keys["UnlockEntityStatusCollection--8"])
    assert 2000032 in unlocked  # Rate 4000
    assert 2120021 in unlocked  # Music 2 AB


@pytest.mark.asyncio
async def test_get_entries_lazy_backfill() -> None:
    service = PlayerStorageService()
    ctx = MockContext()
    pid = f"test-milestone-backfill-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid)
    set_current_player(pid)
    now = int(time.time())

    # Pre-populate DB with PlayerPublicStatus (Rate 5000) and PlayMusicRecord (Music 6 AB)
    # but WITHOUT UnlockEntityStatusCollection--8
    pub_blob = make_player_public_status(rate=5000)
    music_blob = make_play_music_record(mid=6, lunatic_clear=1, lunatic_fc=True, lunatic_ab=True)

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        session.add(
            PlayerStorageEntry(
                player_id=pid,
                key="PlayerPublicStatus--1",
                value=pub_blob,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            PlayerStorageEntry(
                player_id=pid,
                key="PlayMusicRecord--6",
                value=music_blob,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    # Now client sends GetEntriesV2 requesting UnlockEntityStatusCollection--8
    req = player_storage_pb2.PlayerStorageGetEntriesV2.Request(
        criteria=[
            Criterion(
                key="UnlockEntityStatusCollection--8", matching_type=Criterion.MatchingType.EXACT
            )
        ]
    )
    res = await service.GetEntriesV2(req, ctx)  # type: ignore[arg-type]
    keys = {e.key: e.value for e in res.entries}

    assert "UnlockEntityStatusCollection--8" in keys
    unlocked = extract_unlocked_emblem_ids(keys["UnlockEntityStatusCollection--8"])

    # Both Rate 5000 and Music 6 AB should be automatically backfilled into UnlockEntityStatusCollection--8!
    assert 2000033 in unlocked  # Rate 5000
    assert 2000032 in unlocked  # Rate 4000
    assert 2120061 in unlocked  # Music 6 AB
    assert 2120062 in unlocked  # Music 6 FC
    assert 2120063 in unlocked  # Music 6 Clear


@pytest.mark.asyncio
async def test_spot_reconstruction_requires_100_percent() -> None:
    # cnst=4 (constructed) with reconst=80 (80% reconstructed) must NOT grant completion emblem
    spot_80 = encode_raw_protobuf({1: [(0, 1)], 8: [(0, 4)], 9: [(0, 80)]})
    assert 2000013 not in evaluate_milestone_emblems([("SpotStatus--1", spot_80)])

    # reconst=100 must grant completion emblem
    spot_100 = encode_raw_protobuf({1: [(0, 1)], 8: [(0, 4)], 9: [(0, 100)]})
    assert 2000013 in evaluate_milestone_emblems([("SpotStatus--1", spot_100)])


@pytest.mark.asyncio
async def test_set_entries_union_merges_unlock_8() -> None:
    from dankagu.services.player_storage import add_emblems_to_collection

    service = PlayerStorageService()
    ctx = MockContext()
    pid = f"test-milestone-union-{uuid.uuid4().hex[:8]}"
    await ensure_player(pid)
    set_current_player(pid)
    now = int(time.time())

    # Server already unlocked Rate 4000 (2000032) in DB
    db_u8_blob = add_emblems_to_collection(None, [2000032], now)
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        session.add(
            PlayerStorageEntry(
                player_id=pid,
                key="UnlockEntityStatusCollection--8",
                value=db_u8_blob,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    # Client later uploads stale UnlockEntityStatusCollection--8 containing only 1000001
    client_u8_blob = add_emblems_to_collection(None, [1000001], now)
    req = player_storage_pb2.PlayerStorageSetEntriesV2.Request(
        next_revision="rev-union-1",
        entries=[Entry(key="UnlockEntityStatusCollection--8", value=client_u8_blob)],
    )
    res = await service.SetEntriesV2(req, ctx)  # type: ignore[arg-type]

    keys = {e.key: e.value for e in res.entries}
    unlocked = extract_unlocked_emblem_ids(keys["UnlockEntityStatusCollection--8"])
    assert 1000001 in unlocked
    assert 2000032 in unlocked  # Must NOT be overwritten by stale client upload
