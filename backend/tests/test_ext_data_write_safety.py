"""扩展数据并发写入与原子替换测试。"""
from __future__ import annotations

import threading
import time

import polars as pl
import pytest

from app.services import ext_data
from app.services.ext_data import (
    ExtConfig,
    ExtConfigChangedError,
    ExtConfigStore,
    ExtField,
)


def _config() -> ExtConfig:
    return ExtConfig(
        id="concurrent",
        label="并发写入",
        mode="snapshot",
        fields=[ExtField("symbol"), ExtField("value")],
    )


def test_same_config_read_modify_write_is_serialized(tmp_path, monkeypatch) -> None:
    config = _config()
    ExtConfigStore(tmp_path).create(config)
    ext_data.write_ext_parquet(
        pl.DataFrame({"symbol": ["A"], "value": ["0"]}),
        config,
        tmp_path,
    )
    target = tmp_path / "ext_data" / config.id / "part.parquet"
    original_read = pl.read_parquet
    first_read = threading.Event()
    second_read = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    read_count = 0
    count_lock = threading.Lock()

    def controlled_read(path, *args, **kwargs):
        nonlocal read_count
        if path == target:
            with count_lock:
                read_count += 1
                current = read_count
            if current == 1:
                first_read.set()
                assert release_first.wait(2)
            elif current == 2:
                second_read.set()
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(pl, "read_parquet", controlled_read)
    errors: list[BaseException] = []

    def write(symbol: str) -> None:
        try:
            if symbol == "C":
                second_started.set()
            ext_data.write_ext_parquet(
                pl.DataFrame({"symbol": [symbol], "value": [symbol]}),
                config,
                tmp_path,
            )
        except BaseException as exc:  # pragma: no cover - 线程断言汇总
            errors.append(exc)

    first = threading.Thread(target=write, args=("B",))
    second = threading.Thread(target=write, args=("C",))
    first.start()
    assert first_read.wait(1)
    second.start()
    assert second_started.wait(1)
    time.sleep(0.1)
    concurrent_read = second_read.is_set()
    release_first.set()
    first.join(2)
    second.join(2)

    assert not concurrent_read
    assert not errors
    assert not first.is_alive() and not second.is_alive()
    assert set(original_read(target)["symbol"].to_list()) == {"A", "B", "C"}


def test_atomic_parquet_failure_preserves_previous_file(tmp_path) -> None:
    target = tmp_path / "part.parquet"
    target.write_bytes(b"previous")

    class _FailingFrame:
        def write_parquet(self, path) -> None:
            path.write_bytes(b"partial")
            raise RuntimeError("write failed")

    with pytest.raises(RuntimeError, match="write failed"):
        ext_data._atomic_write_parquet(_FailingFrame(), target)

    assert target.read_bytes() == b"previous"


def test_deleted_config_rejects_stale_scheduler_write(tmp_path) -> None:
    """删除返回后，旧调度配置不得重新创建孤儿数据目录。"""
    store = ExtConfigStore(tmp_path)
    store.upsert(_config())
    stale = store.get("concurrent")
    assert stale is not None
    assert store.delete("concurrent") is True

    with pytest.raises(ExtConfigChangedError, match="已删除"):
        ext_data.write_ext_parquet(
            pl.DataFrame({"symbol": ["A"], "value": ["stale"]}),
            stale,
            tmp_path,
        )

    assert not (tmp_path / "ext_data" / "concurrent").exists()


def test_unversioned_config_cannot_create_data_directory(tmp_path) -> None:
    with pytest.raises(ExtConfigChangedError, match="缺少持久化修订号"):
        ext_data.write_ext_parquet(
            pl.DataFrame({"symbol": ["A"], "value": ["unversioned"]}),
            _config(),
            tmp_path,
        )

    assert not (tmp_path / "ext_data" / "concurrent").exists()


def test_deleted_config_rejects_stale_update(tmp_path) -> None:
    store = ExtConfigStore(tmp_path)
    store.create(_config())
    stale = store.get("concurrent")
    assert stale is not None
    assert store.delete("concurrent") is True

    stale.label = "陈旧更新"
    with pytest.raises(ExtConfigChangedError, match="已删除"):
        store.update(stale)

    assert not (tmp_path / "ext_data" / "concurrent").exists()


def test_stale_update_cannot_overwrite_newer_config(tmp_path) -> None:
    store = ExtConfigStore(tmp_path)
    store.create(_config())
    stale = store.get("concurrent")
    current = store.get("concurrent")
    assert stale is not None and current is not None
    current.label = "最新配置"
    store.update(current)

    stale.label = "陈旧配置"
    with pytest.raises(ExtConfigChangedError, match="已更新"):
        store.update(stale)

    assert store.get("concurrent").label == "最新配置"
