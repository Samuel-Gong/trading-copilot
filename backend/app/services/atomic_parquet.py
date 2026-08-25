"""小型 Parquet 数据集的原子发布辅助。"""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def _stage_parquet(frame: Any, target: Path) -> Path:
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
        frame.write_parquet(temporary)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def replace_parquet_set(entries: Iterable[tuple[Path, Any]]) -> None:
    """先完整暂存一组文件，再逐个原子替换；发布失败时恢复旧文件。"""
    items = list(entries)
    targets = [target for target, _frame in items]
    if len(targets) != len(set(targets)):
        raise ValueError("原子发布目标不能重复")

    staged: list[tuple[Path, Path]] = []
    backups: dict[Path, Path | None] = {}
    published: list[Path] = []
    preserved_backups: set[Path] = set()
    try:
        for target, frame in items:
            staged.append((target, _stage_parquet(frame, target)))

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

        for target, temporary in staged:
            os.replace(temporary, target)
            published.append(target)
    except BaseException as publish_error:
        for target in reversed(published):
            backup = backups.get(target)
            try:
                if backup is not None and backup.exists():
                    os.replace(backup, target)
                else:
                    target.unlink(missing_ok=True)
            except BaseException as rollback_error:
                if backup is not None and backup.exists():
                    preserved_backups.add(backup)
                publish_error.add_note(
                    f"回滚 {target} 失败，恢复副本已保留: {rollback_error}"
                )
        raise
    finally:
        for _target, temporary in staged:
            temporary.unlink(missing_ok=True)
        for backup in backups.values():
            if backup is not None and backup not in preserved_backups:
                backup.unlink(missing_ok=True)


def write_parquet_atomic(frame: Any, target: Path) -> None:
    """完整写入单个临时文件后原子替换目标。"""
    replace_parquet_set([(target, frame)])
