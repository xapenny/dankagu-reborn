"""Automatic milestone and performance emblem evaluation engine for Danmaku Kagura.

Evaluates player state (Danmaku Rate, Ura Kagura Lunatic song clears/FC/AB,
total clears/FC/spell breaks, heroine/character levels, expeditions, and
spot reconstructions) to determine which released milestone titles are unlocked.
"""

import logging
from collections.abc import Iterable
from typing import Any

from google.protobuf.internal import decoder

from dankagu.services.box_loot_box import parse_raw_protobuf

logger = logging.getLogger("dankagu.core.milestone_emblems")

# 1. Danmaku Rate thresholds -> Emblem ID
RATE_THRESHOLDS: list[tuple[int, int]] = [
    (500, 2000005),
    (1000, 2000006),
    (1600, 2000007),
    (2000, 2000008),
    (3000, 2000031),
    (4000, 2000032),
    (5000, 2000033),
    (6000, 2000034),
    (7000, 2000035),
]

# 2. Ura Kagura LUNATIC performance songs (25 songs * 3 emblems = 75 emblems)
# Suffix 1: All Brilliant, Suffix 2: Full Combo, Suffix 3: Stage Clear
URA_KAGURA_LUNATIC_SONGS: set[int] = {
    2,
    3,
    6,
    7,
    8,
    10,
    11,
    12,
    14,
    15,
    16,
    17,
    20,
    22,
    29,
    30,
    35,
    36,
    37,
    39,
    42,
    48,
    50,
    53,
    133,
}

# 3. Overall gameplay counts (clears, full combos, spell breaks)
CLEAR_COUNT_THRESHOLDS: list[tuple[int, int]] = [
    (1000, 2000001),
    (10000, 2000002),
]

FULL_COMBO_COUNT_THRESHOLDS: list[tuple[int, int]] = [
    (100, 2000003),
    (1000, 2000004),
]

SPELL_BREAK_COUNT_THRESHOLDS: list[tuple[int, int]] = [
    (100, 2000029),
    (1000, 2000030),
]

# 4. Expedition counts
EXPEDITION_TOTAL_THRESHOLDS: list[tuple[int, int]] = [
    (1000, 2000009),
    (10000, 2000010),
]

EXPEDITION_SUPER_THRESHOLDS: list[tuple[int, int]] = [
    (100, 2000011),
    (1000, 2000012),
]

# 5. Spot Reconstruction completion
SPOT_RECONSTRUCTION_MAP: dict[int, int] = {
    1: 2000013,  # Hakurei Shrine
    2: 2000014,  # Koumakan
    11: 2000015,  # Chireiden
    17: 2000016,  # Forest of Magic
    4: 2000017,  # Basement
    5: 2000018,  # Hakugyokurou
    6: 2000019,  # Eientei
    7: 2000020,  # Bamboo Forest of the Lost
    8: 2000021,  # Moriya Shrine
    9: 2000022,  # Youkai Mountain
    12: 2000023,  # Seirensen
    13: 2000024,  # Shinreibyou
    14: 2000025,  # Shining Needle Castle
    15: 2000026,  # Misty Lake
    19: 2000027,  # Human Village
    18: 2200001,  # Kirisame Magic Shop
    20: 2200002,  # Heaven
    21: 2200003,  # Kasen's Mansion
    22: 2200004,  # Myourenji
    23: 2200005,  # Mayohiga
}

BASE_SPOT_IDS: set[int] = {1, 2, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15, 17, 19}


def decode_repeated_varints(tag_items: list[tuple[int, Any]]) -> list[int]:
    """Decode repeated varints from packed byte string or unpacked integer list."""
    res = []
    for wt, v in tag_items:
        if wt == 0:
            res.append(int(v))
        elif wt == 2 and isinstance(v, (bytes, bytearray)):
            pos = 0
            while pos < len(v):
                val, pos = decoder._DecodeVarint(v, pos)
                res.append(val)
    return res


