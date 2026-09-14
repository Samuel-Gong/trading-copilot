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
    def keep_failing_second(source, target) -> None:
        if Path(target) == second and Path(source).suffix == ".tmp":
            raise OSError("replace denied")
        original_replace(source, target)

    monkeypatch.setattr(atomic_parquet.os, "replace", keep_failing_second)
    with pytest.raises(OSError, match="replace denied"):
        replace_parquet_set([
            (first, pl.DataFrame({"value": [10]})),
            (second, pl.DataFrame({"value": [20]})),
        ])

    assert pl.read_parquet(first)["value"].to_list() == [1]
    assert pl.read_parquet(second)["value"].to_list() == [2]


def test_rollback_failure_keeps_backup_and_continues(tmp_path, monkeypatch) -> None:
    targets = [tmp_path / "history" / f"{index}.parquet" for index in range(3)]
    targets[0].parent.mkdir(parents=True)
    for index, target in enumerate(targets):
        pl.DataFrame({"value": [index]}).write_parquet(target)
    original_replace = atomic_parquet.os.replace

    def fail_publish_and_one_rollback(source, target) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if target_path == targets[2] and source_path.suffix == ".tmp":
            raise OSError("publish denied")
        if target_path == targets[1] and source_path.suffix == ".bak":
            raise OSError("rollback denied")
        original_replace(source, target)

    monkeypatch.setattr(atomic_parquet.os, "replace", fail_publish_and_one_rollback)
    with pytest.raises(OSError, match="publish denied") as raised:
        replace_parquet_set([
            (target, pl.DataFrame({"value": [index + 10]}))
            for index, target in enumerate(targets)
        ])

    assert pl.read_parquet(targets[0])["value"].to_list() == [0]
    assert pl.read_parquet(targets[1])["value"].to_list() == [11]
    assert pl.read_parquet(targets[2])["value"].to_list() == [2]
    backups = list(targets[0].parent.glob(".*.bak"))
    assert len(backups) == 1
    assert pl.read_parquet(backups[0])["value"].to_list() == [1]
    assert any("回滚" in note for note in raised.value.__notes__)


def test_recovery_journal_restores_interrupted_file_set(tmp_path, monkeypatch) -> None:
    """回滚也失败时保留持久日志，下一次读取可恢复完整旧快照。"""
    first = tmp_path / "regime_history" / "part.parquet"
    second = tmp_path / "mainline_history" / "part.parquet"
    journal = tmp_path / ".market_environment_publish.json"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    pl.DataFrame({"value": [1]}).write_parquet(first)
    pl.DataFrame({"value": [2]}).write_parquet(second)
    original_replace = atomic_parquet.os.replace
    first_publish_finished = False

    def interrupt_publish_and_rollback(source, target) -> None:
        nonlocal first_publish_finished
        source_path = Path(source)
        target_path = Path(target)
        if target_path == first and source_path.suffix == ".tmp":
            if first_publish_finished:
                raise OSError("rollback denied")
            first_publish_finished = True
        elif target_path == second and source_path.suffix == ".tmp":
            raise OSError("publish denied")
        original_replace(source, target)

    monkeypatch.setattr(
        atomic_parquet.os,
        "replace",
        interrupt_publish_and_rollback,
    )
    with pytest.raises(OSError, match="publish denied"):
        replace_parquet_set(
            [
                (first, pl.DataFrame({"value": [10]})),
                (second, pl.DataFrame({"value": [20]})),
            ],
            journal_path=journal,
        )

    assert journal.exists()
    assert pl.read_parquet(first)["value"].to_list() == [10]
    assert pl.read_parquet(second)["value"].to_list() == [2]

    monkeypatch.setattr(atomic_parquet.os, "replace", original_replace)
    assert atomic_parquet.recover_file_set(journal)
    assert pl.read_parquet(first)["value"].to_list() == [1]
    assert pl.read_parquet(second)["value"].to_list() == [2]
    assert not journal.exists()
    assert not list(tmp_path.rglob(".*.bak"))


def test_recovery_commits_before_best_effort_backup_cleanup(
    tmp_path,
    monkeypatch,
) -> None:
    """恢复完成后先删除日志；副本清理失败不能让下一次读取重入恢复。"""
    target = tmp_path / "history" / "part.parquet"
    journal = tmp_path / ".publish.json"
    target.parent.mkdir(parents=True)
    pl.DataFrame({"value": [1]}).write_parquet(target)
    backup = target.with_name(".part.parquet.recovery.bak")
    backup.write_bytes(target.read_bytes())
    pl.DataFrame({"value": [10]}).write_parquet(target)
    atomic_parquet._write_recovery_journal(journal, {target: backup})
    original_unlink = Path.unlink

    def keep_backup(path, *args, **kwargs) -> None:
        if path == backup:
            raise OSError("backup busy")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", keep_backup)

    assert atomic_parquet.recover_file_set(journal)
    assert pl.read_parquet(target)["value"].to_list() == [1]
    assert not journal.exists()
    assert backup.exists()
    assert not atomic_parquet.recover_file_set(journal)


def test_committed_publish_ignores_backup_cleanup_failure(
    tmp_path,
    monkeypatch,
) -> None:
    """新快照与 journal 已提交后，垃圾回收失败不得向调用方报错。"""
    target = tmp_path / "history" / "part.parquet"
    journal = tmp_path / ".publish.json"
    target.parent.mkdir(parents=True)
    pl.DataFrame({"value": [1]}).write_parquet(target)
    original_unlink = Path.unlink

    def keep_backups(path, *args, **kwargs) -> None:
        if path.suffix == ".bak":
            raise OSError("backup busy")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", keep_backups)

    replace_parquet_set(
        [(target, pl.DataFrame({"value": [10]}))],
        journal_path=journal,
    )

    assert pl.read_parquet(target)["value"].to_list() == [10]
    assert not journal.exists()
    assert list(target.parent.glob(".*.bak"))
