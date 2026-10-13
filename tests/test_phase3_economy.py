"""Integration and unit tests for Phase 3: Economic system, gacha, wallet & inventory."""

import time
import uuid

import grpc
import pytest
from sqlalchemy import select

from dankagu.core.database import get_sessionmaker
from dankagu.core.economy import (
    InsufficientFundsError,
    _parse_card_fields,
    add_stones,
    deduct_stones,
    persist_card_to_storage,
)
from dankagu.core.session import set_current_player
from dankagu.core.starter import seed_starter_player
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    game_product_pb2,
    loot_box_pb2,
    player_inventory_pb2,
    player_storage_pb2,
    wallet_pb2,
)
from dankagu.models.player import Player
from dankagu.models.storage import PlayerStorageEntry
from dankagu.models.wallet import WalletBalance
from dankagu.services.game_product import GameProductService
from dankagu.services.loot_box import LootBoxService
from dankagu.services.player_inventory import PlayerInventoryService
from dankagu.services.player_storage import PlayerStorageService
from dankagu.services.wallet import WalletService


class DummyContext:
    """Mock gRPC ServicerContext for testing."""

    def __init__(self) -> None:
        self.aborted = False
        self.code = None
        self.details = None

    async def abort(self, code: grpc.StatusCode, details: str) -> None:
        self.aborted = True
        self.code = code
        self.details = details
        raise grpc.aio.AioRpcError(
            code=code, initial_metadata=(), trailing_metadata=(), details=details
        )


@pytest.fixture
async def sample_player() -> Player:
    """Create and seed a test player with default starter state."""
    session_factory = get_sessionmaker()
    pid = f"test-p3-{uuid.uuid4().hex[:8]}"
    uid = f"usr-{uuid.uuid4().hex[:8]}"
    now = int(time.time())

    async with session_factory() as session:
        player = Player(
            id=pid,
            user_id=uid,
            device_account=f"dev-{pid}",
            registered_at=now,
            last_login_at=now,
        )
        session.add(player)
        await session.commit()
        await seed_starter_player(session, player)
        await session.commit()

    return player


@pytest.mark.asyncio
async def test_wallet_real_balance_persistence(sample_player: Player) -> None:
    """Wallet balances reflect actual DB state dynamically."""
    set_current_player(sample_player.id, "tok-test")

    servicer = WalletService()
    ctx = DummyContext()

    # 1. Default initial balance seeded
    req2 = wallet_pb2.WalletGetBalancesV2.Request()
    resp2 = await servicer.GetBalancesV2(req2, ctx)
    vc_map = {vc.virtual_currency_name: vc.amount for vc in resp2.total.virtual_currencies}
    assert vc_map["FREE_STONE"] == 60000
    assert vc_map["PAID_STONE"] == 30000

    # 2. Update DB balance directly
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        w = await session.get(WalletBalance, sample_player.id)
        assert w is not None
        w.free_stones = 12345
        w.paid_stones = 6789
        await session.commit()

    # 3. Next GetBalances reflects updated values
    resp_updated = await servicer.GetBalancesV2(req2, ctx)
    vc_map_updated = {
        vc.virtual_currency_name: vc.amount for vc in resp_updated.total.virtual_currencies
    }
    assert vc_map_updated["FREE_STONE"] == 12345
    assert vc_map_updated["PAID_STONE"] == 6789

    # 4. GetBalancesV1 also matches
    req1 = wallet_pb2.WalletGetBalancesV1.Request()
    resp1 = await servicer.GetBalancesV1(req1, ctx)
    assert len(resp1.balance) > 0
    vc_map1 = {vc.virtual_currency_name: vc.amount for vc in resp1.balance[0].virtual_currencies}
    assert vc_map1["FREE_STONE"] == 12345
    assert vc_map1["PAID_STONE"] == 6789


@pytest.mark.asyncio
async def test_wallet_atomic_deductions_and_addition(sample_player: Player) -> None:
    """Free stones are deducted first, then paid; overflow raises error."""
    session_factory = get_sessionmaker()

    async with session_factory() as session:
        # Initial: 60,000 free, 30,000 paid
        free, paid = await deduct_stones(session, sample_player.id, 1500)
        await session.commit()
        assert free == 58500
        assert paid == 30000

    async with session_factory() as session:
        # Deduct all remaining free (58,500) plus 2,000 paid
        free, paid = await deduct_stones(session, sample_player.id, 60500)
        await session.commit()
        assert free == 0
        assert paid == 28000

    async with session_factory() as session:
        # Insufficient funds error (remaining paid is 28,000; attempting 29,000)
        with pytest.raises(InsufficientFundsError):
            await deduct_stones(session, sample_player.id, 29000)

    async with session_factory() as session:
        # Add stones
        free, paid = await add_stones(session, sample_player.id, free_amount=2500, paid_amount=500)
        await session.commit()
        assert free == 2500
        assert paid == 28500


