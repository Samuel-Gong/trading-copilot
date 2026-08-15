from __future__ import annotations

import time

import pytest
from fastapi import Request, Response

from app.api import auth as auth_api
from app.config import settings
from app.services import auth


def _request() -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": [],
        "client": ("203.0.113.10", 12345),
    })


def test_login_cookie_is_secure_when_production_setting_enabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(auth_api.auth, "is_configured", lambda: True)
    monkeypatch.setattr(auth_api.auth, "verify_and_create_session", lambda _password: "token")
    response = Response()

    result = auth_api.login(auth_api.LoginIn(password="secret"), _request(), response)

    assert result == {"ok": True, "authenticated": True}
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie


def test_login_cookie_remains_usable_for_local_http_development(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(auth_api.auth, "is_configured", lambda: True)
    monkeypatch.setattr(auth_api.auth, "verify_and_create_session", lambda _password: "token")
    response = Response()

    auth_api.login(auth_api.LoginIn(password="secret"), _request(), response)

    assert "Secure" not in response.headers["set-cookie"]


def test_secure_production_boot_invalidates_legacy_non_secure_sessions(monkeypatch) -> None:
    saved: list[dict] = []
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(auth, "_load", lambda: {
        "password_hash": "hash",
        "sessions": {"legacy-token": time.time() + 3600},
    })
    monkeypatch.setattr(auth, "_save", lambda data: saved.append(data.copy()))
    monkeypatch.setattr(auth, "_sessions", {"already-loaded": time.time() + 3600})

    auth._restore_sessions()

    assert auth._sessions == {}
    assert saved == [{
        "password_hash": "hash",
        "sessions": {},
        "cookie_secure": True,
    }]


def test_secure_session_migration_never_overwrites_malformed_auth_file(
    monkeypatch,
    tmp_path,
) -> None:
    auth_file = tmp_path / "auth.json"
    original = "{broken-json"
    auth_file.write_text(original, encoding="utf-8")
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(auth, "_path", lambda: auth_file)
    monkeypatch.setattr(auth, "_sessions", {"legacy-token": time.time() + 3600})

    with pytest.raises(auth.AuthDataError):
        auth._restore_sessions()

    assert auth_file.read_text(encoding="utf-8") == original
