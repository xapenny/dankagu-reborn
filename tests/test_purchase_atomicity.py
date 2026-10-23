"""Economy invariants: a purchase must charge exactly once, and only what master says.

Covers the money-side risks measured before this stage:
``deduct_stones`` read the balance and wrote it back with no row lock (two concurrent
purchases could both pass the funds check), ``transaction_id`` was only logged (a
client retry charged and rolled again), and the price came from splitting the product
id with a hardcoded 1500/150 fallback.
"""

import asyncio
import uuid

import grpc
import pytest
from sqlalchemy import select

from dankagu.core.database import get_sessionmaker
from dankagu.core.economy import get_or_create_wallet
from dankagu.core.identity import resolve_or_create_player
from dankagu.core.session import set_current_player
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import loot_box_pb2
from dankagu.models.master import MasterLootBoxProduct
from dankagu.models.player import Player
from dankagu.models.receipt import PurchaseReceipt
from dankagu.models.wallet import WalletBalance
from dankagu.services.loot_box import LootBoxService


class DummyContext:
    def __init__(self) -> None:
        self.code: grpc.StatusCode | None = None

    async def abort(self, code: grpc.StatusCode, details: str) -> None:
        self.code = code
        raise grpc.aio.AioRpcError(
            code=code, initial_metadata=(), trailing_metadata=(), details=details
        )


async def _make_player() -> Player:
    async with get_sessionmaker()() as session:
        player, _ = await resolve_or_create_player(
            session, user_id=None, device_account=f"economy-{uuid.uuid4()}"
        )
        return player


def _request(product_id: str, transaction_id: str, purchase_num: int = 0):
    request = loot_box_pb2.LootBoxV3PurchaseV1.Request()
    request.loot_box_product_id = product_id
    request.transaction_id = transaction_id
    if purchase_num:
        request.purchase_num = purchase_num
    return request


async def _seed_master_product(product_id: str, amount: int, resource_type: str | None, lot: int):
    async with get_sessionmaker()() as session:
        row = await session.get(MasterLootBoxProduct, product_id)
        data = {
            "lootProduct": {
                "lootBoxProductId": product_id,
                "totalLotNum": lot,
                "consumeResourceSets": [
                    {
                        "consumeResourceSetId": f"{product_id}_RSet1",
                        "resourceType": resource_type,
                        "amount": str(amount),
                    }
                ],
            }
        }
        if row is None:
            session.add(MasterLootBoxProduct(id=product_id, data=data))
        else:
            row.data = data
        await session.commit()


async def _wallet(player_id: str) -> tuple[int, int]:
    async with get_sessionmaker()() as session:
        wallet = await session.get(WalletBalance, player_id)
        assert wallet is not None
        return wallet.free_stones, wallet.paid_stones


@pytest.mark.asyncio
async def test_repeated_transaction_id_charges_once_and_replays_the_same_cards() -> None:
    player = await _make_player()
    set_current_player(player.id, "tok-economy")
    servicer = LootBoxService()
    ctx = DummyContext()
    transaction_id = f"tx-{uuid.uuid4()}"

    first = await servicer.PurchaseV1(_request("G_1_10_TT_L", transaction_id), ctx)  # type: ignore[arg-type]
    wallet_after_first = await _wallet(player.id)
    first_cards = [inv.value for inv in first.player_inventories]
    assert len(first_cards) == 10

    second = await servicer.PurchaseV1(_request("G_1_10_TT_L", transaction_id), ctx)  # type: ignore[arg-type]
    wallet_after_second = await _wallet(player.id)
    second_cards = [inv.value for inv in second.player_inventories]

    assert wallet_after_second == wallet_after_first, "a retry must not charge again"
    assert second_cards == first_cards, "a retry must replay the same cards"

    async with get_sessionmaker()() as session:
        receipts = (
            (
                await session.execute(
                    select(PurchaseReceipt).where(PurchaseReceipt.player_id == player.id)
                )
            )
            .scalars()
            .all()
        )
    assert len(receipts) == 1


@pytest.mark.asyncio
async def test_a_distinct_transaction_is_a_new_charge() -> None:
    player = await _make_player()
    set_current_player(player.id, "tok-economy")
    servicer = LootBoxService()
    ctx = DummyContext()

    await servicer.PurchaseV1(_request("G_1_10_TT_L", f"tx-{uuid.uuid4()}"), ctx)  # type: ignore[arg-type]
    first = await _wallet(player.id)
    await servicer.PurchaseV1(_request("G_1_10_TT_L", f"tx-{uuid.uuid4()}"), ctx)  # type: ignore[arg-type]
    second = await _wallet(player.id)

    assert second[0] == first[0] - 1500


@pytest.mark.asyncio
async def test_wallet_row_is_locked_while_a_spend_is_in_flight() -> None:
    """The spend path must take a row lock; without it concurrent buys both pass."""
    player = await _make_player()

    session_factory = get_sessionmaker()
    async with session_factory() as first_session:
        wallet = await get_or_create_wallet(first_session, player.id, for_update=True)
        wallet.free_stones -= 100

        async def _second_reader() -> int:
            async with session_factory() as second_session:
                locked = await get_or_create_wallet(second_session, player.id, for_update=True)
                return locked.free_stones

        pending = asyncio.create_task(_second_reader())
        await asyncio.sleep(0.5)
        assert not pending.done(), "the second session got the wallet row while the first held it"

        await first_session.commit()
        assert await asyncio.wait_for(pending, timeout=10) == 59900


@pytest.mark.asyncio
async def test_master_price_wins_over_the_built_in_heuristic() -> None:
    product_id = f"G_9_{uuid.uuid4().hex[:4]}_VC_L"
    await _seed_master_product(product_id, amount=250, resource_type=None, lot=1)

    player = await _make_player()
    set_current_player(player.id, "tok-economy")
    servicer = LootBoxService()
    ctx = DummyContext()

    await servicer.PurchaseV1(_request(product_id, f"tx-{uuid.uuid4()}"), ctx)  # type: ignore[arg-type]
    free, _paid = await _wallet(player.id)
    assert free == 60000 - 250


@pytest.mark.asyncio
async def test_ticket_product_is_not_handed_out_for_free() -> None:
    """``G_1_10_TT_L`` consumes the tutorial key (amount -1); that is not a price."""
    player = await _make_player()
    set_current_player(player.id, "tok-economy")
    servicer = LootBoxService()
    ctx = DummyContext()

    await servicer.PurchaseV1(_request("G_1_10_TT_L", f"tx-{uuid.uuid4()}"), ctx)  # type: ignore[arg-type]
    free, _paid = await _wallet(player.id)
    assert free == 60000 - 1500, "a non-positive master amount must not make a ten-pull free"


@pytest.mark.asyncio
async def test_master_pull_count_overrides_the_client() -> None:
    """A client asking for 1 pull of a 10-pull product still gets (and pays for) 10."""
    product_id = f"G_8_{uuid.uuid4().hex[:4]}_VC_L"
    await _seed_master_product(product_id, amount=1500, resource_type=None, lot=10)

    player = await _make_player()
    set_current_player(player.id, "tok-economy")
    servicer = LootBoxService()
    ctx = DummyContext()

    response = await servicer.PurchaseV1(  # type: ignore[arg-type]
        _request(product_id, f"tx-{uuid.uuid4()}", purchase_num=1), ctx
    )
    assert len(response.player_inventories) == 10
    free, _paid = await _wallet(player.id)
    assert free == 60000 - 1500
