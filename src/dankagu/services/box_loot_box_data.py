"""Fallback event box rewards catalog for fresh installs and CI environments."""

from __future__ import annotations


def _make_mock_boxes() -> dict[str, list[dict]]:
    """Generate 13 minimal mock boxes for CI test execution."""
    boxes: dict[str, list[dict]] = {}
    for box_no in range(1, 14):
        boxes[str(box_no)] = [
            {
                "id": box_no * 100 + i,
                "box_no": box_no,
                "prefix": 1 if i == 0 else 3,
                "prefix_name": "Coin" if i == 0 else "VC",
                "schema_key": "ITEM" if i == 0 else "VC",
                "item_type": 0 if i == 0 else 1,
                "search_label": "Coin" if i == 0 else "VC",
                "val": 90001 if i == 0 else 10001,
                "count": 100,
                "reset": i == 0,
                "weight": 1,
                "reward_count": 10 if (box_no == 1 and i == 0) else 1,
            }
            for i in range(14 if box_no == 1 else 2)
        ]
    return boxes


FALLBACK_EVENT_BOX_REWARDS: dict[str, dict[str, list[dict]]] = {
    "32": _make_mock_boxes(),
    "38": _make_mock_boxes(),
}
