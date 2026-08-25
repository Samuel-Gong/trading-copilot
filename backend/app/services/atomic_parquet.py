"""小型 Parquet 数据集的原子发布辅助。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any


FileWriter = Callable[[Path], None]


def _stage_file(writer: FileWriter, target: Path) -> Path:
    """在目标目录完整写入并同步临时文件，失败时不触碰目标。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        writer(temporary)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    """尽力同步目录元数据；Windows 等不支持目录 fsync 时安全降级。"""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_recovery_journal(
    journal_path: Path,
    backups: dict[Path, Path | None],
) -> None:
    payload = {
        "version": 1,
        "targets": [
            {
                "target": str(target.resolve()),
                "backup": str(backup.resolve()) if backup is not None else None,
            }
            for target, backup in backups.items()
        ],
    }
    temporary = _stage_file(
        lambda path: path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        ),
        journal_path,
    )
    try:
        os.replace(temporary, journal_path)
        _fsync_directory(journal_path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _journal_entries(journal_path: Path) -> list[tuple[Path, Path | None]]:
    try:
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"原子发布恢复日志损坏: {journal_path}") from exc
    if payload.get("version") != 1 or not isinstance(payload.get("targets"), list):
        raise ValueError(f"原子发布恢复日志格式不受支持: {journal_path}")

    root = journal_path.parent.resolve()
    entries: list[tuple[Path, Path | None]] = []
    for item in payload["targets"]:
        if not isinstance(item, dict) or not isinstance(item.get("target"), str):
            raise ValueError(f"原子发布恢复日志目标无效: {journal_path}")
        target = Path(item["target"])
        backup_value = item.get("backup")
        if backup_value is not None and not isinstance(backup_value, str):
            raise ValueError(f"原子发布恢复日志副本无效: {journal_path}")
        backup = Path(backup_value) if isinstance(backup_value, str) else None
        paths = [target, *(value for value in (backup,) if value is not None)]
        if any(not path.resolve().is_relative_to(root) for path in paths):
            raise ValueError(f"原子发布恢复日志目标越界: {journal_path}")
        entries.append((target, backup))
    return entries


def _restore_from_backup(target: Path, backup: Path | None) -> None:
    """恢复旧目标但保留 backup，使恢复可重试。"""
    if backup is None:
        target.unlink(missing_ok=True)
        _fsync_directory(target.parent)
        return
    if not backup.exists():
        raise OSError(f"恢复副本不存在: {backup}")
    temporary = _stage_file(
        lambda path: shutil.copy2(backup, path),
        target,
    )
    try:
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def recover_file_set(journal_path: Path) -> bool:
    """若上次发布被进程退出中断，则从持久化日志恢复完整旧快照。"""
    if not journal_path.exists():
        return False
    entries = _journal_entries(journal_path)
    for target, backup in reversed(entries):
        _restore_from_backup(target, backup)
    for _target, backup in entries:
        if backup is not None:
            backup.unlink(missing_ok=True)
    journal_path.unlink()
    _fsync_directory(journal_path.parent)
    return True


def replace_file_set(
    entries: Iterable[tuple[Path, FileWriter]],
    *,
    journal_path: Path | None = None,
) -> None:
    """先完整暂存一组文件，再逐个替换；失败时恢复，亦可跨进程恢复。"""
    items = list(entries)
    targets = [target for target, _writer in items]
    if len(targets) != len(set(targets)):
        raise ValueError("原子发布目标不能重复")
    if journal_path is not None:
        recover_file_set(journal_path)

    staged: list[tuple[Path, Path]] = []
    backups: dict[Path, Path | None] = {}
    published: list[Path] = []
    preserved_backups: set[Path] = set()
    publish_succeeded = False
    try:
        for target, writer in items:
            staged.append((target, _stage_file(writer, target)))

        for target, _temporary in staged:
            if not target.exists():
                backups[target] = None
                continue
            backup = target.with_name(f".{target.name}.{uuid.uuid4().hex}.bak")
            try:
                os.link(target, backup)
            except OSError:
                try:
                    shutil.copy2(target, backup)
                except BaseException:
                    backup.unlink(missing_ok=True)
                    raise
            backups[target] = backup
            try:
                with backup.open("rb") as stream:
                    os.fsync(stream.fileno())
            except OSError:
                pass
            _fsync_directory(backup.parent)

        if journal_path is not None:
            _write_recovery_journal(journal_path, backups)

        for target, temporary in staged:
            os.replace(temporary, target)
            published.append(target)
            _fsync_directory(target.parent)
        publish_succeeded = True
    except BaseException as publish_error:
        rollback_failed = False
        for target in reversed(published):
            backup = backups.get(target)
            try:
                if journal_path is not None:
                    _restore_from_backup(target, backup)
                elif backup is not None and backup.exists():
                    os.replace(backup, target)
                else:
                    target.unlink(missing_ok=True)
            except BaseException as rollback_error:
                rollback_failed = True
                if backup is not None and backup.exists():
                    preserved_backups.add(backup)
                publish_error.add_note(
                    f"回滚 {target} 失败，恢复副本已保留: {rollback_error}"
                )
        if journal_path is not None and journal_path.exists() and not rollback_failed:
            journal_path.unlink()
            _fsync_directory(journal_path.parent)
        raise
    finally:
        for _target, temporary in staged:
            temporary.unlink(missing_ok=True)
        if journal_path is not None and publish_succeeded and journal_path.exists():
            journal_path.unlink()
            _fsync_directory(journal_path.parent)
        for backup in backups.values():
            journal_needs_backup = journal_path is not None and journal_path.exists()
            if (
                backup is not None
                and backup not in preserved_backups
                and not journal_needs_backup
            ):
                backup.unlink(missing_ok=True)


def replace_parquet_set(
    entries: Iterable[tuple[Path, Any]],
    *,
    journal_path: Path | None = None,
) -> None:
    """原子发布一组 Parquet 文件。"""
    replace_file_set(
        [
            (target, lambda path, frame=frame: frame.write_parquet(path))
            for target, frame in entries
        ],
        journal_path=journal_path,
    )


def write_parquet_atomic(frame: Any, target: Path) -> None:
    """完整写入单个临时文件后原子替换目标。"""
    replace_parquet_set([(target, frame)])
