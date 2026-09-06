"""市场环境派生数据写入的进程内串行化边界。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar, cast

from app.services.atomic_parquet import recover_file_set

_F = TypeVar("_F", bound=Callable[..., Any])
_UPDATE_LOCK = threading.RLock()


def market_environment_journal_path(data_dir: Path) -> Path:
    """阶段/主线全量发布的跨进程恢复日志。"""
    return data_dir / ".market_environment_publish.json"


@contextmanager
def market_environment_snapshot(data_dir: Path):
    """让组合读取与整组发布共享边界，并恢复被进程退出中断的发布。"""
    with _UPDATE_LOCK:
        recover_file_set(market_environment_journal_path(data_dir))
        yield


def _find_data_dir(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Path | None:
    direct = kwargs.get("data_dir")
    if isinstance(direct, Path):
        return direct
    for value in args:
        if isinstance(value, Path):
            return value
    for value in args:
        request_state = getattr(getattr(value, "app", None), "state", None)
        request_repo = getattr(request_state, "repo", None)
        candidates = (
            getattr(getattr(value, "store", None), "data_dir", None),
            getattr(getattr(request_repo, "store", None), "data_dir", None),
        )
        for candidate in candidates:
            if isinstance(candidate, Path):
                return candidate
    return None


def serialized_market_environment_update(func: _F) -> _F:
    """让 regime/mainline 的完整计算与落盘区间共享同一可重入锁。"""

    @wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _UPDATE_LOCK:
            data_dir = _find_data_dir(args, kwargs)
            if data_dir is not None:
                recover_file_set(market_environment_journal_path(data_dir))
            return func(*args, **kwargs)

    return cast(_F, wrapped)
