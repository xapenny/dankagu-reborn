"""Economy management: atomic wallet operations and card storage persistence."""

import logging
import time
from dataclasses import dataclass
from typing import Any

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

#: CardStatus payload fields, matching client CardStatusProto wire schema.

CARD_FIELD_INSTANCE_ID = 1
CARD_FIELD_CARD_ID = 2
CARD_FIELD_CREATED_AT = 3
CARD_FIELD_UPDATED_AT = 4

CARD_FIELD_AWAKE_LV = 5

CARD_FIELD_EXP = 6
CARD_FIELD_DANMAKU_EXP = 7

CARD_FIELD_FAVORITE = 8

CARD_FIELD_USE_AWK_IMAGE = 9

CARD_FIELD_NEW = 10

CARD_FIELD_USE_PREMIUM_IMAGE = 11


# Aliases for backward compatibility

CARD_FIELD_BLOSSOM = CARD_FIELD_AWAKE_LV

CARD_FIELD_STATUS = CARD_FIELD_FAVORITE

CARD_FIELD_DUPLICATE = CARD_FIELD_AWAKE_LV

MAX_AWAKE_LV = 5


@dataclass
class CardCanonicalisation:
    """What to write, what to drop, and the resulting copy count per card."""

    writes: list[tuple[str, bytes]]
    drop_keys: list[str]
    copies: dict[int, int]
    collapsed: dict[int, int]


def card_progress(fields: dict[int, int]) -> tuple[int, int, int, int]:
    """How far a card has been developed: (favorite, awake_lv, exp, danmaku_exp).


    Used to pick which of several copies of one card survives a merge, and to carry the
    highest development into the surviving entry, so folding duplicates can never discard
    a level or a limit break the player earned.
    """
    return (
        fields.get(CARD_FIELD_FAVORITE, 0) or 0,
        fields.get(CARD_FIELD_AWAKE_LV, 0) or 0,
        fields.get(CARD_FIELD_EXP, 0) or 0,
        fields.get(CARD_FIELD_DANMAKU_EXP, 0) or 0,
    )


def merge_card_payload(
    card_id: int,
    survivor: dict[int, int],
    others: list[dict[int, int]],
    awake_lv: int,
    now: int,
) -> bytes:
    """Build the surviving payload, keeping the strongest development of every copy."""
    all_copies = [survivor, *others]

    strongest = max(all_copies, key=card_progress)

    created = [fields.get(CARD_FIELD_CREATED_AT) for fields in all_copies]

    created = [value for value in created if value]

    max_exp = max((fields.get(CARD_FIELD_EXP, 0) or 0) for fields in all_copies)

    max_danmaku_exp = max((fields.get(CARD_FIELD_DANMAKU_EXP, 0) or 0) for fields in all_copies)

    fav = 1 if any((fields.get(CARD_FIELD_FAVORITE, 0) or 0) > 0 for fields in all_copies) else 0

    use_prem = (
        1
        if any((fields.get(CARD_FIELD_USE_PREMIUM_IMAGE, 0) or 0) > 0 for fields in all_copies)
        else 0
    )

    use_awk = (
        1
        if any((fields.get(CARD_FIELD_USE_AWK_IMAGE, 0) or 0) > 0 for fields in all_copies)
        else (1 if awake_lv >= 1 else 0)
    )

    return encode_card_status(
        instance_id=survivor.get(CARD_FIELD_INSTANCE_ID, 0),
        card_id=card_id,
        created_at=min(created) if created else now,
        updated_at=now,
        awake_lv=min(MAX_AWAKE_LV, awake_lv),
        exp=max_exp,
        danmaku_exp=max_danmaku_exp,
        favorite=fav,
        use_awk_image=use_awk,
        is_new=strongest.get(CARD_FIELD_NEW, 0) or 0,
        use_premium_image=use_prem,
    )


