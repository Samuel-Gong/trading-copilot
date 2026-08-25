"""多文件 Parquet 原子发布回归测试。"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from app.services import atomic_parquet
from app.services.atomic_parquet import replace_parquet_set


def test_staging_failure_preserves_complete_previous_snapshot(tmp_path) -> None:
    first = tmp_path / "history" / "part.parquet"
    second = tmp_path / "history" / "coverage.parquet"
    first.parent.mkdir(parents=True)
    pl.DataFrame({"value": [1]}).write_parquet(first)
    pl.DataFrame({"value": [2]}).write_parquet(second)

    class _FailingFrame:
        def write_parquet(self, path) -> None:
            path.write_bytes(b"partial")
            raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        replace_parquet_set([
            (first, pl.DataFrame({"value": [10]})),
            (second, _FailingFrame()),
        ])

    assert pl.read_parquet(first)["value"].to_list() == [1]
    assert pl.read_parquet(second)["value"].to_list() == [2]
    assert not list(first.parent.glob(".*.tmp"))
    assert not list(first.parent.glob(".*.bak"))


def test_publish_failure_rolls_back_already_replaced_files(tmp_path, monkeypatch) -> None:
    first = tmp_path / "history" / "part.parquet"
    second = tmp_path / "history" / "coverage.parquet"
    first.parent.mkdir(parents=True)
    pl.DataFrame({"value": [1]}).write_parquet(first)
    pl.DataFrame({"value": [2]}).write_parquet(second)
    original_replace = atomic_parquet.os.replace
    failed = False

    def fail_second_once(source, target) -> None:
        nonlocal failed
        if Path(target) == second and Path(source).suffix == ".tmp" and not failed:
            failed = True
            raise OSError("replace denied")
        original_replace(source, target)

    monkeypatch.setattr(atomic_parquet.os, "replace", fail_second_once)
    with pytest.raises(OSError, match="replace denied"):
        replace_parquet_set([
            (first, pl.DataFrame({"value": [10]})),
            (second, pl.DataFrame({"value": [20]})),
        ])

    assert pl.read_parquet(first)["value"].to_list() == [1]
    assert pl.read_parquet(second)["value"].to_list() == [2]
