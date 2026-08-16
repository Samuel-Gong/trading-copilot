"""策略页策略池偏好的持久化 HTTP 契约测试。"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from stat import S_IMODE
from threading import Event, Lock
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.settings import router
from app.config import settings
from app.services import preferences


def make_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_screener_strategy_pool_survives_application_restart(tmp_path, monkeypatch):
    first_client = make_client(tmp_path, monkeypatch)

    saved = first_client.put(
        "/api/settings/preferences/screener-strategy-pool",
        json={"strategy_ids": ["builtin_a", "custom_b"]},
    )

    assert saved.status_code == 200
    assert saved.json() == {"strategy_ids": ["builtin_a", "custom_b"]}

    restarted_client = make_client(tmp_path, monkeypatch)
    restored = restarted_client.get(
        "/api/settings/preferences/screener-strategy-pool",
    )

    assert restored.status_code == 200
    assert restored.json() == {"strategy_ids": ["builtin_a", "custom_b"]}


def test_screener_strategy_pool_distinguishes_unmigrated_and_empty(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)

    missing = client.get("/api/settings/preferences/screener-strategy-pool")
    assert missing.status_code == 200
    assert missing.json() == {"strategy_ids": None}

    saved = client.put(
        "/api/settings/preferences/screener-strategy-pool",
        json={"strategy_ids": []},
    )
    assert saved.status_code == 200
    assert saved.json() == {"strategy_ids": []}

    restored = make_client(tmp_path, monkeypatch).get(
        "/api/settings/preferences/screener-strategy-pool",
    )
    assert restored.json() == {"strategy_ids": []}


def test_screener_strategy_pool_normalizes_ids(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)

    saved = client.put(
        "/api/settings/preferences/screener-strategy-pool",
        json={"strategy_ids": [" builtin_a ", "", "builtin_a", "custom_b"]},
    )

    assert saved.status_code == 200
    assert saved.json() == {"strategy_ids": ["builtin_a", "custom_b"]}


def test_preferences_concurrent_updates_do_not_lose_strategy_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    preferences.save({"seed": True})

    original_load = preferences.load
    first_loaded = Event()
    second_loaded = Event()
    release_loads = Event()
    counter_lock = Lock()
    load_count = 0

    def delayed_load() -> dict:
        nonlocal load_count
        snapshot = original_load()
        with counter_lock:
            load_count += 1
            if load_count == 1:
                first_loaded.set()
            elif load_count == 2:
                second_loaded.set()
        assert release_loads.wait(timeout=3)
        return snapshot

    monkeypatch.setattr(preferences, "load", delayed_load)
    with ThreadPoolExecutor(max_workers=2) as executor:
        strategy_save = executor.submit(
            preferences.save,
            {"screener_strategy_pool": ["builtin_a"]},
        )
        assert first_loaded.wait(timeout=1)
        quote_save = executor.submit(preferences.save, {"last_fetch_ms": 123})
        second_loaded.wait(timeout=0.2)
        release_loads.set()
        strategy_save.result(timeout=3)
        quote_save.result(timeout=3)

    monkeypatch.setattr(preferences, "load", original_load)
    saved = preferences.load()
    assert saved["screener_strategy_pool"] == ["builtin_a"]
    assert saved["last_fetch_ms"] == 123


def test_preferences_atomic_save_keeps_secret_file_private(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    path = tmp_path / "user_data" / "preferences.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"feishu_webhook_secret":"synthetic-secret"}', encoding="utf-8")
    path.chmod(0o600)

    preferences.save({"screener_strategy_pool": ["builtin_a"]})

    assert S_IMODE(path.stat().st_mode) == 0o600
    assert preferences.get_feishu_webhook_secret() == "synthetic-secret"


def test_windows_atomic_save_binds_protected_dacl_during_create(tmp_path, monkeypatch):
    """Windows 临时文件必须在创建动作中原子绑定当前用户 DACL。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(preferences.os, "fchmod", None)
    monkeypatch.setattr(
        preferences.tempfile,
        "mkstemp",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("Windows 分支不得先创建继承目录 ACL 的临时文件")
        ),
    )

    user_sid = object()
    token = SimpleNamespace(closed=False)
    token.Close = lambda: setattr(token, "closed", True)
    calls = {}

    class FakeAcl:
        def AddAccessAllowedAce(self, revision, access_mask, sid):  # noqa: N802
            calls["ace"] = (revision, access_mask, sid)

    class FakeSecurityDescriptor:
        def Initialize(self):  # noqa: N802
            calls["security_descriptor_initialized"] = True

        def SetSecurityDescriptorDacl(  # noqa: N802
            self,
            present,
            dacl,
            defaulted,
        ):
            calls["security_descriptor_dacl"] = (present, dacl, defaulted)

        def SetSecurityDescriptorControl(  # noqa: N802
            self,
            control_bits,
            bits_to_set,
        ):
            calls["security_descriptor_control"] = (control_bits, bits_to_set)

    class FakeSecurityAttributes:
        def __init__(self):
            calls["security_attributes"] = self
            self.bInheritHandle = True
            self.SECURITY_DESCRIPTOR = FakeSecurityDescriptor()

    class FakeHandle:
        def __init__(self, path):
            self.stream = open(path, "xb")  # noqa: SIM115
            self.closed = False

        def Close(self):  # noqa: N802
            self.stream.close()
            self.closed = True

    fake_win32api = SimpleNamespace(GetCurrentProcess=lambda: "current-process")
    fake_win32con = SimpleNamespace(
        TOKEN_QUERY=8,
        FILE_ALL_ACCESS=0x1F01FF,
        GENERIC_WRITE=0x40000000,
        CREATE_NEW=1,
        FILE_ATTRIBUTE_NORMAL=0x80,
    )
    fake_win32security = SimpleNamespace(
        ACL=FakeAcl,
        ACL_REVISION=2,
        SECURITY_ATTRIBUTES=FakeSecurityAttributes,
        SE_DACL_PROTECTED=0x1000,
        TokenUser=1,
        OpenProcessToken=lambda process, access: (
            calls.update(open_token=(process, access)) or token
        ),
        GetTokenInformation=lambda opened_token, info_class: (
            calls.update(token_info=(opened_token, info_class)) or (user_sid, 0)
        ),
    )

    def create_file(*args):
        calls["create_file"] = args
        return FakeHandle(args[0])

    def write_file(handle, payload):
        written = handle.stream.write(payload)
        calls["write_file"] = (handle, payload)
        return 0, written

    def flush_file_buffers(handle):
        handle.stream.flush()
        calls["flushed_handle"] = handle

    fake_win32file = SimpleNamespace(
        CreateFile=create_file,
        WriteFile=write_file,
        FlushFileBuffers=flush_file_buffers,
    )
    monkeypatch.setitem(sys.modules, "win32api", fake_win32api)
    monkeypatch.setitem(sys.modules, "win32con", fake_win32con)
    monkeypatch.setitem(sys.modules, "win32file", fake_win32file)
    monkeypatch.setitem(sys.modules, "win32security", fake_win32security)

    preferences.save({"screener_strategy_pool": ["builtin_a"]})

    security_attributes = calls["security_attributes"]
    temporary_path, access, share, attributes, creation, flags, template = calls[
        "create_file"
    ]
    assert Path(temporary_path).parent == tmp_path / "user_data"
    assert access == fake_win32con.GENERIC_WRITE
    assert share == 0
    assert attributes is security_attributes
    assert creation == fake_win32con.CREATE_NEW
    assert flags == fake_win32con.FILE_ATTRIBUTE_NORMAL
    assert template is None
    assert calls["security_descriptor_initialized"] is True
    assert security_attributes.bInheritHandle is False
    assert calls["ace"] == (
        fake_win32security.ACL_REVISION,
        fake_win32con.FILE_ALL_ACCESS,
        user_sid,
    )
    assert calls["security_descriptor_dacl"][0::2] == (True, False)
    assert calls["security_descriptor_control"] == (
        fake_win32security.SE_DACL_PROTECTED,
        fake_win32security.SE_DACL_PROTECTED,
    )
    assert calls["flushed_handle"].closed is True
    assert token.closed is True
    assert preferences.get_screener_strategy_pool() == ["builtin_a"]


