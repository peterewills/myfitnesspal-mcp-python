"""Unit tests for loading session cookies from MFP_COOKIES_JSON."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

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


def test_saved_cookies_win_while_they_descend_from_env(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(server, "COOKIES_FILE", tmp_path / "cookies.json")
    monkeypatch.setenv("MFP_COOKIES_JSON", '{"t": "exported"}')
    server.save_cookies({"t": "refreshed"})
    assert server.load_cookies() == {"t": "refreshed"}


def test_new_export_replaces_saved_cookies(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(server, "COOKIES_FILE", tmp_path / "cookies.json")
    monkeypatch.setenv("MFP_COOKIES_JSON", '{"t": "exported"}')
    server.save_cookies({"t": "refreshed"})
    monkeypatch.setenv("MFP_COOKIES_JSON", '{"t": "re-exported"}')
    assert server.load_cookies() == {"t": "re-exported"}


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _fake_session_factory(body, refreshed_value):
    from http.cookiejar import Cookie

    class _FakeSession:
        def __init__(self):
            self.cookies = None

        def get(self, url, headers=None, timeout=None):
            # MFP sets the refreshed token host-only on www, beside the old one.
            self.cookies.set_cookie(Cookie(
                0, server.SESSION_COOKIE, refreshed_value, None, False,
                "www.myfitnesspal.com", False, False, "/", True, True,
                4_000_000_000, False, None, None, {},
            ))
            return _FakeResponse(200, body)

    return _FakeSession


def test_refresh_session_saves_the_refreshed_token(monkeypatch, tmp_path):
    import requests

    monkeypatch.setattr(server, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(server, "COOKIES_FILE", tmp_path / "cookies.json")
    monkeypatch.setenv("MFP_COOKIES_JSON", json.dumps({server.SESSION_COOKIE: "old"}))
    monkeypatch.setattr(
        requests, "Session", _fake_session_factory({"userId": "1", "expires": "x"}, "new")
    )
    server.refresh_session()
    assert server.load_cookies() == {server.SESSION_COOKIE: "new"}


def test_refresh_session_raises_on_dead_session(monkeypatch, tmp_path):
    import requests

    monkeypatch.setattr(server, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(server, "COOKIES_FILE", tmp_path / "cookies.json")
    monkeypatch.setenv("MFP_COOKIES_JSON", json.dumps({server.SESSION_COOKIE: "old"}))
    monkeypatch.setattr(requests, "Session", _fake_session_factory({}, "new"))
    with pytest.raises(RuntimeError, match="no longer valid"):
        server.refresh_session()
    assert not (tmp_path / "cookies.json").exists()
