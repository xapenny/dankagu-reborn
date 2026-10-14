"""One card, one collection entry -- with the duplicate counter carrying the copies.

The client writes a ``CardStatus--<instance id>`` entry **per drawn copy**, so two copies
of a card in a single ten-pull arrived as two entries and a collection could end up
holding the same card up to four times (one live account had 97 card entries for 69
cards, 21 of them stored more than once). The write path now folds them together and
counts the copies.

These tests exercise the merge directly and through the servicer.
"""

import uuid

import pytest

from dankagu.core.database import get_sessionmaker
from dankagu.core.economy import (
    CARD_FIELD_BLOSSOM,
    CARD_FIELD_CARD_ID,
    CARD_FIELD_DUPLICATE,
    CARD_FIELD_EXP,
    _parse_card_fields,
    canonicalise_card_entries,
    encode_card_status,
)
from dankagu.core.identity import resolve_or_create_player
from dankagu.core.session import reset_current_player, set_current_player
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_storage_pb2,
)
from dankagu.models.player import Player
from dankagu.models.storage import PlayerStorageEntry
from dankagu.services.player_storage import PlayerStorageService


class DummyContext:
    async def abort(self, code: object, details: str) -> None:  # pragma: no cover
        raise AssertionError(f"unexpected abort: {code} {details}")


def _card_entry(
    instance_id: int, card_id: int, duplicate: int = 1, exp: int = 0, blossom: int = 1
) -> bytes:
    return encode_card_status(
        instance_id=instance_id,
        card_id=card_id,
        created_at=1,
        updated_at=1,
        blossom=blossom,
        exp=exp,
        duplicate=duplicate,
        status=1,
    )


def test_two_copies_in_one_request_become_one_entry_counting_two() -> None:
    """The reported case: the same card twice in one ten-pull."""
    first_key = f"CardStatus--{1000}"
    second_key = f"CardStatus--{1001}"

    plan = canonicalise_card_entries(
        existing={},
        incoming=[
            (first_key, _card_entry(1000, 100318)),
            (second_key, _card_entry(1001, 100318)),
        ],
        now=10,
    )

    assert plan is not None
    assert len(plan.writes) == 1, "the card must occupy exactly one entry"
    key, value = plan.writes[0]
    assert key == first_key, "the client's first key is kept"
    assert plan.drop_keys == [second_key]
    fields = _parse_card_fields(value)
    assert fields[CARD_FIELD_CARD_ID] == 100318
    assert fields[CARD_FIELD_DUPLICATE] == 2, "two copies must be counted, not stored twice"
    assert plan.collapsed == {100318: 2}


def test_a_new_copy_increments_the_stored_counter() -> None:
    """Drawing a card the player already owns adds to its counter."""
    stored_key = "CardStatus--500"
    plan = canonicalise_card_entries(
        existing={100318: (stored_key, _card_entry(500, 100318, duplicate=3))},
        incoming=[("CardStatus--2000", _card_entry(2000, 100318))],
        now=20,
    )

    assert plan is not None
    assert [key for key, _ in plan.writes] == [stored_key]
    fields = _parse_card_fields(plan.writes[0][1])
    assert fields[CARD_FIELD_DUPLICATE] == 4, "3 owned + 1 drawn"
    assert plan.drop_keys == ["CardStatus--2000"]


def test_three_copies_in_one_request() -> None:
    plan = canonicalise_card_entries(
        existing={},
        incoming=[
            ("CardStatus--1", _card_entry(1, 100281)),
            ("CardStatus--2", _card_entry(2, 100281)),
            ("CardStatus--3", _card_entry(3, 100281)),
        ],
        now=30,
    )
    assert plan is not None
    assert len(plan.writes) == 1
    assert _parse_card_fields(plan.writes[0][1])[CARD_FIELD_DUPLICATE] == 3
    assert sorted(plan.drop_keys) == ["CardStatus--2", "CardStatus--3"]


def test_non_card_entries_are_left_alone() -> None:
    plan = canonicalise_card_entries(
        existing={}, incoming=[("PlayerStatus--1", b"\x08\x01")], now=40
    )
    assert plan is None, "no card entries means the caller keeps its fast path"


def test_a_later_upgrade_is_carried_into_the_survivor() -> None:
    """The client levels a card under its own key; the merge must not lose the level."""
    plan = canonicalise_card_entries(
        existing={100318: ("CardStatus--500", _card_entry(500, 100318, exp=3))},
        incoming=[("CardStatus--2000", _card_entry(2000, 100318, exp=9))],
        now=50,
    )

    assert plan is not None
    fields = _parse_card_fields(plan.writes[0][1])
    assert fields[CARD_FIELD_EXP] == 9, "the higher experience must survive the merge"
    assert fields[CARD_FIELD_DUPLICATE] == 2


def test_the_strongest_copy_wins_within_a_request() -> None:
    """Two copies that differ in development: the developed one is kept."""
    plan = canonicalise_card_entries(
        existing={},
        incoming=[
            ("CardStatus--100", _card_entry(100, 100281)),
            ("CardStatus--200", _card_entry(200, 100281, exp=5, blossom=2)),
        ],
        now=60,
    )

    assert plan is not None
    fields = _parse_card_fields(plan.writes[0][1])
    assert (fields[CARD_FIELD_EXP], fields[CARD_FIELD_BLOSSOM]) == (5, 2)
    assert fields[CARD_FIELD_DUPLICATE] == 2


@pytest.mark.asyncio
async def test_the_servicer_stores_one_entry_and_reports_it() -> None:
    async with get_sessionmaker()() as session:
        player, _ = await resolve_or_create_player(
            session, user_id=None, device_account=f"card-merge-{uuid.uuid4()}"
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
        # A card of a different id in the same request must survive on its own.
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
    assert len(card_keys) == 1, f"the card must occupy one entry, got {card_keys}"
    assert _parse_card_fields(by_key[card_keys[0]])[CARD_FIELD_DUPLICATE] == 2
    assert "CardStorage--9999" in by_key

    # The response tells the client what was actually stored.
    response_keys = {entry.key for entry in response.entries}
    assert card_keys[0] in response_keys
    assert "CardStorage--9999" in response_keys


@pytest.mark.asyncio
async def test_writing_a_card_repairs_existing_duplicates() -> None:
    """Players who already carry duplicates are repaired when the card is next written."""
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

    assert len(rows) == 1, f"the duplicates must be gone, still have {[r.key for r in rows]}"
    fields = _parse_card_fields(rows[0].value)
    # Three stored copies (each counter said 1) plus the one just drawn.
    assert fields[CARD_FIELD_DUPLICATE] == 4