@pytest.mark.asyncio
async def test_card_persistence_and_duplicate_breakthrough(sample_player: Player) -> None:
    """Cards are persisted to PlayerStorage, and duplicates increment limit break."""
    session_factory = get_sessionmaker()
    now = int(time.time())
    test_card_id = 999001

    async with session_factory() as session:
        # 1. First pull: new card
        key1, is_new1 = await persist_card_to_storage(session, sample_player.id, test_card_id, now)
        assert is_new1 is True
        assert key1.startswith("CardStatus--")
        await session.commit()

    async with session_factory() as session:
        # Verify card saved in database
        entry1 = await session.get(PlayerStorageEntry, (sample_player.id, key1))
        assert entry1 is not None
        fields1 = _parse_card_fields(entry1.value)
        assert fields1[2] == test_card_id  # card_id
        assert fields1[11] == 1  # duplicate count

        # 2. Second pull: duplicate of same card
        key2, is_new2 = await persist_card_to_storage(
            session, sample_player.id, test_card_id, now + 10
        )
        assert is_new2 is False
        assert key2 == key1  # same storage key updated
        await session.commit()

    async with session_factory() as session:
        # Verify duplicate count incremented
        entry2 = await session.get(PlayerStorageEntry, (sample_player.id, key1))
        assert entry2 is not None
        fields2 = _parse_card_fields(entry2.value)
        assert fields2[2] == test_card_id
        assert fields2[11] == 2  # incremented to 2!


@pytest.mark.asyncio
async def test_lootbox_purchase_flow(sample_player: Player) -> None:
    """LootBox PurchaseV1 deducts stones, persists cards, and returns updated wallet."""
    set_current_player(sample_player.id, "tok-gacha-test")

    servicer = LootBoxService()
    ctx = DummyContext()

    # Perform a 10-pull (costs 1,500 stones)
    req = loot_box_pb2.LootBoxV3PurchaseV1.Request()
    req.loot_box_product_id = "G_1_10_TT_L"
    req.transaction_id = f"tx-{uuid.uuid4().hex[:8]}"

    resp = await servicer.PurchaseV1(req, ctx)

    assert resp.transaction_id == req.transaction_id
    assert len(resp.player_inventories) == 10
    assert len(resp.extra_player_inventories) == 1  # bonus item for 10-pull

    # Check updated wallet in response: 60,000 - 1,500 = 58,500
    vc_map = {vc.virtual_currency_name: vc.amount for vc in resp.wallet.virtual_currencies}
    assert vc_map["FREE_STONE"] == 58500
    assert vc_map["PAID_STONE"] == 30000

    # Verify DB wallet balance
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        w = await session.get(WalletBalance, sample_player.id)
        assert w is not None
        assert w.free_stones == 58500

    # Simulate client saving the drawn cards via SetEntriesV2
    storage_servicer = PlayerStorageService()
    set_req = player_storage_pb2.PlayerStorageSetEntriesV2.Request()
    set_req.next_revision = "3"
    written_keys: set[str] = set()
    for i, _inv in enumerate(resp.player_inventories):
        e = set_req.entries.add()
        e.key = f"CardStatus--9161027867904573{i:02d}"
        e.value = b"\x08\x01\x10\x64"
        written_keys.add(e.key)
    await storage_servicer.SetEntriesV2(set_req, ctx)

    async with session_factory() as session:
        stmt = select(PlayerStorageEntry).where(
            PlayerStorageEntry.player_id == sample_player.id,
            PlayerStorageEntry.key.like("CardStatus--%"),
        )
        res = await session.execute(stmt)
        card_entries = res.scalars().all()
        # Everything the client just sent must have been persisted. (This used to
        # reach the asserted count only via 3 starter CardStatus entries that the
        # starter seeding no longer writes, so the bound is derived from the
        # payload instead of a magic number.)
        assert written_keys <= {e.key for e in card_entries}


@pytest.mark.asyncio
async def test_lootbox_purchase_insufficient_stones(sample_player: Player) -> None:
    """LootBox PurchaseV1 aborts with RESOURCE_EXHAUSTED when stones are insufficient."""
    set_current_player(sample_player.id, "tok-poor-test")

    # Set player to 50 stones (less than 150 needed for single pull)
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        w = await session.get(WalletBalance, sample_player.id)
        assert w is not None
        w.free_stones = 50
        w.paid_stones = 0
        await session.commit()

    servicer = LootBoxService()
    ctx = DummyContext()

    req = loot_box_pb2.LootBoxV3PurchaseV1.Request()
    req.loot_box_product_id = "G_1_1_TT_L"  # single pull
    req.transaction_id = "tx-fail"

    with pytest.raises(grpc.aio.AioRpcError):
        await servicer.PurchaseV1(req, ctx)

    assert ctx.aborted is True
    assert ctx.code == grpc.StatusCode.RESOURCE_EXHAUSTED


