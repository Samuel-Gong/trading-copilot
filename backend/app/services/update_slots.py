"""按数据目录保留单写者资格；耗时操作不持有互斥锁。"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

_guard = threading.Lock()
_owners: dict[tuple[str, str], tuple[int, int]] = {}


class UpdateBusyError(RuntimeError):
    """已有同领域更新运行，调用方应稍后重试。"""


@contextmanager
def update_slot(domain: str, data_dir: Path):
    key = (domain, str(data_dir.resolve()))
    owner = threading.get_ident()
    with _guard:
        current, depth = _owners.get(key, (owner, 0))
        if current != owner:
            raise UpdateBusyError(f"{domain}更新正在运行，请稍后重试")
        _owners[key] = (owner, depth + 1)
    try:
        yield
    finally:
        with _guard:
            current, depth = _owners[key]
            if depth == 1:
                del _owners[key]
            else:
                _owners[key] = (current, depth - 1)
