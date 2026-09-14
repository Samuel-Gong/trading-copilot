"""Secrets 持久化的并发与原子性回归。"""

from __future__ import annotations

import json
import threading

import pytest

from app import secrets_store


def test_concurrent_saves_to_different_fields_do_not_lose_updates(tmp_path, monkeypatch):
    path = tmp_path / "secrets.json"
    monkeypatch.setattr(secrets_store, "_path", lambda: path)
    original_load = secrets_store._load_path
    first_inside_lock = threading.Event()
    release_first = threading.Event()
    block_once = True

    def _blocking_load(target):
        nonlocal block_once
        if block_once:
            block_once = False
            first_inside_lock.set()
            assert release_first.wait(timeout=5)
        return original_load(target)

    monkeypatch.setattr(secrets_store, "_load_path", _blocking_load)
    errors: list[BaseException] = []

    def _save(payload):
        try:
            secrets_store.save(payload)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=_save, args=({"ai_api_key": "ai-secret"},))
    second = threading.Thread(target=_save, args=({"fuyao_api_key": "fuyao-secret"},))
    first.start()
    assert first_inside_lock.wait(timeout=5)
    second.start()
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not errors
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "ai_api_key": "ai-secret",
        "fuyao_api_key": "fuyao-secret",
    }
    assert path.stat().st_mode & 0o777 == 0o600


def test_atomic_write_failure_preserves_previous_secrets(tmp_path, monkeypatch):
    path = tmp_path / "secrets.json"
    monkeypatch.setattr(secrets_store, "_path", lambda: path)
    secrets_store.save({"ai_api_key": "old-secret"})
    before = path.read_bytes()

    def _fail_replace(_source, _target):
        raise OSError("injected replace failure")

    monkeypatch.setattr(secrets_store.os, "replace", _fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        secrets_store.save({"fuyao_api_key": "new-secret"})

    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".secrets.json.*.tmp"))


def test_windows_write_uses_protected_dacl_at_file_creation(tmp_path, monkeypatch):
    path = tmp_path / "secrets.json"
    monkeypatch.setattr(secrets_store, "_path", lambda: path)
    monkeypatch.setattr(secrets_store.os, "fchmod", None)
    monkeypatch.setattr(
        secrets_store.tempfile,
        "mkstemp",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("Windows 分支不得先创建继承目录 ACL 的临时文件")
        ),
    )
    calls: list[bytes] = []

    def _secure_create(target, payload):
        calls.append(payload)
        temporary = target.parent / ".secure.tmp"
        temporary.write_bytes(payload)
        return temporary

    monkeypatch.setattr(
        "app.services.preferences._create_windows_private_temporary_file",
        _secure_create,
    )

    secrets_store.save({"ai_api_key": "secret"})

    assert len(calls) == 1
    assert json.loads(path.read_text(encoding="utf-8")) == {"ai_api_key": "secret"}