@pytest.mark.asyncio
async def test_player_inventory_gift_box_flow(sample_player: Player) -> None:
    """Player receives gift box items and wallet balances are automatically credited."""
    set_current_player(sample_player.id, "tok-inv-test")

    servicer = PlayerInventoryService()
    ctx = DummyContext()

    # 1. Starter seeding gave a welcome gift worth 2,500 Free Stones. The starter
    #    mailbox also holds the Day-1 cultivation materials, so pick the gift by
    #    resource name instead of assuming it is the first entry.
    req_get = player_inventory_pb2.PlayerInventoryGetInventoriesV1.Request()
    resp_get = await servicer.GetInventoriesV1(req_get, ctx)
    assert len(resp_get.inventories) >= 1
    welcome_gift = next(g for g in resp_get.inventories if g.system_resource_name == "FREE_STONE")
    assert welcome_gift.system_resource_num == 2500

    # 2. GetInventoriesAndCountV1 check
    req_count = player_inventory_pb2.PlayerInventoryGetInventoriesAndCountV1.Request()
    resp_count = await servicer.GetInventoriesAndCountV1(req_count, ctx)
    assert resp_count.inventories_count == len(resp_get.inventories)

    # 3. Claim the gift
    req_rcv = player_inventory_pb2.PlayerInventoryReceiveV1.Request()
    req_rcv.delete_inventory_ids.append(welcome_gift.id)
    req_rcv.next_revision = "5"
    resp_rcv = await servicer.ReceiveV1(req_rcv, ctx)

    # 4. Wallet updated: 60,000 + 2,500 = 62,500
    vc_map = {vc.virtual_currency_name: vc.amount for vc in resp_rcv.wallet.virtual_currencies}
    assert vc_map["FREE_STONE"] == 62500
    assert resp_rcv.revision == "5"

    # 5. The claimed gift is gone from the mailbox; the other starter mail stays.
    resp_after = await servicer.GetInventoriesV1(req_get, ctx)
    assert welcome_gift.id not in {i.id for i in resp_after.inventories}

    # 6. GetReceivedInventoriesV1 shows the claimed gift
    req_hist = player_inventory_pb2.PlayerInventoryGetReceivedInventoriesV1.Request()
    resp_hist = await servicer.GetReceivedInventoriesV1(req_hist, ctx)
    assert len(resp_hist.received_inventories) >= 1
    assert welcome_gift.id in {i.id for i in resp_hist.received_inventories}


@pytest.mark.asyncio
async def test_game_product_purchase_continue(sample_player: Player) -> None:
    """GameProduct purchase (Continue/Stamina) deducts stones and updates PlayerStorage."""
    set_current_player(sample_player.id, "tok-shop-test")

    servicer = GameProductService()
    ctx = DummyContext()

    # 1. Purchase Continue (SystemGameProduct_1, costs 50 stones)
    req1 = game_product_pb2.GameProductPurchaseV1.Request()
    req1.game_product_id = "SystemGameProduct_1"
    req1.transaction_id = "tx-continue-1"

    resp1 = await servicer.PurchaseV1(req1, ctx)
    assert resp1.transaction_id == "tx-continue-1"
    vc_map1 = {vc.virtual_currency_name: vc.amount for vc in resp1.wallet.virtual_currencies}
    assert vc_map1["FREE_STONE"] == 59950  # 60,000 - 50

    # 2. Purchase and Save Player Storage
    req_save = game_product_pb2.GameProductPurchaseAndSavePlayerStorageV1.Request()
    req_save.game_product_id = "SystemGameProduct_1"
    req_save.transaction_id = "tx-continue-2"
    req_save.next_revision = "10"
    entry = req_save.entries.add()
    entry.key = "PlayMusicRecord--1001"
    entry.value = b"\x08\x01\x10\x64"

    resp_save = await servicer.PurchaseAndSavePlayerStorageV1(req_save, ctx)
    vc_map2 = {vc.virtual_currency_name: vc.amount for vc in resp_save.wallet.virtual_currencies}
    assert vc_map2["FREE_STONE"] == 59900  # 59,950 - 50
    assert resp_save.revision == "10"
    assert len(resp_save.entries) == 1

    # Verify storage entry was saved to database
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        saved_entry = await session.get(
            PlayerStorageEntry, (sample_player.id, "PlayMusicRecord--1001")
        )
        assert saved_entry is not None
        assert saved_entry.value == b"\x08\x01\x10\x64"


def test_roll_card_survives_empty_gacha_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: an unseeded database must not crash a roll.

    ``_CARDS_BY_RARITY`` is initialised with empty lists, so the ``.get(2, [...])``
    fallback used to hand back an empty pool and ``random.choice([])`` raised
    IndexError (seen on CI, where the database holds no gacha rows).
    """
    from dankagu.services import loot_box as lb

    monkeypatch.setattr(lb, "_CARDS_BY_RARITY", {1: [], 2: [], 3: [], 4: []})
    monkeypatch.setattr(lb, "_BY_ELEMENT", {1: {}, 2: {}, 3: {}})
    monkeypatch.setattr(lb, "_CARDS_BY_ID", {})
    monkeypatch.setattr(lb, "_BANNERS", {})

    # guaranteed SSR and a plain roll both have to survive the empty pool
    assert lb._roll_card(159, is_guaranteed_ssr=True) == lb.STARTER_CARD_ID
    assert lb._roll_card(159) == lb.STARTER_CARD_ID
