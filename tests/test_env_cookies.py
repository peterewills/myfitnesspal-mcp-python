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


def _cookie(name, value):
    from http.cookiejar import Cookie

    # MFP sets refreshed cookies host-only on www, beside the seeded copy.
    return Cookie(
        0, name, value, None, False, "www.myfitnesspal.com", False, False, "/", True,
        True, None, False, None, None, {},
    )


def _fake_session_factory(auth_ok=True, nextauth_ok=True):
    class _FakeSession:
        def __init__(self):
            self.cookies = None

        def get(self, url, headers=None, timeout=None):
            if "/user/auth_token" in url:
                if not auth_ok:
                    return _FakeResponse(401, {"error": "unauthorized"})
                self.cookies.set_cookie(_cookie("_mfp_session", "rotated"))
                return _FakeResponse(200, {"access_token": "a", "user_id": "1"})
            if nextauth_ok:
                self.cookies.set_cookie(_cookie(server.SESSION_COOKIE, "new"))
                return _FakeResponse(200, {"userId": "1", "expires": "x"})
            return _FakeResponse(200, {})

    return _FakeSession


def _seed(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(server, "COOKIES_FILE", tmp_path / "cookies.json")
    monkeypatch.setenv(
        "MFP_COOKIES_JSON", json.dumps({server.SESSION_COOKIE: "old", "_mfp_session": "old"})
    )


def test_refresh_saves_rotated_cookies(monkeypatch, tmp_path):
    import requests

    _seed(monkeypatch, tmp_path)
    monkeypatch.setattr(requests, "Session", _fake_session_factory())
    server.refresh_session()
    assert server.load_cookies() == {server.SESSION_COOKIE: "new", "_mfp_session": "rotated"}


def test_refresh_succeeds_without_nextauth(monkeypatch, tmp_path):
    import requests

    _seed(monkeypatch, tmp_path)
    monkeypatch.setattr(requests, "Session", _fake_session_factory(nextauth_ok=False))
    server.refresh_session()
    assert server.load_cookies() == {server.SESSION_COOKIE: "old", "_mfp_session": "rotated"}


def test_refresh_raises_and_saves_nothing_when_auth_token_fails(monkeypatch, tmp_path):
    import requests

    _seed(monkeypatch, tmp_path)
    monkeypatch.setattr(requests, "Session", _fake_session_factory(auth_ok=False))
    with pytest.raises(RuntimeError, match="no longer accepts"):
        server.refresh_session()
    assert not (tmp_path / "cookies.json").exists()


def test_keepalive_exits_after_repeated_failures(monkeypatch):
    import threading

    calls = []

    def failing_refresh():
        calls.append(1)
        raise RuntimeError("dead")

    def fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(server, "refresh_session", failing_refresh)
    monkeypatch.setattr(server.os, "_exit", fake_exit)
    stop = threading.Event()
    monkeypatch.setattr(stop, "wait", lambda timeout: False)
    with pytest.raises(SystemExit):
        server._keepalive_loop(stop)
    assert len(calls) == server.REFRESH_MAX_FAILURES


def test_keepalive_resets_failures_after_a_success(monkeypatch):
    import threading

    outcomes = iter([False, False, True, False, False, True, "stop"])

    def refresh():
        if not next(outcomes):
            raise RuntimeError("blip")

    stop = threading.Event()
    waits = []

    def wait(timeout):
        waits.append(timeout)
        return len(waits) == 6

    monkeypatch.setattr(server, "refresh_session", refresh)
    monkeypatch.setattr(server.os, "_exit", lambda code: pytest.fail("exited"))
    monkeypatch.setattr(stop, "wait", wait)
    server._keepalive_loop(stop)
    assert waits[2] == server.KEEPALIVE_INTERVAL.total_seconds()