def test_preferences_atomic_save_fails_closed_when_windows_acl_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(preferences.os, "fchmod", None)

    def fail_to_build_security_attributes():
        raise PermissionError("synthetic ACL failure")

    monkeypatch.setattr(
        preferences,
        "_windows_private_security_attributes",
        fail_to_build_security_attributes,
    )

    with pytest.raises(PermissionError, match="synthetic ACL failure"):
        preferences.save({"screener_strategy_pool": ["builtin_a"]})

    user_data_dir = tmp_path / "user_data"
    assert not (user_data_dir / "preferences.json").exists()
    assert list(user_data_dir.iterdir()) == []


def test_windows_atomic_save_removes_incomplete_temporary_file(tmp_path, monkeypatch):
    target = tmp_path / "preferences.json"
    temporary_path = tmp_path / ".preferences.json.synthetic.tmp"
    calls = {}

    class FakeHandle:
        def __init__(self, path):
            self.stream = open(path, "xb")  # noqa: SIM115
            self.closed = False

        def Close(self):  # noqa: N802
            if not self.closed:
                self.stream.close()
                self.closed = True

    fake_win32con = SimpleNamespace(
        GENERIC_WRITE=0x40000000,
        CREATE_NEW=1,
        FILE_ATTRIBUTE_NORMAL=0x80,
    )

    def create_file(path, *_args):
        calls["handle"] = FakeHandle(path)
        return calls["handle"]

    def write_file(handle, payload):
        written = handle.stream.write(payload[:-1])
        return 0, written

    fake_win32file = SimpleNamespace(
        CreateFile=create_file,
        WriteFile=write_file,
        FlushFileBuffers=lambda _handle: calls.update(flushed=True),
    )
    monkeypatch.setattr(
        preferences,
        "_windows_private_security_attributes",
        lambda: (fake_win32con, fake_win32file, object()),
    )
    monkeypatch.setattr(
        preferences.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="synthetic"),
    )

    with pytest.raises(OSError, match="写入不完整"):
        preferences._create_windows_private_temporary_file(target, b"secret")

    assert calls["handle"].closed is True
    assert "flushed" not in calls
    assert not temporary_path.exists()


def test_strategy_pool_cleanup_is_atomic_with_concurrent_save(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    preferences.save({"screener_strategy_pool": ["target", "old"]})

    original_load = preferences.load
    save_loaded = Event()
    release_save = Event()
    load_lock = Lock()
    load_count = 0

    def delayed_first_load() -> dict:
        nonlocal load_count
        snapshot = original_load()
        with load_lock:
            load_count += 1
            should_delay = load_count == 1
        if should_delay:
            save_loaded.set()
            assert release_save.wait(timeout=3)
        return snapshot

    monkeypatch.setattr(preferences, "load", delayed_first_load)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pool_save = executor.submit(
            preferences.set_screener_strategy_pool,
            ["target", "old", "new"],
        )
        assert save_loaded.wait(timeout=1)
        cleanup = executor.submit(
            preferences.remove_screener_strategy_from_pool,
            "target",
        )
        release_save.set()
        pool_save.result(timeout=3)
        assert cleanup.result(timeout=3) == ["old", "new"]

    monkeypatch.setattr(preferences, "load", original_load)
    assert preferences.get_screener_strategy_pool() == ["old", "new"]