def evaluate_milestone_emblems(entries: Iterable[Any]) -> set[int]:
    """Evaluate entries to determine all achieved milestone emblem IDs.

    Accepts an iterable of objects having ``key`` and ``value`` attributes,
    or ``(key, value)`` tuples.
    """
    emblems: set[int] = set()
    reconstructed_spots: set[int] = set()

    for item in entries:
        if isinstance(item, tuple):
            k, v = item[0], item[1]
        elif hasattr(item, "key") and hasattr(item, "value"):
            k, v = item.key, item.value
        elif isinstance(item, dict):
            k, v = item.get("key"), item.get("value")
        else:
            continue

        if not v or not isinstance(k, str):
            continue

        # ---------------------------------------------------------------------
        # 1. PlayerPublicStatus--1 (Danmaku Rate, total clears/FCs/spell breaks)
        # ---------------------------------------------------------------------
        if k == "PlayerPublicStatus--1" or k.startswith("PlayerPublicStatus--"):
            try:
                fields = parse_raw_protobuf(v)

                # Danmaku Rate (Tag 7)
                rate = fields.get(7, [(0, 0)])[0][1] if 7 in fields else 0
                for thresh, eid in RATE_THRESHOLDS:
                    if rate >= thresh:
                        emblems.add(eid)

                # Total Clears (Tag 2 repeated ints across 5 difficulties)
                clears = sum(decode_repeated_varints(fields.get(2, [])))
                for thresh, eid in CLEAR_COUNT_THRESHOLDS:
                    if clears >= thresh:
                        emblems.add(eid)

                # Total Full Combos (Tag 3 repeated ints across 5 difficulties)
                fcs = sum(decode_repeated_varints(fields.get(3, [])))
                for thresh, eid in FULL_COMBO_COUNT_THRESHOLDS:
                    if fcs >= thresh:
                        emblems.add(eid)

                # Total Spell Breaks (Tag 5 or Tag 20)
                sb1 = fields.get(5, [(0, 0)])[0][1] if 5 in fields else 0
                sb2 = fields.get(20, [(0, 0)])[0][1] if 20 in fields else 0
                sb = max(sb1, sb2)
                for thresh, eid in SPELL_BREAK_COUNT_THRESHOLDS:
                    if sb >= thresh:
                        emblems.add(eid)

                # Spot symbol count (Tag 11)
                sym_cnt = fields.get(11, [(0, 0)])[0][1] if 11 in fields else 0
                if sym_cnt >= 15:
                    emblems.add(2000028)
                    emblems.add(2000036)
            except Exception as e:
                logger.debug("Failed parsing PlayerPublicStatus: %s", e)

        # ---------------------------------------------------------------------
        # 2. PlayMusicRecord--<mid> (Lunatic All Brilliant / Full Combo / Clear)
        # ---------------------------------------------------------------------
        elif k.startswith("PlayMusicRecord--"):
            try:
                mid_str = k.split("--", 1)[1]
                mid = int(mid_str)
                fields = parse_raw_protobuf(v)
                levels = fields.get(8, [])
                for idx, (_, lvl_raw) in enumerate(levels):
                    if not isinstance(lvl_raw, (bytes, bytearray)):
                        continue
                    lvl = parse_raw_protobuf(lvl_raw)
                    diff = lvl.get(1, [(0, idx)])[0][1]
                    if diff == 4 or idx == 4:  # Lunatic difficulty
                        clear_cnt = lvl.get(3, [(0, 0)])[0][1] if 3 in lvl else 0
                        is_fc = bool(lvl.get(12, [(0, 0)])[0][1]) if 12 in lvl else False
                        is_ab = bool(lvl.get(13, [(0, 0)])[0][1]) if 13 in lvl else False

                        if mid in URA_KAGURA_LUNATIC_SONGS:
                            base_id = 2120000 + mid * 10
                            if is_ab:
                                emblems.add(base_id + 1)
                                emblems.add(base_id + 2)
                                emblems.add(base_id + 3)
                            elif is_fc:
                                emblems.add(base_id + 2)
                                emblems.add(base_id + 3)
                            elif clear_cnt > 0:
                                emblems.add(base_id + 3)
            except Exception as e:
                logger.debug("Failed parsing PlayMusicRecord for key %s: %s", k, e)

        # ---------------------------------------------------------------------
        # 3. HeroineStatus-- and CharacterStatus-- (Lv 50 / Lv 100 milestones)
        # ---------------------------------------------------------------------
        elif k.startswith("HeroineStatus--"):
            try:
                fields = parse_raw_protobuf(v)
                chara_id = fields.get(2, [(0, 0)])[0][1]
                exp = fields.get(8, [(0, 0)])[0][1]
                if chara_id > 0:
                    if exp >= 49:
                        emblems.add(1000000 + chara_id)
                    if exp >= 99:
                        emblems.add(1100000 + chara_id)
            except Exception as e:
                logger.debug("Failed parsing HeroineStatus: %s", e)

        elif k.startswith("CharacterStatus--"):
            try:
                fields = parse_raw_protobuf(v)
                chara_id = fields.get(1, [(0, 0)])[0][1]
                exp = fields.get(4, [(0, 0)])[0][1]
                if chara_id > 0:
                    if exp >= 49:
                        emblems.add(1000000 + chara_id)
                    if exp >= 99:
                        emblems.add(1100000 + chara_id)
            except Exception as e:
                logger.debug("Failed parsing CharacterStatus: %s", e)

        # ---------------------------------------------------------------------
        # 4. ExpeditionMissionRecordEntityCollecttion--1 (Expedition totals)
        # ---------------------------------------------------------------------
        elif k.startswith("ExpeditionMissionRecordEntityCollecttion"):
            try:
                fields = parse_raw_protobuf(v)
                tot_expeditions = 0
                tot_super = 0
                for _, rec_raw in fields.get(2, []):
                    if isinstance(rec_raw, (bytes, bytearray)):
                        rec = parse_raw_protobuf(rec_raw)
                        succ = rec.get(2, [(0, 0)])[0][1] if 2 in rec else 0
                        great = rec.get(3, [(0, 0)])[0][1] if 3 in rec else 0
                        super_cnt = rec.get(4, [(0, 0)])[0][1] if 4 in rec else 0
                        tot_expeditions += succ + great + super_cnt
                        tot_super += super_cnt

                for thresh, eid in EXPEDITION_TOTAL_THRESHOLDS:
                    if tot_expeditions >= thresh:
                        emblems.add(eid)

                for thresh, eid in EXPEDITION_SUPER_THRESHOLDS:
                    if tot_super >= thresh:
                        emblems.add(eid)
            except Exception as e:
                logger.debug("Failed parsing Expedition records: %s", e)

        # ---------------------------------------------------------------------
        # 5. SpotStatus--<spot_id> (Reconstruction progress)
        # ---------------------------------------------------------------------
        elif k.startswith("SpotStatus--"):
            try:
                fields = parse_raw_protobuf(v)
                sid = fields.get(1, [(0, 0)])[0][1] if 1 in fields else 0
                reconst = fields.get(9, [(0, 0)])[0][1] if 9 in fields else 0

                # Reconstructed when reconstruction >= 100
                if sid > 0 and reconst >= 100:
                    if sid in SPOT_RECONSTRUCTION_MAP:
                        emblems.add(SPOT_RECONSTRUCTION_MAP[sid])
                    reconstructed_spots.add(sid)
            except Exception as e:
                logger.debug("Failed parsing SpotStatus: %s", e)

    # Check if all 15 base spots are reconstructed
    if BASE_SPOT_IDS.issubset(reconstructed_spots):
        emblems.add(2000028)
        emblems.add(2000036)

    return emblems
