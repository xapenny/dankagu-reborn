"""Reconstruction spot responses must be per-caller, and prizes per-player.

Two hazards this pins down:

* ``GetAvailableV1`` returned the cached protobuf message itself, so every player held
  the *same* object; anything that mutates a response would be visible to everyone.
* ``ReceivePrizesV1`` fell back to the literal player id ``"dankagu-reborn-user"`` when
  no session was present, so an unauthenticated call read and wrote one shared
  account's prizes.
"""

import uuid

import grpc
import pytest
from player_factory import ensure_player

from dankagu.core.database import get_sessionmaker
from dankagu.core.session import reset_current_player, set_current_player
from dankagu.grpc.generated.takasho.schema.fes.player_api import reconstruction_spot_pb2
from dankagu.models.inventory import PlayerInventoryItem
from dankagu.services.reconstruction_spot import ReconstructionSpotService


class DummyContext:
    def __init__(self) -> None:
        self.code: grpc.StatusCode | None = None

    async def abort(self, code: grpc.StatusCode, details: str) -> None:
        self.code = code
        raise grpc.aio.AioRpcError(
            code=code, initial_metadata=(), trailing_metadata=(), details=details
        )


@pytest.mark.asyncio
async def test_get_available_hands_out_independent_messages() -> None:
    servicer = ReconstructionSpotService()
    ctx = DummyContext()
    request = reconstruction_spot_pb2.ReconstructionSpotGetAvailableV1.Request()

    first = await servicer.GetAvailableV1(request, ctx)  # type: ignore[arg-type]
    second = await servicer.GetAvailableV1(request, ctx)  # type: ignore[arg-type]

    assert first is not second
    cached = servicer.cached_response
    assert cached is not None

    before = len(cached.spots)
    first.spots.clear()
    assert len(servicer.cached_response.spots) == before, "a caller mutated the shared cache"
    assert len(second.spots) == before


@pytest.mark.asyncio
async def test_receive_prizes_requires_a_player_session() -> None:
    servicer = ReconstructionSpotService()
    ctx = DummyContext()
    reset_current_player(set_current_player(None, None))

    request = reconstruction_spot_pb2.ReconstructionSpotReceivePrizesV1.Request()
    with pytest.raises(grpc.aio.AioRpcError):
        await servicer.ReceivePrizesV1(request, ctx)  # type: ignore[arg-type]

    assert ctx.code == grpc.StatusCode.UNAUTHENTICATED

    async with get_sessionmaker()() as session:
        shared = (
            await session.execute(
                PlayerInventoryItem.__table__.select().where(
                    PlayerInventoryItem.player_id == "dankagu-reborn-user"
                )
            )
        ).all()
    assert shared == [], "the shared fallback account must never receive prizes"


@pytest.mark.asyncio
async def test_prizes_belong_to_the_calling_player_only() -> None:
    player_a = await ensure_player(f"recon-a-{uuid.uuid4()}")
    player_b = await ensure_player(f"recon-b-{uuid.uuid4()}")
    servicer = ReconstructionSpotService()
    ctx = DummyContext()

    tokens_a = set_current_player(player_a, "tok-recon-a")
    try:
        first = await servicer.ReceivePrizesV1(  # type: ignore[arg-type]
            reconstruction_spot_pb2.ReconstructionSpotReceivePrizesV1.Request(), ctx
        )
        assert len(first.inventories) == 1
        assert first.inventories[0].player_id == player_a
    finally:
        reset_current_player(tokens_a)

    tokens_b = set_current_player(player_b, "tok-recon-b")
    try:
        second = await servicer.ReceivePrizesV1(  # type: ignore[arg-type]
            reconstruction_spot_pb2.ReconstructionSpotReceivePrizesV1.Request(), ctx
        )
        # Player B gets their own prize, never a view of player A's inventory.
        assert all(inv.player_id == player_b for inv in second.inventories)
    finally:
        reset_current_player(tokens_b)

    async with get_sessionmaker()() as session:
        stolen = (
            await session.execute(
                PlayerInventoryItem.__table__.select().where(
                    PlayerInventoryItem.player_id == player_b,
                    PlayerInventoryItem.search_label == "CoopProgressItem",
                )
            )
        ).all()
    assert len(stolen) == 1
