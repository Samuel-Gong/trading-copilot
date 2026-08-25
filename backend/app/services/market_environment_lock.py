"""市场环境派生数据写入的进程内串行化边界。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar, cast

_F = TypeVar("_F", bound=Callable[..., Any])
_UPDATE_LOCK = threading.RLock()


def serialized_market_environment_update(func: _F) -> _F:
    """让 regime/mainline 的完整计算与落盘区间共享同一可重入锁。"""

    @wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _UPDATE_LOCK:
            return func(*args, **kwargs)

    return cast(_F, wrapped)
