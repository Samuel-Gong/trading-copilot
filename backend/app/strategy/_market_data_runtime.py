"""策略只读行情入口的内部运行态。

本模块不在策略 import 白名单中：仓库句柄与执行时点不能暴露给用户策略。
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from typing import Any

from app.market_time import cn_today

_repo: Any = None
_lock = threading.Lock()
_execution_as_of: ContextVar[date | None] = ContextVar(
    "strategy_market_data_as_of",
    default=None,
)


def get_repo() -> Any:
    global _repo
    if _repo is None:
        with _lock:
            if _repo is None:
                from app.tickflow.repository import DataStore, KlineRepository

                _repo = KlineRepository(DataStore())
    return _repo


def set_repo(repo: Any) -> None:
    """仅供框架初始化和测试注入。"""
    global _repo
    with _lock:
        _repo = repo


def reset_repo() -> None:
    """仅供测试清理。"""
    global _repo
    with _lock:
        _repo = None


@contextmanager
def execution_as_of(value: date) -> Iterator[None]:
    token = _execution_as_of.set(value)
    try:
        yield
    finally:
        _execution_as_of.reset(token)


def bounded_end(value: date | None) -> date:
    """把策略请求截止日约束在当前执行时点内。"""
    cutoff = _execution_as_of.get()
    if cutoff is None:
        if value is None:
            raise ValueError("策略行情读取缺少执行截止日")
        cutoff = cn_today()
    return cutoff if value is None else min(value, cutoff)
