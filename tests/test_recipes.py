"""Unit tests for recipe listing, matching, and the recipe -> food id scan."""
from __future__ import annotations

import sys
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

    def __init__(self, recipes=None, diary=None):
        self.session = self
        self._recipes = recipes or []
        self._diary = diary or {}

    def get(self, url, params=None, headers=None, timeout=None):
        if url.endswith("/v2/recipes"):
            return _Resp({"items": self._recipes})
        return _Resp({"items": self._diary.get(params["entry_date"], [])})


def _recipe(id_, name, version, servings=4, calories=2000):
    return {
        "id": id_, "name": name, "version": version, "servings": servings,
        "nutritional_contents": {"energy": {"value": calories}, "protein": 100},
    }


def test_list_recipes_reports_per_serving_and_normalises_names():
    client = _FakeClient(recipes=[_recipe("1", "Chili\xa0(540g)", "v1")])
    [r] = server.list_recipes(client)
    assert r["name"] == "Chili (540g)"
    assert r["per_serving"]["calories"] == 500.0
    assert r["per_serving"]["protein"] == 25.0


def test_name_matches_needs_every_query_word():
    assert server._name_matches("liver onions", "Liver and Onions")
    assert not server._name_matches("liver bacon", "Liver and Onions")


def test_scan_maps_recipe_versions_to_newest_food_id():
    from datetime import date, timedelta

    today = date.today()
    day = lambda n: (today - timedelta(days=n)).strftime("%Y-%m-%d")  # noqa: E731
    food = lambda id_, version, type_="recipe": {"food": {"id": id_, "version": version, "type": type_}}  # noqa: E731
    client = _FakeClient(diary={
        day(0): [food("new", "v1"), food("plain", "v9", "food")],
        day(5): [food("old", "v1"), food("other", "v2")],
    })
    assert server._scan_recipe_foods(client, days=10) == {"v1": "new", "v2": "other"}
