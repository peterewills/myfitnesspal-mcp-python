"""Unit tests for v2 diary entry listing and matching entry ids to scraped entries."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mfp_mcp import server  # noqa: E402


class _Resp:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _FakeClient:
    access_token = "t"
    user_id = "u"

    def __init__(self, items):
        self.session = self
        self._items = items

    def get(self, url, params=None, headers=None, timeout=None):
        return _Resp({"items": self._items})


def _item(id_, meal, pos, desc, deleted=False):
    return {
        "type": "food_entry",
        "id": id_,
        "meal_name": meal,
        "meal_position": pos,
        "food": {"description": desc},
        "servings": 1.0,
        "serving_size": {"unit": "serving"},
        "nutritional_contents": {"energy": {"value": 100}},
        "deleted": deleted,
    }


def test_list_sorts_by_meal_and_position_and_drops_deleted():
    client = _FakeClient([
        _item("c", "Snacks", 0, "Apple"),
        _item("b", "Breakfast", 1, "Toast"),
        _item("a", "Breakfast", 0, "Eggs"),
        _item("x", "Lunch", 0, "Gone", deleted=True),
    ])
    entries = server.list_diary_entries(client, date(2026, 10, 22))
    assert [e["entry_id"] for e in entries] == ["a", "b", "c"]


def test_claim_entry_id_pops_matches_in_order():
    v2 = [
        {"entry_id": "1", "meal": "Snacks", "name": "Apple"},
        {"entry_id": "2", "meal": "Snacks", "name": "Apple"},
    ]
    assert server._claim_entry_id(v2, "snacks", "Apple, 1 small") == "1"
    assert server._claim_entry_id(v2, "snacks", "Apple, 1 small") == "2"
    assert server._claim_entry_id(v2, "snacks", "Apple, 1 small") is None
