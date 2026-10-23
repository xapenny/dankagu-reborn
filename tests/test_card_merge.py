"""Tests for authentic duplicate card preservation across storage, inventory, and gacha.

Danmaku Kagura client natively supports and expects duplicate card copies:
- Each drawn copy is assigned a unique instance ID (CardStatus--<instance_id>).
- Duplicate copies can be decomposed via the Card Decompose ("お別れ") system.
- Awakening (开花 / 深入度) is performed using growth items, NOT by merging duplicate cards.
- The backend preserves all duplicate card entries as independent records in database
  and gRPC responses.
"""

import uuid
from dataclasses import dataclass

import pytest

from dankagu.core.database import get_sessionmaker
from dankagu.core.economy import (
    CARD_FIELD_CARD_ID,
    _parse_card_fields,
    encode_card_status,
)
from dankagu.core.identity import resolve_or_create_player
from dankagu.core.session import reset_current_player, set_current_player
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_inventory_pb2,
    player_storage_pb2,
)
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_storage.v2_pb2 import (
    Criterion,
)
from dankagu.models.player import Player
from dankagu.models.storage import PlayerStorageEntry
from dankagu.services.player_inventory import PlayerInventoryService
from dankagu.services.player_storage import PlayerStorageService


class DummyContext:
    async def abort(self, code: object, details: str) -> None:  # pragma: no cover
        raise AssertionError(f"unexpected abort: {code} {details}")


def _card_entry(instance_id: int, card_id: int, awake_lv: int = 0, exp: int = 0) -> bytes:
    return encode_card_status(
        instance_id=instance_id,
        card_id=card_id,
        created_at=1,
        updated_at=1,
        awake_lv=awake_lv,
        exp=exp,
        favorite=1,
    )


@pytest.mark.asyncio
async def test_two_copies_in_one_set_entries_request_are_preserved() -> None:
    """When a multi-pull produces two copies of the same card, both entries are stored."""
    async with get_sessionmaker()() as session:
        player, _ = await resolve_or_create_player(
            session, user_id=None, device_account=f"card-test-{uuid.uuid4()}"
        )
    player_id: str = player.id
    tokens = set_current_player(player_id, "tok-cards")
    try:
        servicer = PlayerStorageService()
        request = player_storage_pb2.PlayerStorageSetEntriesV2.Request()
        request.next_revision = "1"
        for instance_id in (7001, 7002):
            entry = request.entries.add()
            entry.key = f"CardStatus--{instance_id}"
            entry.value = _card_entry(instance_id, 100415)
        # A card of a different id in the same request
        other = request.entries.add()
        other.key = "CardStorage--9999"
        other.value = _card_entry(9999, 100416)

        response = await servicer.SetEntriesV2(request, DummyContext())  # type: ignore[arg-type]
        assert response.revision == "1"

        async with get_sessionmaker()() as session:
            rows = (
                await session.execute(
                    PlayerStorageEntry.__table__.select().where(
                        PlayerStorageEntry.player_id == player_id
                    )
                )
            ).all()
    finally:
        reset_current_player(tokens)

    by_key = {row.key: row.value for row in rows}
    card_keys = [key for key in by_key if key.startswith("CardStatus--")]
    # Both card copies must exist as independent entries
    assert len(card_keys) == 2, f"both card copies must be stored, got {card_keys}"
    assert "CardStatus--7001" in by_key
    assert "CardStatus--7002" in by_key
    assert "CardStorage--9999" in by_key

    # Response must echo both stored keys
    response_keys = {entry.key for entry in response.entries}
    assert "CardStatus--7001" in response_keys
    assert "CardStatus--7002" in response_keys
    assert "CardStorage--9999" in response_keys


@pytest.mark.asyncio
async def test_writing_a_card_does_not_drop_existing_duplicates() -> None:
    """Players who already hold multiple copies of a card retain all of them when saving."""
    async with get_sessionmaker()() as session:
        player: Player
        player, _ = await resolve_or_create_player(
            session, user_id=None, device_account=f"card-repair-{uuid.uuid4()}"
        )
        player_id = player.id
        for instance_id in (8001, 8002, 8003):
            session.add(
                PlayerStorageEntry(
                    player_id=player_id,
                    key=f"CardStatus--{instance_id}",
                    value=_card_entry(instance_id, 100500),
                    created_at=1,
                    updated_at=1,
                )
            )
        await session.commit()

    tokens = set_current_player(player_id, "tok-repair")
    try:
        request = player_storage_pb2.PlayerStorageSetEntriesV2.Request()
        request.next_revision = "2"
        entry = request.entries.add()
        entry.key = "CardStatus--8004"
        entry.value = _card_entry(8004, 100500)
        await PlayerStorageService().SetEntriesV2(request, DummyContext())  # type: ignore[arg-type]
    finally:
        reset_current_player(tokens)

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                PlayerStorageEntry.__table__.select().where(
                    PlayerStorageEntry.player_id == player_id,
                    PlayerStorageEntry.key.like("CardStatus--%"),
                )
            )
        ).all()

    # All 4 copies must be preserved in DB
    assert len(rows) == 4, f"all 4 copies must be preserved, got {[r.key for r in rows]}"
    keys = {r.key for r in rows}
    assert keys == {
        "CardStatus--8001",
        "CardStatus--8002",
        "CardStatus--8003",
        "CardStatus--8004",
    }


