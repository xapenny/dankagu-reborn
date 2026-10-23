"""Error paths must fail visibly instead of answering with made-up state.

Regression guards for the class of bug where a handler catches an exception and
returns a normal-looking response: the client cannot tell it apart from "the
player really has no data", so progress silently disappears 鈥?or, as in the
mailbox case below, a guessed reward is granted.
"""

import json
import uuid
from typing import Any

import grpc
import pytest
from sqlalchemy import select

from dankagu.core.database import get_sessionmaker
from dankagu.core.economy import CorruptedPlayerValueError, get_player_key_value
from dankagu.core.session import set_current_player
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_inventory_pb2,
    player_storage_pb2,
)
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_storage import (
    v2_pb2 as storage_v2_pb2,
)
from dankagu.models.inventory import PlayerInventoryItem
from dankagu.models.player import Player
from dankagu.models.storage import PlayerStorageEntry
from dankagu.models.wallet import WalletBalance
from dankagu.services.player_inventory import PlayerInventoryService
from dankagu.services.player_storage import PlayerStorageService


class DummyContext:
    """gRPC servicer context that records the abort it was given."""

    def __init__(self) -> None:
        self.aborted = False
        self.code: grpc.StatusCode | None = None
        self.details: str | None = None

    async def abort(self, code: grpc.StatusCode, details: str) -> None:
        self.aborted = True
        self.code = code
        self.details = details
        raise grpc.aio.AioRpcError(
            code=code, initial_metadata=(), trailing_metadata=(), details=details
        )


async def _make_player() -> Player:
    """Register a fresh player through the same path the client uses."""
    from dankagu.core.identity import resolve_or_create_player

    async with get_sessionmaker()() as session:
        player, _ = await resolve_or_create_player(
            session, user_id=None, device_account=f"failure-path-{uuid.uuid4()}"
        )
        return player


@pytest.mark.asyncio
async def test_corrupt_mailbox_payload_never_grants_a_guessed_reward() -> None:
    """An unreadable mailbox item must abort, not hand out `system_resource_num or 200`."""
    player = await _make_player()
    set_current_player(player.id, "tok-failure")
    servicer = PlayerInventoryService()
    ctx = DummyContext()

    async with get_sessionmaker()() as session:
        item = PlayerInventoryItem(
            id=f"broken-{uuid.uuid4()}",
            player_id=player.id,
            item_type=0,
            schema_key="mail",
            value=b"this-is-not-json",
            route=0,
            message="",
            search_label="BROKEN",
            opened_at=0,
            expired_at=0,
            system_resource_name="FREE_STONE",
            system_resource_num=200,
        )
        session.add(item)
        await session.commit()
        wallet_before = (await session.get(WalletBalance, player.id)).free_stones

    request = player_inventory_pb2.PlayerInventoryReceiveV1.Request()
    request.delete_inventory_ids.append(item.id)

    with pytest.raises(grpc.aio.AioRpcError):
        await servicer.ReceiveV1(request, ctx)

    assert ctx.code == grpc.StatusCode.INTERNAL

    async with get_sessionmaker()() as session:
        wallet_after = (await session.get(WalletBalance, player.id)).free_stones
        stored = await session.get(PlayerInventoryItem, item.id)
    assert wallet_after == wallet_before, "no reward may be granted from an unreadable payload"
    assert stored is not None and stored.is_claimed is False


@pytest.mark.asyncio
async def test_corrupt_key_value_raises_instead_of_defaulting() -> None:
    """A present-but-unreadable balance must raise, not silently read as 0."""
    player = await _make_player()

    async with get_sessionmaker()() as session:
        session.add(
            PlayerStorageEntry(
                player_id=player.id,
                key="PlayerKeyValue:point",
                value=b"\x00\xff-not-an-int",
                created_at=0,
                updated_at=0,
            )
        )
        await session.commit()

        with pytest.raises(CorruptedPlayerValueError):
            await get_player_key_value(session, player.id, "point", default=8612)


@pytest.mark.asyncio
async def test_absent_key_value_still_returns_the_default() -> None:
    """Absence is normal (new player); only corruption is an error."""
    player = await _make_player()
    async with get_sessionmaker()() as session:
        assert await get_player_key_value(session, player.id, "point", default=8612) == 8612


@pytest.mark.asyncio
async def test_storage_write_failure_is_reported_as_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing persistence step must surface as INTERNAL, not as a happy response."""
    player = await _make_player()
    set_current_player(player.id, "tok-failure")
    servicer = PlayerStorageService()
    ctx = DummyContext()

    from dankagu.core import storage_state

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(
        storage_state.StorageRevisionManager, "set_revision_and_save", staticmethod(_boom)
    )

    request = player_storage_pb2.PlayerStorageSetEntriesV2.Request()
    entry = request.entries.add()
    entry.key = "PlayerBootStatus--1"
    entry.value = b"\x08\x01"

    with pytest.raises(grpc.aio.AioRpcError):
        await servicer.SetEntriesV2(request, ctx)

    assert ctx.code == grpc.StatusCode.INTERNAL
    assert "Failed to persist player storage entries" in (ctx.details or "")


@pytest.mark.asyncio
async def test_storage_read_failure_is_reported_as_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing read must not look like "this player has no entries"."""
    player = await _make_player()
    set_current_player(player.id, "tok-failure")
    servicer = PlayerStorageService()
    ctx = DummyContext()

    import dankagu.services.player_storage as storage_module

    class _BrokenSession:
        async def __aenter__(self) -> "_BrokenSession":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def get(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("database went away")

        async def execute(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("database went away")

    monkeypatch.setattr(storage_module, "get_sessionmaker", lambda: lambda: _BrokenSession())

    request = player_storage_pb2.PlayerStorageGetEntriesV2.Request()
    criterion = request.criteria.add()
    criterion.matching_type = storage_v2_pb2.Criterion.MatchingType.EXACT
    criterion.key = "PlayerBootStatus--1"

    with pytest.raises(grpc.aio.AioRpcError):
        await servicer.GetEntriesV2(request, ctx)

    assert ctx.code == grpc.StatusCode.INTERNAL


@pytest.mark.asyncio
async def test_mailbox_grant_still_works_for_a_valid_payload() -> None:
    """The happy path must keep granting exactly what the payload says."""
    player = await _make_player()
    set_current_player(player.id, "tok-failure")
    servicer = PlayerInventoryService()
    ctx = DummyContext()

    async with get_sessionmaker()() as session:
        item = PlayerInventoryItem(
            id=f"good-{uuid.uuid4()}",
            player_id=player.id,
            item_type=0,
            schema_key="mail",
            value=json.dumps({"Prefix": "VC", "Value": "FREE_STONE", "Count": 1500}).encode(),
            route=0,
            message="",
            search_label="WELCOME",
            opened_at=0,
            expired_at=0,
            system_resource_name="FREE_STONE",
            system_resource_num=0,
        )
        session.add(item)
        await session.commit()
        before = (await session.get(WalletBalance, player.id)).free_stones
        item_id = item.id

    request = player_inventory_pb2.PlayerInventoryReceiveV1.Request()
    request.delete_inventory_ids.append(item_id)
    await servicer.ReceiveV1(request, ctx)

    async with get_sessionmaker()() as session:
        after = (await session.get(WalletBalance, player.id)).free_stones
        claimed = (
            await session.execute(
                select(PlayerInventoryItem).where(PlayerInventoryItem.id == item_id)
            )
        ).scalar_one()
    assert after == before + 1500
    assert claimed.is_claimed is True
