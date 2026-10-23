"""Unit tests for loading session cookies from MFP_COOKIES_JSON."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mfp_mcp import server  # noqa: E402


def test_env_cookies_take_precedence(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "COOKIES_FILE", tmp_path / "cookies.json")
    monkeypatch.setenv("MFP_COOKIES_JSON", '{"__Secure-next-auth.session-token": "abc"}')
    assert server.load_cookies() == {"__Secure-next-auth.session-token": "abc"}


def test_invalid_env_cookies_fall_back_to_file(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "COOKIES_FILE", tmp_path / "cookies.json")
    monkeypatch.setenv("MFP_COOKIES_JSON", "not json")
    assert server.load_cookies() is None
