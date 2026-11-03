"""Minimal fallback event box rewards catalog for fresh installs and CI environments."""

FALLBACK_EVENT_BOX_REWARDS: dict[str, dict[str, list[dict]]] = {
    "32": {
        "1": [
            {
                "id": 1,
                "box_no": 1,
                "prefix": 1,
                "prefix_name": "Coin",
                "schema_key": "ITEM",
                "item_type": 0,
                "search_label": "Coin",
                "val": 90001,
                "count": 100,
                "reset": True,
                "weight": 1,
                "reward_count": 1,
            },
        ],
    },
    "38": {
        "1": [
            {
                "id": i,
                "box_no": 1,
                "prefix": 1 if i == 1 else 3,
                "prefix_name": "Coin" if i == 1 else "VC",
                "schema_key": "ITEM" if i == 1 else "VC",
                "item_type": 0 if i == 1 else 1,
                "search_label": "Coin" if i == 1 else "VC",
                "val": 90001 if i == 1 else 10001,
                "count": 100,
                "reset": i == 1,
                "weight": 1,
                "reward_count": 1,
            }
            for i in range(1, 15)
        ],
    },
}
