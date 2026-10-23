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


def test_diary_groups_by_meal_totals_and_remaining(monkeypatch):
    item = _item("a", "Breakfast", 0, "Eggs")
    item["nutritional_contents"] = {"energy": {"value": 150}, "protein": 12.0}
    item["serving_size"] = {"value": 2, "unit": "egg"}
    item["servings"] = 1.5
    client = _FakeClient([item])
    monkeypatch.setattr(
        server, "get_daily_goals", lambda c, d: {"calories": 2000.0, "protein": None}
    )
    diary = server.get_diary_v2(client, date(2026, 10, 22))
    [entry] = diary["meals"]["Breakfast"]["entries"]
    assert entry["amount"] == "3 egg"
    assert diary["meals"]["Lunch"]["entries"] == []
    assert diary["daily_totals"] == {"calories": 150.0, "protein": 12.0}
    assert diary["remaining"] == {"calories": 1850.0}


class _GoalsClient(_FakeClient):
    def __init__(self, rules):
        super().__init__([])
        self._rules = rules

    def get(self, url, params=None, headers=None, timeout=None):
        return _Resp({"items": self._rules})


def test_daily_goals_pick_rule_valid_on_date_and_its_weekday():
    def rule(valid_from, valid_to, calories):
        return {"valid_from": valid_from, "valid_to": valid_to, "daily_goals": [
            {"day_of_week": "thursday", "energy": {"value": calories}},
            {"day_of_week": "friday", "energy": {"value": calories + 1}},
        ]}

    client = _GoalsClient([rule("2026-01-01", "2026-10-11", 1800), rule("2026-10-12", "2100-01-01", 2700)])
    assert server.get_daily_goals(client, date(2026, 10, 22))["calories"] == 2700  # a Thursday
    assert server.get_daily_goals(client, date(2026, 10, 23))["calories"] == 2701
    assert server.get_daily_goals(client, date(2026, 10, 1))["calories"] == 1800
    assert server.get_daily_goals(client, date(2025, 1, 2)) is None
