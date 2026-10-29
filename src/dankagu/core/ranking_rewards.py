"""Milestone Ranking Rewards Distribution via Mail.

Provides instant rewards delivery to player mailbox as soon as the player
achieves a specified rank or tier milestone in Events and Medley Grand Prix.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from dankagu.models.inventory import PlayerInventoryItem
from dankagu.models.ranking import EventLeaderboard
from dankagu.models.storage import PlayerStorageEntry

logger = logging.getLogger("dankagu.ranking_rewards")

# Pre-compiled ranking reward tiers extracted from authentic master tables
# (EventRankingReward-score38.raw, EventRankingReward-point38.raw,
#  GpxRankingReward-class1.raw, GpxRankingReward-group1.raw)

SCORE38_TIERS: list[dict[str, Any]] = [
    {
        "tier_id": 1,
        "rank_from": 1,
        "rank_to": 1,
        "achievement_key": "EventRankingReward_score38_1",
        "items": [["Emblem", 3003801, 1]],
    },
    {
        "tier_id": 2,
        "rank_from": 2,
        "rank_to": 2,
        "achievement_key": "EventRankingReward_score38_2",
        "items": [["Emblem", 3003802, 1]],
    },
    {
        "tier_id": 3,
        "rank_from": 3,
        "rank_to": 3,
        "achievement_key": "EventRankingReward_score38_3",
        "items": [["Emblem", 3003803, 1]],
    },
    {
        "tier_id": 4,
        "rank_from": 4,
        "rank_to": 10,
        "achievement_key": "EventRankingReward_score38_4",
        "items": [["Emblem", 3003804, 1]],
    },
    {
        "tier_id": 5,
        "rank_from": 11,
        "rank_to": 100,
        "achievement_key": "EventRankingReward_score38_5",
        "items": [["Emblem", 3003805, 1]],
    },
    {
        "tier_id": 6,
        "rank_from": 101,
        "rank_to": 500,
        "achievement_key": "EventRankingReward_score38_6",
        "items": [["Emblem", 3003806, 1]],
    },
    {
        "tier_id": 7,
        "rank_from": 501,
        "rank_to": 1000,
        "achievement_key": "EventRankingReward_score38_7",
        "items": [["Emblem", 3003807, 1]],
    },
    {
        "tier_id": 8,
        "rank_from": 1001,
        "rank_to": 5000,
        "achievement_key": "EventRankingReward_score38_8",
        "items": [["Emblem", 3003808, 1]],
    },
    {
        "tier_id": 9,
        "rank_from": 5001,
        "rank_to": 10000,
        "achievement_key": "EventRankingReward_score38_9",
        "items": [["Emblem", 3003809, 1]],
    },
    {
        "tier_id": 10,
        "rank_from": 10001,
        "rank_to": 50000,
        "achievement_key": "EventRankingReward_score38_10",
        "items": [["Emblem", 3003810, 1]],
    },
    {
        "tier_id": 11,
        "rank_from": 50001,
        "rank_to": 100000,
        "achievement_key": "EventRankingReward_score38_11",
        "items": [["Emblem", 3003811, 1]],
    },
    {
        "tier_id": 12,
        "rank_from": 100001,
        "rank_to": 500000,
        "achievement_key": "EventRankingReward_score38_12",
        "items": [["Emblem", 3003812, 1]],
    },
]

POINT38_TIERS: list[dict[str, Any]] = [
    {
        "tier_id": 1,
        "rank_from": 1,
        "rank_to": 1,
        "achievement_key": "EventRankingReward_point38_1",
        "items": [
            ["Card", 100369, 12],
            ["Card", 100370, 17],
            ["Growth", 32004, 1],
            ["Growth", 32003, 2],
            ["Growth", 30005, 50],
        ],
    },
    {
        "tier_id": 2,
        "rank_from": 2,
        "rank_to": 2,
        "achievement_key": "EventRankingReward_point38_2",
        "items": [
            ["Card", 100369, 12],
            ["Card", 100370, 17],
            ["Growth", 32004, 1],
            ["Growth", 32003, 1],
            ["Growth", 30005, 40],
        ],
    },
    {
        "tier_id": 3,
        "rank_from": 3,
        "rank_to": 3,
        "achievement_key": "EventRankingReward_point38_3",
        "items": [
            ["Card", 100369, 12],
            ["Card", 100370, 17],
            ["Growth", 32004, 1],
            ["Growth", 32003, 1],
            ["Growth", 30005, 30],
        ],
    },
    {
        "tier_id": 4,
        "rank_from": 4,
        "rank_to": 10,
        "achievement_key": "EventRankingReward_point38_4",
        "items": [
            ["Card", 100369, 12],
            ["Card", 100370, 17],
            ["Growth", 32003, 1],
            ["Growth", 30005, 20],
            ["GenericItem", 32006, 40],
            ["GenericItem", 32005, 40],
        ],
    },
    {
        "tier_id": 5,
        "rank_from": 11,
        "rank_to": 100,
        "achievement_key": "EventRankingReward_point38_5",
        "items": [
            ["Card", 100369, 12],
            ["Card", 100370, 17],
            ["Growth", 32003, 1],
            ["Growth", 30005, 20],
            ["GenericItem", 32006, 30],
            ["GenericItem", 32005, 30],
        ],
    },
    {
        "tier_id": 6,
        "rank_from": 101,
        "rank_to": 1000,
        "achievement_key": "EventRankingReward_point38_6",
        "items": [
            ["Card", 100369, 12],
            ["Card", 100370, 17],
            ["Growth", 32003, 1],
            ["Growth", 30005, 20],
            ["GenericItem", 32006, 20],
            ["GenericItem", 32005, 20],
        ],
    },
    {
        "tier_id": 7,
        "rank_from": 1001,
        "rank_to": 5000,
        "achievement_key": "EventRankingReward_point38_7",
        "items": [
            ["Card", 100369, 5],
            ["Card", 100370, 10],
            ["Growth", 30005, 10],
            ["GenericItem", 32006, 10],
            ["GenericItem", 32005, 10],
        ],
    },
    {
        "tier_id": 8,
        "rank_from": 5001,
        "rank_to": 10000,
        "achievement_key": "EventRankingReward_point38_8",
        "items": [
            ["Card", 100369, 5],
            ["Card", 100370, 10],
            ["Growth", 30005, 10],
            ["GenericItem", 32006, 5],
            ["GenericItem", 32005, 5],
        ],
    },
    {
        "tier_id": 9,
        "rank_from": 10001,
        "rank_to": 20000,
        "achievement_key": "EventRankingReward_point38_9",
        "items": [["Card", 100369, 3], ["Card", 100370, 8], ["Growth", 30005, 5]],
    },
    {
        "tier_id": 10,
        "rank_from": 20001,
        "rank_to": 50000,
        "achievement_key": "EventRankingReward_point38_10",
        "items": [["Card", 100369, 1], ["Card", 100370, 5], ["Growth", 30005, 3]],
    },
    {
        "tier_id": 11,
        "rank_from": 50001,
        "rank_to": 100000,
        "achievement_key": "EventRankingReward_point38_11",
        "items": [["Card", 100370, 3]],
    },
    {
        "tier_id": 12,
        "rank_from": 100001,
        "rank_to": 200000,
        "achievement_key": "EventRankingReward_point38_12",
        "items": [["Card", 100370, 2]],
    },
    {
        "tier_id": 13,
        "rank_from": 200001,
        "rank_to": 3000000,
        "achievement_key": "EventRankingReward_point38_13",
        "items": [["Card", 100370, 1]],
    },
]

GPX_CLASS_TIERS: list[dict[str, Any]] = [
    {
        "class_id": 1,
        "items": [
            ["VC", 90001, 6000],
            ["HeroineFragment", 20000, 4000],
            ["Growth", 30005, 200],
            ["Growth", 31013, 10],
            ["Growth", 31004, 50],
            ["Growth", 31014, 10],
            ["Growth", 31008, 50],
            ["Growth", 31015, 10],
            ["Growth", 31012, 50],
            ["Growth", 32004, 2],
            ["Growth", 32003, 6],
            ["Coin", 10001, 800000],
        ],
    },
    {
        "class_id": 2,
        "items": [
            ["VC", 90001, 4500],
            ["HeroineFragment", 20000, 3000],
            ["Growth", 30005, 160],
            ["Growth", 31013, 7],
            ["Growth", 31004, 40],
            ["Growth", 31014, 7],
            ["Growth", 31008, 40],
            ["Growth", 31015, 7],
            ["Growth", 31012, 40],
            ["Growth", 32004, 1],
            ["Growth", 32003, 4],
            ["Coin", 10001, 600000],
        ],
    },
    {
        "class_id": 3,
        "items": [
            ["VC", 90001, 3000],
            ["HeroineFragment", 20000, 2000],
            ["Growth", 30005, 120],
            ["Growth", 31013, 5],
            ["Growth", 31004, 30],
            ["Growth", 31014, 5],
            ["Growth", 31008, 30],
            ["Growth", 31015, 5],
            ["Growth", 31012, 30],
            ["Growth", 32003, 2],
            ["GenericItem", 32006, 40],
            ["Coin", 10001, 500000],
        ],
    },
    {
        "class_id": 4,
        "items": [
            ["VC", 90001, 1500],
            ["HeroineFragment", 20000, 1000],
            ["Growth", 30005, 90],
            ["Growth", 31013, 3],
            ["Growth", 31004, 20],
            ["Growth", 31014, 3],
            ["Growth", 31008, 20],
            ["Growth", 31015, 3],
            ["Growth", 31012, 20],
            ["GenericItem", 32006, 20],
            ["GenericItem", 32005, 30],
            ["Coin", 10001, 300000],
        ],
    },
]

GPX_GROUP_TIERS: list[dict[str, Any]] = [
    {
        "tier_id": 1,
        "class_id": 1,
        "rank_from": 1,
        "rank_to": 1,
        "items": [["GenericItem", 70010, 2500], ["VC", 90001, 1000]],
    },
    {
        "tier_id": 2,
        "class_id": 1,
        "rank_from": 2,
        "rank_to": 2,
        "items": [["GenericItem", 70010, 2400], ["VC", 90001, 950]],
    },
    {
        "tier_id": 3,
        "class_id": 1,
        "rank_from": 3,
        "rank_to": 3,
        "items": [["GenericItem", 70010, 2300], ["VC", 90001, 900]],
    },
    {
        "tier_id": 4,
        "class_id": 1,
        "rank_from": 4,
        "rank_to": 4,
        "items": [["GenericItem", 70010, 2200]],
    },
    {
        "tier_id": 5,
        "class_id": 1,
        "rank_from": 5,
        "rank_to": 5,
        "items": [["GenericItem", 70010, 2150]],
    },
    {
        "tier_id": 6,
        "class_id": 1,
        "rank_from": 6,
        "rank_to": 6,
        "items": [["GenericItem", 70010, 2100]],
    },
    {
        "tier_id": 7,
        "class_id": 1,
        "rank_from": 7,
        "rank_to": 7,
        "items": [["GenericItem", 70010, 2050]],
    },
    {
        "tier_id": 8,
        "class_id": 1,
        "rank_from": 8,
        "rank_to": 8,
        "items": [["GenericItem", 70010, 2000]],
    },
    {
        "tier_id": 9,
        "class_id": 1,
        "rank_from": 9,
        "rank_to": 9,
        "items": [["GenericItem", 70010, 1950]],
    },
    {
        "tier_id": 10,
        "class_id": 1,
        "rank_from": 10,
        "rank_to": 10,
        "items": [["GenericItem", 70010, 1940]],
    },
    {
        "tier_id": 11,
        "class_id": 1,
        "rank_from": 11,
        "rank_to": 11,
        "items": [["GenericItem", 70010, 1930]],
    },
    {
        "tier_id": 12,
        "class_id": 1,
        "rank_from": 12,
        "rank_to": 12,
        "items": [["GenericItem", 70010, 1920]],
    },
    {
        "tier_id": 13,
        "class_id": 1,
        "rank_from": 13,
        "rank_to": 13,
        "items": [["GenericItem", 70010, 1910]],
    },
    {
        "tier_id": 14,
        "class_id": 1,
        "rank_from": 14,
        "rank_to": 14,
        "items": [["GenericItem", 70010, 1900]],
    },
    {
        "tier_id": 15,
        "class_id": 1,
        "rank_from": 15,
        "rank_to": 15,
        "items": [["GenericItem", 70010, 1890]],
    },
    {
        "tier_id": 16,
        "class_id": 1,
        "rank_from": 16,
        "rank_to": 16,
        "items": [["GenericItem", 70010, 1880]],
    },
    {
        "tier_id": 17,
        "class_id": 1,
        "rank_from": 17,
        "rank_to": 17,
        "items": [["GenericItem", 70010, 1870]],
    },
    {
        "tier_id": 18,
        "class_id": 1,
        "rank_from": 18,
        "rank_to": 18,
        "items": [["GenericItem", 70010, 1860]],
    },
    {
        "tier_id": 19,
        "class_id": 1,
        "rank_from": 19,
        "rank_to": 19,
        "items": [["GenericItem", 70010, 1850]],
    },
    {
        "tier_id": 20,
        "class_id": 1,
        "rank_from": 20,
        "rank_to": 99999999,
        "items": [["GenericItem", 70010, 1840]],
    },
    {
        "tier_id": 21,
        "class_id": 2,
        "rank_from": 1,
        "rank_to": 1,
        "items": [["GenericItem", 70010, 1800], ["VC", 90001, 850]],
    },
    {
        "tier_id": 22,
        "class_id": 2,
        "rank_from": 2,
        "rank_to": 2,
        "items": [["GenericItem", 70010, 1750], ["VC", 90001, 800]],
    },
    {
        "tier_id": 23,
        "class_id": 2,
        "rank_from": 3,
        "rank_to": 3,
        "items": [["GenericItem", 70010, 1700], ["VC", 90001, 750]],
    },
    {
        "tier_id": 24,
        "class_id": 2,
        "rank_from": 4,
        "rank_to": 4,
        "items": [["GenericItem", 70010, 1650]],
    },
    {
        "tier_id": 25,
        "class_id": 2,
        "rank_from": 5,
        "rank_to": 5,
        "items": [["GenericItem", 70010, 1625]],
    },
    {
        "tier_id": 26,
        "class_id": 2,
        "rank_from": 6,
        "rank_to": 6,
        "items": [["GenericItem", 70010, 1600]],
    },
    {
        "tier_id": 27,
        "class_id": 2,
        "rank_from": 7,
        "rank_to": 7,
        "items": [["GenericItem", 70010, 1575]],
    },
    {
        "tier_id": 28,
        "class_id": 2,
        "rank_from": 8,
        "rank_to": 8,
        "items": [["GenericItem", 70010, 1550]],
    },
    {
        "tier_id": 29,
        "class_id": 2,
        "rank_from": 9,
        "rank_to": 9,
        "items": [["GenericItem", 70010, 1525]],
    },
    {
        "tier_id": 30,
        "class_id": 2,
        "rank_from": 10,
        "rank_to": 10,
        "items": [["GenericItem", 70010, 1510]],
    },
    {
        "tier_id": 31,
        "class_id": 2,
        "rank_from": 11,
        "rank_to": 11,
        "items": [["GenericItem", 70010, 1500]],
    },
    {
        "tier_id": 32,
        "class_id": 2,
        "rank_from": 12,
        "rank_to": 12,
        "items": [["GenericItem", 70010, 1490]],
    },
    {
        "tier_id": 33,
        "class_id": 2,
        "rank_from": 13,
        "rank_to": 13,
        "items": [["GenericItem", 70010, 1480]],
    },
    {
        "tier_id": 34,
        "class_id": 2,
        "rank_from": 14,
        "rank_to": 14,
        "items": [["GenericItem", 70010, 1470]],
    },
    {
        "tier_id": 35,
        "class_id": 2,
        "rank_from": 15,
        "rank_to": 15,
        "items": [["GenericItem", 70010, 1460]],
    },
    {
        "tier_id": 36,
        "class_id": 2,
        "rank_from": 16,
        "rank_to": 16,
        "items": [["GenericItem", 70010, 1450]],
    },
    {
        "tier_id": 37,
        "class_id": 2,
        "rank_from": 17,
        "rank_to": 17,
        "items": [["GenericItem", 70010, 1440]],
    },
    {
        "tier_id": 38,
        "class_id": 2,
        "rank_from": 18,
        "rank_to": 18,
        "items": [["GenericItem", 70010, 1430]],
    },
    {
        "tier_id": 39,
        "class_id": 2,
        "rank_from": 19,
        "rank_to": 19,
        "items": [["GenericItem", 70010, 1420]],
    },
    {
        "tier_id": 40,
        "class_id": 2,
        "rank_from": 20,
        "rank_to": 99999999,
        "items": [["GenericItem", 70010, 1410]],
    },
    {
        "tier_id": 41,
        "class_id": 3,
        "rank_from": 1,
        "rank_to": 1,
        "items": [["GenericItem", 70010, 1300], ["VC", 90001, 700]],
    },
    {
        "tier_id": 42,
        "class_id": 3,
        "rank_from": 2,
        "rank_to": 2,
        "items": [["GenericItem", 70010, 1250], ["VC", 90001, 650]],
    },
    {
        "tier_id": 43,
        "class_id": 3,
        "rank_from": 3,
        "rank_to": 3,
        "items": [["GenericItem", 70010, 1200], ["VC", 90001, 600]],
    },
    {
        "tier_id": 44,
        "class_id": 3,
        "rank_from": 4,
        "rank_to": 4,
        "items": [["GenericItem", 70010, 1150]],
    },
    {
        "tier_id": 45,
        "class_id": 3,
        "rank_from": 5,
        "rank_to": 5,
        "items": [["GenericItem", 70010, 1125]],
    },
    {
        "tier_id": 46,
        "class_id": 3,
        "rank_from": 6,
        "rank_to": 6,
        "items": [["GenericItem", 70010, 1100]],
    },
    {
        "tier_id": 47,
        "class_id": 3,
        "rank_from": 7,
        "rank_to": 7,
        "items": [["GenericItem", 70010, 1075]],
    },
    {
        "tier_id": 48,
        "class_id": 3,
        "rank_from": 8,
        "rank_to": 8,
        "items": [["GenericItem", 70010, 1050]],
    },
    {
        "tier_id": 49,
        "class_id": 3,
        "rank_from": 9,
        "rank_to": 9,
        "items": [["GenericItem", 70010, 1025]],
    },
    {
        "tier_id": 50,
        "class_id": 3,
        "rank_from": 10,
        "rank_to": 10,
        "items": [["GenericItem", 70010, 1010]],
    },
    {
        "tier_id": 51,
        "class_id": 3,
        "rank_from": 11,
        "rank_to": 11,
        "items": [["GenericItem", 70010, 1000]],
    },
    {
        "tier_id": 52,
        "class_id": 3,
        "rank_from": 12,
        "rank_to": 12,
        "items": [["GenericItem", 70010, 990]],
    },
    {
        "tier_id": 53,
        "class_id": 3,
        "rank_from": 13,
        "rank_to": 13,
        "items": [["GenericItem", 70010, 980]],
    },
    {
        "tier_id": 54,
        "class_id": 3,
        "rank_from": 14,
        "rank_to": 14,
        "items": [["GenericItem", 70010, 970]],
    },
    {
        "tier_id": 55,
        "class_id": 3,
        "rank_from": 15,
        "rank_to": 15,
        "items": [["GenericItem", 70010, 960]],
    },
    {
        "tier_id": 56,
        "class_id": 3,
        "rank_from": 16,
        "rank_to": 16,
        "items": [["GenericItem", 70010, 950]],
    },
    {
        "tier_id": 57,
        "class_id": 3,
        "rank_from": 17,
        "rank_to": 17,
        "items": [["GenericItem", 70010, 940]],
    },
    {
        "tier_id": 58,
        "class_id": 3,
        "rank_from": 18,
        "rank_to": 18,
        "items": [["GenericItem", 70010, 930]],
    },
    {
        "tier_id": 59,
        "class_id": 3,
        "rank_from": 19,
        "rank_to": 19,
        "items": [["GenericItem", 70010, 920]],
    },
    {
        "tier_id": 60,
        "class_id": 3,
        "rank_from": 20,
        "rank_to": 99999999,
        "items": [["GenericItem", 70010, 910]],
    },
    {
        "tier_id": 61,
        "class_id": 4,
        "rank_from": 1,
        "rank_to": 1,
        "items": [["GenericItem", 70010, 800], ["VC", 90001, 550]],
    },
    {
        "tier_id": 62,
        "class_id": 4,
        "rank_from": 2,
        "rank_to": 2,
        "items": [["GenericItem", 70010, 750], ["VC", 90001, 500]],
    },
    {
        "tier_id": 63,
        "class_id": 4,
        "rank_from": 3,
        "rank_to": 3,
        "items": [["GenericItem", 70010, 700], ["VC", 90001, 450]],
    },
    {
        "tier_id": 64,
        "class_id": 4,
        "rank_from": 4,
        "rank_to": 4,
        "items": [["GenericItem", 70010, 650]],
    },
    {
        "tier_id": 65,
        "class_id": 4,
        "rank_from": 5,
        "rank_to": 5,
        "items": [["GenericItem", 70010, 625]],
    },
    {
        "tier_id": 66,
        "class_id": 4,
        "rank_from": 6,
        "rank_to": 6,
        "items": [["GenericItem", 70010, 600]],
    },
    {
        "tier_id": 67,
        "class_id": 4,
        "rank_from": 7,
        "rank_to": 7,
        "items": [["GenericItem", 70010, 575]],
    },
    {
        "tier_id": 68,
        "class_id": 4,
        "rank_from": 8,
        "rank_to": 8,
        "items": [["GenericItem", 70010, 550]],
    },
    {
        "tier_id": 69,
        "class_id": 4,
        "rank_from": 9,
        "rank_to": 9,
        "items": [["GenericItem", 70010, 525]],
    },
    {
        "tier_id": 70,
        "class_id": 4,
        "rank_from": 10,
        "rank_to": 10,
        "items": [["GenericItem", 70010, 510]],
    },
    {
        "tier_id": 71,
        "class_id": 4,
        "rank_from": 11,
        "rank_to": 11,
        "items": [["GenericItem", 70010, 500]],
    },
    {
        "tier_id": 72,
        "class_id": 4,
        "rank_from": 12,
        "rank_to": 12,
        "items": [["GenericItem", 70010, 490]],
    },
    {
        "tier_id": 73,
        "class_id": 4,
        "rank_from": 13,
        "rank_to": 13,
        "items": [["GenericItem", 70010, 480]],
    },
    {
        "tier_id": 74,
        "class_id": 4,
        "rank_from": 14,
        "rank_to": 14,
        "items": [["GenericItem", 70010, 470]],
    },
    {
        "tier_id": 75,
        "class_id": 4,
        "rank_from": 15,
        "rank_to": 15,
        "items": [["GenericItem", 70010, 460]],
    },
    {
        "tier_id": 76,
        "class_id": 4,
        "rank_from": 16,
        "rank_to": 16,
        "items": [["GenericItem", 70010, 450]],
    },
    {
        "tier_id": 77,
        "class_id": 4,
        "rank_from": 17,
        "rank_to": 17,
        "items": [["GenericItem", 70010, 440]],
    },
    {
        "tier_id": 78,
        "class_id": 4,
        "rank_from": 18,
        "rank_to": 18,
        "items": [["GenericItem", 70010, 430]],
    },
    {
        "tier_id": 79,
        "class_id": 4,
        "rank_from": 19,
        "rank_to": 19,
        "items": [["GenericItem", 70010, 420]],
    },
    {
        "tier_id": 80,
        "class_id": 4,
        "rank_from": 20,
        "rank_to": 99999999,
        "items": [["GenericItem", 70010, 410]],
    },
]

CLASS_NAMES: dict[int, str] = {
    1: "プラチナ",
    2: "ゴールド",
    3: "シルバー",
    4: "ブロンズ",
}


def get_inventory_search_label(it_prefix: str) -> str:
    """Return authentic client SearchLabel for present box tabs."""
    if it_prefix in ("VC", "VirtualCurrency", "Stone"):
        return "VC"
    elif it_prefix == "Coin":
        return "Coin"
    elif it_prefix == "Card":
        return "Card"
    elif it_prefix in ("GachaTicket", "GachaTicketExchange"):
        return "GachaTicket"
    elif it_prefix == "SelectTicket":
        return "SelectTicket"
    elif it_prefix == "Emblem":
        return "Other"
    else:
        return "Item"


def get_inventory_schema_key(it_prefix: str) -> str:
    """Return authentic schema key for AppItemInfoBuilder.Build."""
    if it_prefix in ("VC", "VirtualCurrency", "Stone"):
        return "VC"
    elif it_prefix == "Emblem":
        return "EVENT_EMBLEM_DIRECT"
    else:
        return "ITEM"


def extract_unlocked_emblem_ids(blob: bytes | None) -> set[int]:
    """Extract set of unlocked emblem IDs from UnlockEntityStatusCollection--8 protobuf blob."""
    if not blob:
        return set()
    unlocked: set[int] = set()
    try:
        from dankagu.services.box_loot_box import parse_raw_protobuf

        fields = parse_raw_protobuf(blob)
        for _, sub_raw in fields.get(2, []):
            if isinstance(sub_raw, (bytes, bytearray)):
                sub = parse_raw_protobuf(sub_raw)
                uids = sub.get(4, [])
                if uids:
                    unlocked.add(uids[0][1])
    except Exception:
        pass
    return unlocked


def extract_stock_counts(storage_rows: list[PlayerStorageEntry]) -> dict[int, int]:
    """Extract item stock counts from GrowthItemStock--% and ItemStockStatus--% entries."""
    stocks: dict[int, int] = {}
    from dankagu.services.box_loot_box import parse_raw_protobuf

    for entry in storage_rows:
        if not entry.value:
            continue
        try:
            fields = parse_raw_protobuf(entry.value)
            f1 = fields.get(1)
            f2 = fields.get(2)
            if f1 and f2:
                stocks[f1[0][1]] = f2[0][1]
        except Exception:
            pass
    return stocks


def should_skip_reward_item(
    it_prefix: str,
    it_id: int,
    unlocked_emblems: set[int],
    stocks: dict[int, int],
) -> bool:
    """Return True if player already owns this unique item or has reached max capacity."""
    # 1. Unique Emblems (possession limit is 1)
    if it_prefix == "Emblem" and it_id in unlocked_emblems:
        return True

    # 2. Kaleidoscopes (possession limit is 99)
    if it_prefix == "Growth" and it_id in (32003, 32004) and stocks.get(it_id, 0) >= 99:
        return True

    # 3. Growth Materials (possession limit is 2500)
    if it_prefix == "Growth" and stocks.get(it_id, 0) >= 2500:
        return True

    # 4. Kaleidoscope Shards (comp_id = 13_000_000 + it_id, limit is 2500)
    if it_prefix == "GenericItem" and it_id in (32005, 32006):
        comp_id = 13_000_000 + it_id
        if stocks.get(comp_id, 0) >= 2500:
            return True

    return False


async def check_and_grant_event_ranking_milestones(
    session: AsyncSession,
    player_id: str,
    ranking_event_id: str,
    current_rank: int,
) -> int:
    """Check and distribute event ranking milestone rewards via mailbox.

    Plan A: Historical peak milestone rewards.
    If player achieves rank <= tier.rank_to, grant that tier reward if not already given.
    """
    if current_rank <= 0:
        return 0

    clean_key = ranking_event_id.lower()
    tiers: list[dict[str, Any]] = []
    category_code = ""
    category_title = "イベントポイントランキング"

    if "point" in clean_key:
        tiers = POINT38_TIERS
        category_code = "point38"
        category_title = "イベントポイントランキング"
    else:
        tiers = SCORE38_TIERS
        category_code = "score38"
        category_title = "イベントランキング"

    # 1. Identify all eligible tiers
    eligible_tiers = [t for t in tiers if current_rank <= t["rank_to"]]
    if not eligible_tiers:
        return 0

    candidate_labels = [f"rank_rew:{category_code}:tier_{t['tier_id']}" for t in eligible_tiers]

    # 2. Check already granted labels via system_resource_name or legacy search_label
    stmt = select(PlayerInventoryItem.system_resource_name, PlayerInventoryItem.search_label).where(
        PlayerInventoryItem.player_id == player_id,
        or_(
            PlayerInventoryItem.system_resource_name.like(f"rank_rew:{category_code}:tier_%"),
            PlayerInventoryItem.search_label.in_(candidate_labels),
        ),
    )
    existing_labels = set()
    for sys_name, s_label in (await session.execute(stmt)).all():
        if sys_name and sys_name.startswith(f"rank_rew:{category_code}:tier_"):
            existing_labels.add(sys_name.split("#")[0])
        if s_label and s_label.startswith(f"rank_rew:{category_code}:tier_"):
            existing_labels.add(s_label)

    # 3. Grant ungranted tiers
    now = int(time.time())
    new_items_count = 0

    # 3.1 Load player storage entries to check for unique/capped items
    storage_stmt = select(PlayerStorageEntry).where(
        PlayerStorageEntry.player_id == player_id,
        or_(
            PlayerStorageEntry.key == "UnlockEntityStatusCollection--8",
            PlayerStorageEntry.key.like("GrowthItemStock--%"),
            PlayerStorageEntry.key.like("ItemStockStatus--13032%"),
        ),
    )
    storage_rows = (await session.execute(storage_stmt)).scalars().all()
    unlocked_emblems: set[int] = set()
    stocks = extract_stock_counts(storage_rows)
    for entry in storage_rows:
        if entry.key == "UnlockEntityStatusCollection--8" and entry.value:
            unlocked_emblems = extract_unlocked_emblem_ids(entry.value)

    for tier in eligible_tiers:
        tier_label = f"rank_rew:{category_code}:tier_{tier['tier_id']}"
        if tier_label in existing_labels:
            continue

        rank_to = tier["rank_to"]
        for it_prefix, it_id, it_cnt in tier["items"]:
            if should_skip_reward_item(it_prefix, it_id, unlocked_emblems, stocks):
                continue

            item_type = 1 if it_prefix in ("VC", "VirtualCurrency", "Stone") else 0
            val_bytes = json.dumps({"Prefix": it_prefix, "Value": it_id, "Count": it_cnt}).encode(
                "utf-8"
            )

            msg = f"【{category_title}】{rank_to:,}位以内達成報酬！"
            if rank_to <= 3:
                msg = f"【{category_title}】第{rank_to}位入賞特別報酬！"

            inv_item = PlayerInventoryItem(
                id=str(uuid.uuid4()),
                player_id=player_id,
                item_type=item_type,
                schema_key=get_inventory_schema_key(it_prefix),
                value=val_bytes,
                route=4,  # Route.Achievement
                message=msg,
                search_label=get_inventory_search_label(it_prefix),
                opened_at=now,
                expired_at=now + 86400 * 90,
                system_resource_name=f"{tier_label}#{it_prefix}_{it_id}",
                system_resource_num=it_cnt,
                is_claimed=False,
                created_at=now,
            )
            session.add(inv_item)
            new_items_count += 1

        existing_labels.add(tier_label)

    return new_items_count


async def check_and_grant_gpx_milestones(
    session: AsyncSession,
    player_id: str,
    grand_prix_rank: int,
    group_rank: int,
    class_id: int,
) -> int:
    """Check and distribute Medley Grand Prix class and group ranking milestone rewards."""
    if grand_prix_rank <= 0 and group_rank <= 0:
        return 0

    now = int(time.time())
    new_items_count = 0
    class_name = CLASS_NAMES.get(class_id, "ブロンズ")

    # Load player storage entries to check for unique/capped items
    storage_stmt = select(PlayerStorageEntry).where(
        PlayerStorageEntry.player_id == player_id,
        or_(
            PlayerStorageEntry.key == "UnlockEntityStatusCollection--8",
            PlayerStorageEntry.key.like("GrowthItemStock--%"),
            PlayerStorageEntry.key.like("ItemStockStatus--13032%"),
        ),
    )
    storage_rows = (await session.execute(storage_stmt)).scalars().all()
    unlocked_emblems: set[int] = set()
    stocks = extract_stock_counts(storage_rows)
    for entry in storage_rows:
        if entry.key == "UnlockEntityStatusCollection--8" and entry.value:
            unlocked_emblems = extract_unlocked_emblem_ids(entry.value)

    # 1. Check Class Tier milestone (e.g. reaching Platinum / Gold / Silver / Bronze)
    class_label = f"rank_rew:gpx_class:c{class_id}"
    class_stmt = select(
        PlayerInventoryItem.system_resource_name, PlayerInventoryItem.search_label
    ).where(
        PlayerInventoryItem.player_id == player_id,
        or_(
            PlayerInventoryItem.system_resource_name.like(f"{class_label}%"),
            PlayerInventoryItem.search_label == class_label,
        ),
    )
    already_class = (await session.execute(class_stmt)).first() is not None

    if not already_class:
        for ct in GPX_CLASS_TIERS:
            if ct["class_id"] == class_id:
                for it_prefix, it_id, it_cnt in ct["items"]:
                    if should_skip_reward_item(it_prefix, it_id, unlocked_emblems, stocks):
                        continue

                    item_type = 1 if it_prefix in ("VC", "VirtualCurrency", "Stone") else 0
                    val_bytes = json.dumps(
                        {"Prefix": it_prefix, "Value": it_id, "Count": it_cnt}
                    ).encode("utf-8")
                    inv_item = PlayerInventoryItem(
                        id=str(uuid.uuid4()),
                        player_id=player_id,
                        item_type=item_type,
                        schema_key=get_inventory_schema_key(it_prefix),
                        value=val_bytes,
                        route=4,  # Route.Achievement
                        message=f"【メドレーグランプリ】クラス「{class_name}」達成報酬！",
                        search_label=get_inventory_search_label(it_prefix),
                        opened_at=now,
                        expired_at=now + 86400 * 90,
                        system_resource_name=f"{class_label}#{it_prefix}_{it_id}",
                        system_resource_num=it_cnt,
                        is_claimed=False,
                        created_at=now,
                    )
                    session.add(inv_item)
                    new_items_count += 1

    # 2. Check Group rank milestones within this class
    if group_rank > 0:
        eligible_group_tiers = [
            t for t in GPX_GROUP_TIERS if t["class_id"] == class_id and group_rank <= t["rank_to"]
        ]
        if eligible_group_tiers:
            candidate_labels = [
                f"rank_rew:gpx_group_c{class_id}:tier_{t['tier_id']}" for t in eligible_group_tiers
            ]
            stmt = select(
                PlayerInventoryItem.system_resource_name, PlayerInventoryItem.search_label
            ).where(
                PlayerInventoryItem.player_id == player_id,
                or_(
                    PlayerInventoryItem.system_resource_name.like(
                        f"rank_rew:gpx_group_c{class_id}:tier_%"
                    ),
                    PlayerInventoryItem.search_label.in_(candidate_labels),
                ),
            )
            rows = (await session.execute(stmt)).all()
            existing_labels = set()
            for sys_name, s_label in rows:
                if sys_name and sys_name.startswith(f"rank_rew:gpx_group_c{class_id}:tier_"):
                    existing_labels.add(sys_name.split("#")[0])
                if s_label and s_label.startswith(f"rank_rew:gpx_group_c{class_id}:tier_"):
                    existing_labels.add(s_label)

            for tier in eligible_group_tiers:
                tier_label = f"rank_rew:gpx_group_c{class_id}:tier_{tier['tier_id']}"
                if tier_label in existing_labels:
                    continue

                rank_to = tier["rank_to"]
                rank_str = f"第{rank_to}位" if rank_to <= 20 else "グループ参加"
                for it_prefix, it_id, it_cnt in tier["items"]:
                    if should_skip_reward_item(it_prefix, it_id, unlocked_emblems, stocks):
                        continue

                    item_type = 1 if it_prefix in ("VC", "VirtualCurrency", "Stone") else 0
                    val_bytes = json.dumps(
                        {"Prefix": it_prefix, "Value": it_id, "Count": it_cnt}
                    ).encode("utf-8")
                    inv_item = PlayerInventoryItem(
                        id=str(uuid.uuid4()),
                        player_id=player_id,
                        item_type=item_type,
                        schema_key=get_inventory_schema_key(it_prefix),
                        value=val_bytes,
                        route=4,  # Route.Achievement
                        message=f"【メドレーグランプリ】{class_name}グループ{rank_str}達成報酬！",
                        search_label=get_inventory_search_label(it_prefix),
                        opened_at=now,
                        expired_at=now + 86400 * 90,
                        system_resource_name=f"{tier_label}#{it_prefix}_{it_id}",
                        system_resource_num=it_cnt,
                        is_claimed=False,
                        created_at=now,
                    )
                    session.add(inv_item)
                    new_items_count += 1

                existing_labels.add(tier_label)

    return new_items_count


def extract_gpx_class_id(gpx_info_val: bytes | None) -> int:
    """Extract lastClassID (field 6) from GpxInfoStatus protobuf bytes."""
    if not gpx_info_val:
        return 1
    try:
        i = 0
        n = len(gpx_info_val)
        while i < n:
            tag = 0
            shift = 0
            while i < n:
                b = gpx_info_val[i]
                i += 1
                tag |= (b & 0x7F) << shift
                shift += 7
                if not (b & 0x80):
                    break
            field_num = tag >> 3
            wire_type = tag & 7
            if wire_type == 0:  # Varint
                v = 0
                shift = 0
                while i < n:
                    b = gpx_info_val[i]
                    i += 1
                    v |= (b & 0x7F) << shift
                    shift += 7
                    if not (b & 0x80):
                        break
                if field_num == 6:  # lastClassID
                    return v if 1 <= v <= 4 else 1
            elif wire_type == 1:
                i += 8
            elif wire_type == 2:
                length = 0
                shift = 0
                while i < n:
                    b = gpx_info_val[i]
                    i += 1
                    length |= (b & 0x7F) << shift
                    shift += 7
                    if not (b & 0x80):
                        break
                i += length
            elif wire_type == 5:
                i += 4
            else:
                break
    except Exception:
        pass
    return 1


def extract_event_info_status(val: bytes | None) -> tuple[int, int, int]:
    """Extract (event_id, event_point, max_score) from EventInfoStatusProto bytes."""
    if not val:
        return 0, 0, 0
    event_id = 0
    event_point = 0
    max_score = 0
    try:
        i = 0
        n = len(val)
        while i < n:
            tag = 0
            shift = 0
            while i < n:
                b = val[i]
                i += 1
                tag |= (b & 0x7F) << shift
                shift += 7
                if not (b & 0x80):
                    break
            field_num = tag >> 3
            wire_type = tag & 7
            if wire_type == 0:  # Varint
                v = 0
                shift = 0
                while i < n:
                    b = val[i]
                    i += 1
                    v |= (b & 0x7F) << shift
                    shift += 7
                    if not (b & 0x80):
                        break
                if field_num == 1:
                    event_id = v
                elif field_num == 2:
                    event_point = v
                elif field_num == 5:
                    max_score = v
            elif wire_type == 1:
                i += 8
            elif wire_type == 2:
                length = 0
                shift = 0
                while i < n:
                    b = val[i]
                    i += 1
                    length |= (b & 0x7F) << shift
                    shift += 7
                    if not (b & 0x80):
                        break
                i += length
            elif wire_type == 5:
                i += 4
            else:
                break
    except Exception:
        pass
    return event_id, event_point, max_score


async def check_and_grant_gpx_milestones_for_player(
    session: AsyncSession,
    player_id: str,
    score: int | None = None,
    gpx_info_bytes: bytes | None = None,
    ranking_key: str = "gpx_s1_p1005",
) -> int:
    """Convenience helper to compute player GPX rank and class, then grant milestones.

    If score is None, queries EventLeaderboard for player_id's score.
    """
    if score is None:
        stmt = select(EventLeaderboard.score).where(
            EventLeaderboard.ranking_event_id == ranking_key,
            EventLeaderboard.player_id == player_id,
        )
        score_val = (await session.execute(stmt)).scalar()
        if score_val is None or score_val <= 0:
            return 0
        score = score_val
    elif score <= 0:
        return 0

    # 1. Compute rank in grand prix
    higher_stmt = (
        select(func.count())
        .select_from(EventLeaderboard)
        .where(
            EventLeaderboard.ranking_event_id == ranking_key,
            EventLeaderboard.score > score,
        )
    )
    higher_count = (await session.execute(higher_stmt)).scalar() or 0
    rank = higher_count + 1

    # 2. Determine class_id
    class_id = 1
    if gpx_info_bytes is not None:
        class_id = extract_gpx_class_id(gpx_info_bytes)
    else:
        st_stmt = select(PlayerStorageEntry.value).where(
            PlayerStorageEntry.player_id == player_id,
            PlayerStorageEntry.key.like("GpxInfoStatus%"),
        )
        val = (await session.execute(st_stmt)).scalars().first()
        if val:
            class_id = extract_gpx_class_id(val)

    group_rank = min(rank, 20) if rank > 0 else 1
    return await check_and_grant_gpx_milestones(
        session,
        player_id,
        grand_prix_rank=rank,
        group_rank=group_rank,
        class_id=class_id,
    )
