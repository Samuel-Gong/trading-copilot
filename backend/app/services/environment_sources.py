"""扩展目录整体的版本边界，覆盖新建配置及数据写入，不持锁执行扫描。"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

_lock = threading.RLock()
_states: dict[str, tuple[int, int]] = {}


class EnvironmentSourceChangedError(RuntimeError):
    """计算期间来源变化，结果不能发布。"""


def _key(data_dir: Path) -> str:
    return str(data_dir.resolve())


@contextmanager
def ext_source_update(data_dir: Path):
    key = _key(data_dir)
    with _lock:
        version, active = _states.get(key, (0, 0))
        _states[key] = (version + 1, active + 1)
    try:
        yield
    finally:
        with _lock:
            version, active = _states[key]
            _states[key] = (version + 1, active - 1)


def ext_source_version(data_dir: Path) -> int:
    with _lock:
        version, active = _states.get(_key(data_dir), (0, 0))
        if active:
            raise EnvironmentSourceChangedError("扩展数据正在更新，请稍后重算")
        return version


@contextmanager
def ext_source_commit_guard(data_dir: Path, expected: int):
    with _lock:
        if ext_source_version(data_dir) != expected:
            raise EnvironmentSourceChangedError("计算期间扩展数据已更新，请重新计算")
        yield
