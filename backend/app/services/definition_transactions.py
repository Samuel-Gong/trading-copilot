"""用户可编辑定义与其引用图的数据目录级进程内事务。"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

_LOCKS_GUARD = threading.Lock()
_DATA_DIR_LOCKS: dict[str, threading.RLock] = {}


@contextmanager
def definitions_transaction(data_dir: Path):
    """串行化同一数据目录中的定义写入、引用检查与删除。"""
    key = str(data_dir.resolve())
    with _LOCKS_GUARD:
        lock = _DATA_DIR_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield
