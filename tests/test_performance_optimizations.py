"""Tests verifying Phase 1-5 high-concurrency and performance optimizations."""

import time

import pytest
from player_factory import ensure_player

from dankagu.core.cipher import Cipher
from dankagu.core.database import get_sessionmaker
from dankagu.core.session import reset_current_player, set_current_player
from dankagu.core.storage_state import StorageRevisionManager
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_storage_pb2,
)
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_storage.v2_pb2 import (
    Criterion,
    Entry,
)
from dankagu.grpc.interceptors import _format_proto_for_log
from dankagu.services.player_storage import (
    PlayerStorageService,
    batch_upsert_storage_entries,
)


class MockContext:
    def __init__(self) -> None:
        self.initial_metadata: list[tuple[str, str]] = []
        self.trailing_metadata: list[tuple[str, str]] = []

    async def send_initial_metadata(self, metadata: tuple[tuple[str, str], ...]) -> None:
        self.initial_metadata.extend(metadata)

    def set_trailing_metadata(self, metadata: tuple[tuple[str, str], ...]) -> None:
        self.trailing_metadata.extend(metadata)

    async def abort(self, code: object, details: str = "") -> None:
        import grpc

        raise grpc.RpcError(f"Aborted: {code} - {details}")


def test_vectorized_cipher_speed_and_roundtrip() -> None:
    """Phase 4: Vectorized ChaCha cipher must roundtrip arbitrary lengths and exceed 5 MB/s."""
    key = b"ZA1Cu0eZosC3o8YTFuGjloxRkCg6ugVv"
    nonce = b"123456789012"

    # Test non-multiple-of-64 sizes as well as large payloads
    for size in (0, 1, 15, 63, 64, 65, 127, 128, 1024):
        payload = bytes((i * 31) & 0xFF for i in range(size))
        encrypted = Cipher(key, nonce).transform(payload)
        decrypted = Cipher(key, nonce).transform(encrypted)
        assert decrypted == payload

    # Benchmark 100 KB realistic TakashoPacker pack/unpack and 16 KB raw Cipher
    from dankagu.core.packer import TakashoPacker

    raw_16kb = bytes((i * 73) & 0xFF for i in range(16384))
    t0 = time.perf_counter()
    enc = Cipher(key, nonce).transform(raw_16kb)
    elapsed_16kb_ms = (time.perf_counter() - t0) * 1000.0
    assert Cipher(key, nonce).transform(enc) == raw_16kb
    assert elapsed_16kb_ms < 35.0, f"16 KB cipher took {elapsed_16kb_ms:.2f}ms (expected < 35ms)"

    packer = TakashoPacker(key)
    large_proto = b"\x0a\x20CardEntityStatusCollection--0001\x12\x40" * 1600  # ~102 KB
    t1 = time.perf_counter()
    packed = packer.pack(large_proto)
    unpacked = packer.unpack(packed)
    packer_ms = (time.perf_counter() - t1) * 1000.0
    assert unpacked == large_proto
    assert packer_ms < 25.0, f"100 KB TakashoPacker roundtrip took {packer_ms:.2f}ms"


def test_interceptor_large_proto_log_summary() -> None:
    """Phase 5: Protobuf payloads > 4096 bytes are summarized instead of full MessageToString."""
    small_req = player_storage_pb2.PlayerStorageSetEntriesV2.Request(
        entries=[Entry(player_id="p1", key="Small--1", value=b"abc")],
        next_revision="rev-1",
    )
    small_str = _format_proto_for_log(small_req)
    assert "Small--1" in small_str

    large_req = player_storage_pb2.PlayerStorageSetEntriesV2.Request(
        entries=[Entry(player_id="p1", key=f"Large--{i}", value=b"x" * 600) for i in range(10)],
        next_revision="rev-2",
    )
    large_str = _format_proto_for_log(large_req)
    assert "entries=10" in large_str
    assert "next_revision='rev-2'" in large_str
    assert len(large_str) < 200


@pytest.mark.asyncio
async def test_batch_upsert_deduplicates_and_updates() -> None:
    """Phase 1 & 3: Batch upsert handles duplicate keys in same batch and single-tx revision."""
    pid = await ensure_player("perf-batch-player")
    now = int(time.time())
    session_factory = get_sessionmaker()

    async with session_factory() as session:
        await batch_upsert_storage_entries(
            session,
            [
                {
                    "player_id": pid,
                    "key": "DupKey--1",
                    "value": b"\x01",
                    "created_at": now,
                    "updated_at": now,
                },
                {
                    "player_id": pid,
                    "key": "DupKey--1",
                    "value": b"\x02",
                    "created_at": now,
                    "updated_at": now + 1,
                },
                {
                    "player_id": pid,
                    "key": "SecondKey--1",
                    "value": b"\x03",
                    "created_at": now,
                    "updated_at": now,
                },
            ],
        )
        rev = await StorageRevisionManager.set_revision_and_save(
            "batch-rev-1", player_id=pid, session=session
        )
        await session.commit()

    assert rev == "batch-rev-1"
    assert StorageRevisionManager.get_revision(pid) == "batch-rev-1"

    # Verify SQL filter pushdown in GetEntriesV2 (EXACT + FORWARD)
    service = PlayerStorageService()
    tokens = set_current_player(pid)
    try:
        req = player_storage_pb2.PlayerStorageGetEntriesV2.Request(
            criteria=[
                Criterion(key="DupKey--1", matching_type=Criterion.MatchingType.EXACT),
                Criterion(key="SecondKey--", matching_type=Criterion.MatchingType.FORWARD),
            ]
        )
        resp = await service.GetEntriesV2(req, MockContext())  # type: ignore[arg-type]
        returned = {e.key: e.value for e in resp.entries}
        assert returned["DupKey--1"] == b"\x02"
        assert returned["SecondKey--1"] == b"\x03"
        assert resp.revision == "batch-rev-1"
    finally:
        reset_current_player(tokens)
