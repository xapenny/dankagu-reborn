"""Economy management: atomic wallet operations and card storage persistence."""

import logging
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.models.storage import PlayerStorageEntry
from dankagu.models.wallet import WalletBalance

logger = logging.getLogger("dankagu.core.economy")


class InsufficientFundsError(Exception):
    """Raised when player does not have enough virtual currencies."""


class CorruptedPlayerValueError(Exception):
    """Raised when a stored player value exists but cannot be decoded."""


async def get_or_create_wallet(
    session: AsyncSession, player_id: str, *, for_update: bool = False
) -> WalletBalance:
    """Fetch player's WalletBalance or create a seeded default if non-existent.

    ``for_update=True`` takes a row lock, which the spend paths need: reading the
    balance and writing it back without a lock lets two concurrent purchases both
    pass the funds check and both write, so one of them is effectively free.
    """
    wallet: WalletBalance | None = None
    if for_update:
        wallet = (
            (
                await session.execute(
                    select(WalletBalance)
                    .where(WalletBalance.player_id == player_id)
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )
        if wallet is not None:
            return wallet

    wallet = await session.get(WalletBalance, player_id)
    if wallet is None:
        wallet = WalletBalance(
            player_id=player_id,
            free_stones=60000,
            paid_stones=30000,
            updated_at=int(time.time()),
        )
        session.add(wallet)
        await session.flush()
        logger.info("💰 Created default wallet for player %s (60000 free, 30000 paid)", player_id)
    return wallet


async def deduct_stones(
    session: AsyncSession,
    player_id: str,
    cost: int,
    allow_paid: bool = True,
) -> tuple[int, int]:
    """Atomically deduct stones from player's wallet (free first, then paid).

    Returns:
        tuple[int, int]: (remaining_free_stones, remaining_paid_stones)

    Raises:
        InsufficientFundsError: if total balance is insufficient.
    """
    wallet = await get_or_create_wallet(session, player_id, for_update=True)
    total_available = wallet.free_stones + (wallet.paid_stones if allow_paid else 0)

    if total_available < cost:
        raise InsufficientFundsError(
            f"Player {player_id} insufficient stones: has {total_available} (free={wallet.free_stones}, "
            f"paid={wallet.paid_stones}), requires {cost}"
        )

    if wallet.free_stones >= cost:
        wallet.free_stones -= cost
    else:
        remainder = cost - wallet.free_stones
        wallet.free_stones = 0
        wallet.paid_stones -= remainder

    wallet.updated_at = int(time.time())
    logger.info(
        "💸 Deducted %d stones from player %s (free=%d, paid=%d remaining)",
        cost,
        player_id,
        wallet.free_stones,
        wallet.paid_stones,
    )
    return wallet.free_stones, wallet.paid_stones


async def add_stones(
    session: AsyncSession,
    player_id: str,
    free_amount: int = 0,
    paid_amount: int = 0,
) -> tuple[int, int]:
    """Add virtual currencies to player's wallet.

    Returns:
        tuple[int, int]: (new_free_stones, new_paid_stones)
    """
    wallet = await get_or_create_wallet(session, player_id, for_update=True)
    wallet.free_stones += free_amount
    wallet.paid_stones += paid_amount
    wallet.updated_at = int(time.time())
    logger.info(
        "💎 Added stones to player %s (+%d free, +%d paid -> free=%d, paid=%d)",
        player_id,
        free_amount,
        paid_amount,
        wallet.free_stones,
        wallet.paid_stones,
    )
    return wallet.free_stones, wallet.paid_stones


def _encode_varint(field_num: int, val: int) -> bytes:
    """Encode field number and integer value into protobuf varint wire format."""
    tag = (field_num << 3) | 0
    buf = bytearray()
    while tag > 0x7F:
        buf.append((tag & 0x7F) | 0x80)
        tag >>= 7
    buf.append(tag)
    while val > 0x7F:
        buf.append((val & 0x7F) | 0x80)
        val >>= 7
    buf.append(val)
    return bytes(buf)


CARD_STATUS_PREFIX = "CardStatus--"

#: CardStatus payload fields, as the client's own schema numbers them.
CARD_FIELD_INSTANCE_ID = 1
CARD_FIELD_CARD_ID = 2
CARD_FIELD_CREATED_AT = 3
CARD_FIELD_BLOSSOM = 5
CARD_FIELD_EXP = 6
CARD_FIELD_STATUS = 8
CARD_FIELD_DUPLICATE = 11


@dataclass
class CardCanonicalisation:
    """What to write, what to drop, and the resulting copy count per card."""

    writes: list[tuple[str, bytes]]
    drop_keys: list[str]
    copies: dict[int, int]
    collapsed: dict[int, int]


def card_progress(fields: dict[int, int]) -> tuple[int, int, int]:
    """How far a card has been developed: (status, blossom, exp), strongest last.

    Used to pick which of several copies of one card survives a merge, and to carry the
    highest development into the surviving entry, so folding duplicates can never discard
    a level or a limit break the player earned.
    """
    return (
        fields.get(CARD_FIELD_STATUS, 0) or 0,
        fields.get(CARD_FIELD_BLOSSOM, 0) or 0,
        fields.get(CARD_FIELD_EXP, 0) or 0,
    )


def merge_card_payload(
    card_id: int,
    survivor: dict[int, int],
    others: list[dict[int, int]],
    duplicate: int,
    now: int,
) -> bytes:
    """Build the surviving payload, keeping the strongest development of every copy."""
    strongest = max([survivor, *others], key=card_progress)
    created = [fields.get(CARD_FIELD_CREATED_AT) for fields in [survivor, *others]]
    created = [value for value in created if value]
    return encode_card_status(
        instance_id=survivor.get(CARD_FIELD_INSTANCE_ID, 0),
        card_id=card_id,
        created_at=min(created) if created else now,
        updated_at=now,
        blossom=strongest.get(CARD_FIELD_BLOSSOM, 1) or 1,
        exp=strongest.get(CARD_FIELD_EXP, 0) or 0,
        duplicate=duplicate,
        status=strongest.get(CARD_FIELD_STATUS, 1) or 1,
    )


def canonicalise_card_entries(
    existing: dict[int, tuple[str, bytes]],
    incoming: list[tuple[str, bytes]],
    now: int,
) -> CardCanonicalisation | None:
    """Fold the client's per-copy card entries into one entry per card.

    The client writes one ``CardStatus--<instance id>`` entry **per drawn copy**, so two
    copies of a card in a single ten-pull became two collection entries (the live
    database shows cards occupying up to four), instead of one entry whose ``duplicate``
    field counts the copies -- which is what this project's own merge helper always
    assumed, and what the client's limit-break logic reads.

    Returns ``None`` when the request carries no card entries at all, so the caller can
    keep its fast path.
    """
    card_incoming: list[tuple[str, bytes]] = [
        (key, value) for key, value in incoming if key.startswith(CARD_STATUS_PREFIX)
    ]
    if not card_incoming:
        return None

    writes: list[tuple[str, bytes]] = []
    drop_keys: list[str] = []
    copies: dict[int, int] = {}
    collapsed: dict[int, int] = {}

    # Group the request by card id, keeping the order the client sent them in.
    grouped: dict[int, list[tuple[str, bytes]]] = {}
    for key, value in card_incoming:
        fields = _parse_card_fields(value)
        card_id = fields.get(CARD_FIELD_CARD_ID)
        if not card_id:
            # Not a card we can read; store it verbatim rather than losing the write.
            writes.append((key, value))
            continue
        grouped.setdefault(int(card_id), []).append((key, value))

    for card_id, entries in grouped.items():
        stored = existing.get(card_id)
        # One copy means one increment; an entry without the field still counts as one.
        increment = sum(
            _parse_card_fields(value).get(CARD_FIELD_DUPLICATE, 1) or 1 for _, value in entries
        )

        if stored is not None:
            stored_key, stored_value = stored
            stored_fields = _parse_card_fields(stored_value)
            incoming_fields = [_parse_card_fields(value) for _key, value in entries]
            total = (stored_fields.get(CARD_FIELD_DUPLICATE, 1) or 1) + increment
            writes.append(
                (
                    stored_key,
                    merge_card_payload(
                        card_id=card_id,
                        survivor=stored_fields,
                        others=incoming_fields,
                        duplicate=total,
                        now=now,
                    ),
                )
            )
            # Every key the client just sent is redundant: the card lives under the key
            # it already had. Drop them, including copies stored by earlier writes.
            for key, _value in entries:
                if key != stored_key:
                    drop_keys.append(key)
        else:
            keep_key, keep_value = entries[0]
            keep_fields = _parse_card_fields(keep_value)
            writes.append(
                (
                    keep_key,
                    merge_card_payload(
                        card_id=card_id,
                        survivor=keep_fields,
                        others=[_parse_card_fields(value) for _key, value in entries[1:]],
                        duplicate=increment,
                        now=now,
                    ),
                )
            )
            for key, _value in entries[1:]:
                drop_keys.append(key)

        copies[card_id] = increment
        if len(entries) > 1:
            collapsed[card_id] = len(entries)

    return CardCanonicalisation(
        writes=writes, drop_keys=drop_keys, copies=copies, collapsed=collapsed
    )


def _parse_card_fields(data: bytes) -> dict[int, int]:
    """Parse varint fields from CardStatus wire payload."""
    fields: dict[int, int] = {}
    i = 0
    n = len(data)
    while i < n:
        tag_byte = data[i]
        field_num = tag_byte >> 3
        wire_type = tag_byte & 7
        i += 1
        if wire_type == 0:  # varint
            val = 0
            shift = 0
            while i < n:
                b = data[i]
                i += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            fields[field_num] = val
        elif wire_type == 2:  # length-delimited
            if i >= n:
                break
            length = data[i]
            i += 1 + length
        else:
            break
    return fields


def encode_card_status(
    instance_id: int,
    card_id: int,
    created_at: int,
    updated_at: int,
    blossom: int = 1,
    exp: int = 0,
    duplicate: int = 1,
    status: int = 1,
) -> bytes:
    """Construct authentic CardStatus binary protobuf matching iOS/Android client schema."""
    res = bytearray()
    res.extend(_encode_varint(1, instance_id))
    res.extend(_encode_varint(2, card_id))
    res.extend(_encode_varint(3, created_at))
    res.extend(_encode_varint(4, updated_at))
    if blossom > 1:
        res.extend(_encode_varint(5, blossom))
    if exp > 0:
        res.extend(_encode_varint(6, exp))
    res.extend(_encode_varint(8, status))
    res.extend(_encode_varint(10, 1))
    res.extend(_encode_varint(11, duplicate))
    return bytes(res)


async def persist_cards_to_storage(
    session: AsyncSession,
    player_id: str,
    card_ids: list[int],
    now: int,
) -> None:
    """Persist multiple drawn cards into PlayerStorage atomically.

    Handles duplicate cards within the same pull and existing collection cleanly.
    """
    if not card_ids:
        return

    stmt = select(PlayerStorageEntry).where(
        PlayerStorageEntry.player_id == player_id,
        PlayerStorageEntry.key.like("CardStatus--%"),
    )
    res = await session.execute(stmt)
    entries = res.scalars().all()

    # Map card_id -> (entry, parsed_fields)
    card_map: dict[int, tuple[PlayerStorageEntry, dict[int, int]]] = {}
    for entry in entries:
        fields = _parse_card_fields(entry.value)
        cid = fields.get(2)
        if cid:
            card_map[cid] = (entry, fields)

    base_inst_id = int(time.time() * 10000)

    for idx, card_id in enumerate(card_ids):
        if card_id in card_map:
            entry, fields = card_map[card_id]
            inst_id_val = fields.get(1)
            if not inst_id_val:
                try:
                    inst_id_val = int(entry.key.removeprefix("CardStatus--"))
                except ValueError:
                    inst_id_val = base_inst_id + idx

            created_at = fields.get(3, now)
            blossom = fields.get(5, 1)
            exp = fields.get(6, 0)
            dup = fields.get(11, fields.get(7, 1)) + 1
            status = fields.get(8, 1)

            entry.value = encode_card_status(
                instance_id=inst_id_val,
                card_id=card_id,
                created_at=created_at,
                updated_at=now,
                blossom=blossom,
                exp=exp,
                duplicate=dup,
                status=status,
            )
            entry.updated_at = now
            fields[11] = dup
            logger.info(
                "🎴 Increment duplicate for card %d (player %s, new dup count=%d)",
                card_id,
                player_id,
                dup,
            )
        else:
            new_inst_id = base_inst_id + idx
            new_bytes = encode_card_status(
                instance_id=new_inst_id,
                card_id=card_id,
                created_at=now,
                updated_at=now,
                blossom=1,
                exp=0,
                duplicate=1,
                status=1,
            )
            new_entry = PlayerStorageEntry(
                player_id=player_id,
                key=f"CardStatus--{new_inst_id}",
                value=new_bytes,
                created_at=now,
                updated_at=now,
            )
            session.add(new_entry)
            card_map[card_id] = (
                new_entry,
                {
                    1: new_inst_id,
                    2: card_id,
                    3: now,
                    4: now,
                    5: 1,
                    6: 0,
                    8: 1,
                    11: 1,
                },
            )
            logger.info(
                "🎴 Added NEW card %d to player %s (key=%s)",
                card_id,
                player_id,
                new_entry.key,
            )


async def persist_card_to_storage(
    session: AsyncSession,
    player_id: str,
    card_id: int,
    now: int,
) -> tuple[str, bool]:
    """Persist a single drawn card into PlayerStorage."""
    stmt = select(PlayerStorageEntry).where(
        PlayerStorageEntry.player_id == player_id,
        PlayerStorageEntry.key.like("CardStatus--%"),
    )
    res = await session.execute(stmt)
    entries = res.scalars().all()
    existing_key = None
    for entry in entries:
        fields = _parse_card_fields(entry.value)
        if fields.get(2) == card_id:
            existing_key = entry.key
            break

    await persist_cards_to_storage(session, player_id, [card_id], now)
    if existing_key:
        return existing_key, False

    res2 = await session.execute(stmt)
    for entry in res2.scalars().all():
        fields = _parse_card_fields(entry.value)
        if fields.get(2) == card_id:
            return entry.key, True

    return f"CardStatus--{card_id}", True


async def get_player_key_value(
    session: AsyncSession,
    player_id: str,
    key: str,
    default: int = 0,
    *,
    for_update: bool = False,
) -> int:
    """Retrieve player key value (e.g. points, tickets) from PlayerStorage.

    A value that is present but unreadable raises instead of silently falling back
    to ``default``: pretending a corrupted balance is 0 (or 8612 points) is how a
    player's currency appears to vanish — or is handed to them for free.

    ``for_update=True`` locks the row so a spend cannot race another spend.
    """
    storage_key = f"PlayerKeyValue:{key}"
    entry: PlayerStorageEntry | None
    if for_update:
        entry = (
            (
                await session.execute(
                    select(PlayerStorageEntry)
                    .where(
                        PlayerStorageEntry.player_id == player_id,
                        PlayerStorageEntry.key == storage_key,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )
    else:
        entry = await session.get(PlayerStorageEntry, (player_id, storage_key))
    if entry and entry.value:
        try:
            return int(entry.value.decode("utf-8"))
        except Exception as exc:
            logger.error(
                "❌ Unreadable player value player=%s key=%r raw=%r",
                player_id,
                storage_key,
                entry.value[:64],
                exc_info=exc,
            )
            raise CorruptedPlayerValueError(
                f"Player {player_id} value {storage_key!r} cannot be decoded"
            ) from exc
    return default


async def deduct_player_key_value(
    session: AsyncSession,
    player_id: str,
    key: str,
    amount: int,
    default: int = 0,
) -> int:
    """Atomically deduct amount from a player key value (e.g. points).

    The row is locked before it is read, so two concurrent spends cannot both see
    the old balance and both write it back (which would make one of them free).
    """
    current = await get_player_key_value(session, player_id, key, default=default, for_update=True)
    if current < amount:
        raise InsufficientFundsError(f"Insufficient {key}: has {current}, requires {amount}")
    new_val = current - amount
    storage_key = f"PlayerKeyValue:{key}"
    now = int(time.time())
    entry = await session.get(PlayerStorageEntry, (player_id, storage_key))
    if entry:
        entry.value = str(new_val).encode("utf-8")
        entry.updated_at = now
    else:
        entry = PlayerStorageEntry(
            player_id=player_id,
            key=storage_key,
            value=str(new_val).encode("utf-8"),
            created_at=now,
            updated_at=now,
        )
        session.add(entry)
    logger.info("💸 Deducted %d %s from %s (%d -> %d)", amount, key, player_id, current, new_val)
    return new_val