@pytest.mark.asyncio
async def test_player_inventory_receive_preserves_duplicate_cards() -> None:
    """Claiming gift/inventory items containing duplicate cards saves all copies."""
    async with get_sessionmaker()() as session:
        player, _ = await resolve_or_create_player(
            session, user_id=None, device_account=f"inv-card-{uuid.uuid4()}"
        )
    player_id = player.id
    tokens = set_current_player(player_id, "tok-inv")
    try:
        servicer = PlayerInventoryService()
        request = player_inventory_pb2.PlayerInventoryReceiveV1.Request()
        request.next_revision = "rev-1"
        for inst_id in (9501, 9502):
            e = request.entries.add()
            e.key = f"CardStatus--{inst_id}"
            e.value = _card_entry(inst_id, 100999)

        response = await servicer.ReceiveV1(request, DummyContext())  # type: ignore[arg-type]

        async with get_sessionmaker()() as session:
            rows = (
                await session.execute(
                    PlayerStorageEntry.__table__.select().where(
                        PlayerStorageEntry.player_id == player_id,
                        PlayerStorageEntry.key.like("CardStatus--%"),
                    )
                )
            ).all()

        assert len(rows) == 2, f"both card entries must be stored, got {[r.key for r in rows]}"
        row_keys = {r.key for r in rows}
        assert row_keys == {"CardStatus--9501", "CardStatus--9502"}

        # Check response entries
        resp_card_keys = {e.key for e in response.entries if e.key.startswith("CardStatus--")}
        assert resp_card_keys == {"CardStatus--9501", "CardStatus--9502"}
    finally:
        reset_current_player(tokens)


@pytest.mark.asyncio
async def test_get_entries_v2_returns_all_duplicate_card_rows() -> None:
    """GetEntriesV2 returns all duplicate card rows stored in the database."""
    async with get_sessionmaker()() as session:
        player, _ = await resolve_or_create_player(
            session, user_id=None, device_account=f"get-dup-{uuid.uuid4()}"
        )
        player_id = player.id
        # Seed 3 duplicate rows in DB
        for inst_id in (6001, 6002, 6003):
            session.add(
                PlayerStorageEntry(
                    player_id=player_id,
                    key=f"CardStatus--{inst_id}",
                    value=_card_entry(inst_id, 100777),
                    created_at=1,
                    updated_at=1,
                )
            )
        await session.commit()

    tokens = set_current_player(player_id, "tok-get")
    try:
        request = player_storage_pb2.PlayerStorageGetEntriesV2.Request()
        c = request.criteria.add()
        c.key = "CardStatus--"
        c.matching_type = Criterion.MatchingType.FORWARD
        servicer = PlayerStorageService()
        response = await servicer.GetEntriesV2(request, DummyContext())  # type: ignore[arg-type]

        card_entries = [e for e in response.entries if e.key.startswith("CardStatus--")]
        assert len(card_entries) == 3, (
            f"GetEntriesV2 must return all 3 copies, got {len(card_entries)}"
        )
        returned_keys = {e.key for e in card_entries}
        assert returned_keys == {"CardStatus--6001", "CardStatus--6002", "CardStatus--6003"}
        for e in card_entries:
            fields = _parse_card_fields(e.value)
            assert fields[CARD_FIELD_CARD_ID] == 100777
    finally:
        reset_current_player(tokens)


def test_roll_card_respects_rarity_rates_and_pickups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gacha _roll_card rolls properly based on rates and banner configurations."""
    import dankagu.services.loot_box as lb

    @dataclass
    class FakeCard:
        id: int
        rarity: int

    @dataclass
    class FakeBanner:
        element: int
        pickups: list[int]

    cards_map = {
        100001: FakeCard(id=100001, rarity=4),
        100002: FakeCard(id=100002, rarity=4),
        100101: FakeCard(id=100101, rarity=3),
        100201: FakeCard(id=100201, rarity=2),
    }

    monkeypatch.setattr(
        lb,
        "_CARDS_BY_RARITY",
        {
            4: [100001, 100002],
            3: [100101],
            2: [100201],
        },
    )
    monkeypatch.setattr(lb, "_CARDS_BY_ID", cards_map)
    monkeypatch.setattr(lb, "_BY_ELEMENT", {})
    monkeypatch.setattr(lb, "_BANNERS", {159: FakeBanner(element=0, pickups=[100001])})

    # Guaranteed SSR must return an SSR
    for _ in range(20):
        ssr = lb._roll_card(159, is_guaranteed_ssr=True)
        assert ssr in (100001, 100002)

    # Guaranteed SR must return an SR or SSR
    for _ in range(20):
        sr = lb._roll_card(159, is_guaranteed_sr=True)
        assert sr in (100001, 100002, 100101)

    # Normal roll returns any card from candidate pool
    for _ in range(50):
        c = lb._roll_card(159)
        assert c in (100001, 100002, 100101, 100201)
