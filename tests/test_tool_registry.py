"""Guards against a helper slipping between an @mcp.tool decorator and its tool."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mfp_mcp import server  # noqa: E402


def test_only_tools_are_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert all(n.startswith("mfp_") for n in names), names
    assert {"mfp_get_diary", "mfp_remove_food_from_diary", "mfp_add_food_to_diary"} <= names


def test_each_tool_wraps_the_function_of_the_same_name():
    for tool in server.mcp._tool_manager.list_tools():
        assert tool.fn.__name__ == tool.name, (tool.name, tool.fn.__name__)