def canonicalise_card_entries(
    existing: dict[int, tuple[str, bytes]],
    incoming: list[tuple[str, bytes]],
    now: int,
) -> CardCanonicalisation | None:
    """Fold the client's per-copy card entries into one entry per card.

    The client writes one ``CardStatus--<instance id>`` entry **per drawn copy**, so two
    copies of a card in a single ten-pull became two collection entries, instead of one

    entry whose ``awake_lv`` (Field 5) reflects the card's limit-break progression.


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

    grouped: dict[int, list[tuple[str, bytes]]] = {}
    for key, value in card_incoming:
        fields = _parse_card_fields(value)
        card_id = fields.get(CARD_FIELD_CARD_ID)
        if not card_id:
            writes.append((key, value))
            continue
        grouped.setdefault(int(card_id), []).append((key, value))

    for card_id, entries in grouped.items():
        stored = existing.get(card_id)
        # Each entry represents at least 1 copy (plus any previous AwakeLv if non-zero)

        incoming_copies = sum(
            max(1, (_parse_card_fields(value).get(CARD_FIELD_AWAKE_LV, 0) or 0) + 1)
            for _, value in entries
        )

        if stored is not None:
            stored_key, stored_value = stored
            stored_fields = _parse_card_fields(stored_value)
            incoming_fields = [_parse_card_fields(value) for _key, value in entries]
            stored_copies = max(1, (stored_fields.get(CARD_FIELD_AWAKE_LV, 0) or 0) + 1)

            total_copies = stored_copies + incoming_copies

            new_awake = min(MAX_AWAKE_LV, max(0, total_copies - 1))

            writes.append(
                (
                    stored_key,
                    merge_card_payload(
                        card_id=card_id,
                        survivor=stored_fields,
                        others=incoming_fields,
                        awake_lv=new_awake,
                        now=now,
                    ),
                )
            )
            for key, _value in entries:
                if key != stored_key:
                    drop_keys.append(key)
            copies[card_id] = total_copies

        else:
            keep_key, keep_value = entries[0]
            keep_fields = _parse_card_fields(keep_value)
            other_fields = [_parse_card_fields(value) for _key, value in entries[1:]]

            total_copies = incoming_copies

            new_awake = min(MAX_AWAKE_LV, max(0, total_copies - 1))

            writes.append(
                (
                    keep_key,
                    merge_card_payload(
                        card_id=card_id,
                        survivor=keep_fields,
                        others=other_fields,
                        awake_lv=new_awake,
                        now=now,
                    ),
                )
            )
            for key, _value in entries[1:]:
                drop_keys.append(key)
            copies[card_id] = total_copies

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
    awake_lv: int = 0,
    exp: int = 0,
    danmaku_exp: int = 0,
    favorite: int = 0,
    use_awk_image: int = 0,
    is_new: int = 0,
    use_premium_image: int = 0,
    **kwargs: Any,
) -> bytes:
    """Construct authentic CardStatus binary protobuf matching iOS/Android client schema."""
    if "blossom" in kwargs and kwargs["blossom"] is not None and not awake_lv:
        awake_lv = kwargs["blossom"]

    if "duplicate" in kwargs and kwargs["duplicate"] is not None and not awake_lv:
        # Backward compatibility for tests passing duplicate:

        # 1 copy -> awake_lv 0; N copies -> awake_lv N - 1

        dup = kwargs["duplicate"]

        awake_lv = max(0, dup - 1) if dup > 0 else 0

    if "status" in kwargs and kwargs["status"] is not None and not favorite:
        favorite = kwargs["status"]

    res = bytearray()
    if instance_id:
        res.extend(_encode_varint(1, instance_id))

    if card_id:
        res.extend(_encode_varint(2, card_id))

    if created_at:
        res.extend(_encode_varint(3, created_at))

    if updated_at:
        res.extend(_encode_varint(4, updated_at))

    if awake_lv > 0:
        res.extend(_encode_varint(5, awake_lv))

    if exp > 0:
        res.extend(_encode_varint(6, exp))
    if danmaku_exp > 0:
        res.extend(_encode_varint(7, danmaku_exp))

    if favorite > 0:
        res.extend(_encode_varint(8, favorite))

    if use_awk_image > 0:
        res.extend(_encode_varint(9, use_awk_image))

    if is_new > 0:
        res.extend(_encode_varint(10, is_new))

    if use_premium_image > 0:
        res.extend(_encode_varint(11, use_premium_image))

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
        cid = fields.get(CARD_FIELD_CARD_ID)

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

            created_at = fields.get(CARD_FIELD_CREATED_AT, now)

            awake_lv = fields.get(CARD_FIELD_AWAKE_LV, 0) or 0

            exp = fields.get(CARD_FIELD_EXP, 0) or 0

            favorite = fields.get(CARD_FIELD_FAVORITE, 1) or 1

            new_awake = min(MAX_AWAKE_LV, awake_lv + 1)

            entry.value = encode_card_status(
                instance_id=inst_id_val,
                card_id=card_id,
                created_at=created_at,
                updated_at=now,
                awake_lv=new_awake,
                exp=exp,
                favorite=favorite,
            )
            entry.updated_at = now
            fields[CARD_FIELD_AWAKE_LV] = new_awake

            logger.info(
                "🎴 Increment AwakeLv for card %d (player %s, new AwakeLv=%d)",
                card_id,
                player_id,
                new_awake,
            )
        else:
            new_inst_id = base_inst_id + idx
            new_bytes = encode_card_status(
                instance_id=new_inst_id,
                card_id=card_id,
                created_at=now,
                updated_at=now,
                awake_lv=0,
                exp=0,
                favorite=1,
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
                    CARD_FIELD_INSTANCE_ID: new_inst_id,
                    CARD_FIELD_CARD_ID: card_id,
                    CARD_FIELD_CREATED_AT: now,
                    CARD_FIELD_UPDATED_AT: now,
                    CARD_FIELD_AWAKE_LV: 0,
                    CARD_FIELD_EXP: 0,
                    CARD_FIELD_FAVORITE: 1,
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
