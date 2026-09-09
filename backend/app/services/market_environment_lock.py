"""市场环境派生数据写入的进程内串行化边界。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar, cast

from app.enriched_generation import enriched_commit_guard, get_enriched_generation
from app.services.atomic_parquet import recover_file_set, replace_parquet_set
from app.services.environment_sources import (
    EnvironmentSourceChangedError,
    ext_source_commit_guard,
    ext_source_version,
)
from app.services.update_slots import update_slot

_F = TypeVar("_F", bound=Callable[..., Any])
_UPDATE_LOCK = threading.RLock()
_SOURCE_SNAPSHOT: ContextVar[tuple | None] = ContextVar("market_source_snapshot", default=None)
_COMMIT_ACTIVE: ContextVar[Path | None] = ContextVar("market_commit_active", default=None)


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
    """更新资格覆盖整个计算，但只在最终发布时与读者互斥。"""

    @wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        data_dir = _find_data_dir(args, kwargs)
        if data_dir is None:
            raise ValueError("市场环境更新缺少数据目录")
        with update_slot("市场环境", data_dir):
            token = None
            if _SOURCE_SNAPSHOT.get() is None and not func.__name__.startswith("clear"):
                from app.services import preferences

                snapshot = (
                    data_dir,
                    get_enriched_generation(data_dir, "stock"),
                    get_enriched_generation(data_dir, "index"),
                    ext_source_version(data_dir),
                    preferences.get_mainline_filter_config(),
                )
                token = _SOURCE_SNAPSHOT.set(snapshot)
            try:
                with market_environment_snapshot(data_dir):
                    pass
                return func(*args, **kwargs)
            finally:
                if token is not None:
                    _SOURCE_SNAPSHOT.reset(token)

    return cast(_F, wrapped)


@contextmanager
def market_commit_guard(data_dir: Path):
    """短提交边界：来源复验与替换之间不允许其他来源写者插入。"""
    if _COMMIT_ACTIVE.get() == data_dir:
        yield
        return
    snapshot = _SOURCE_SNAPSHOT.get()
    with ExitStack() as stack:
        if snapshot is not None:
            from app.services import preferences

            path, stock, index, ext, filters = snapshot
            if path != data_dir:
                raise EnvironmentSourceChangedError("市场环境发布目录与计算目录不一致")
            for asset, expected in (("stock", stock), ("index", index)):
                actual = stack.enter_context(enriched_commit_guard(data_dir, asset))
                if actual != expected:
                    raise EnvironmentSourceChangedError("计算期间行情已更新，请重新计算")
            stack.enter_context(preferences.provider_route_lock())
            stack.enter_context(ext_source_commit_guard(data_dir, ext))
            if preferences.get_mainline_filter_config() != filters:
                raise EnvironmentSourceChangedError("计算期间主线过滤配置已更新，请重新计算")
        stack.enter_context(market_environment_snapshot(data_dir))
        token = _COMMIT_ACTIVE.set(data_dir)
        try:
            yield
        finally:
            _COMMIT_ACTIVE.reset(token)


def replace_market_parquet_set(entries, *, journal_path: Path) -> None:
    replace_parquet_set(
        entries, journal_path=journal_path,
        commit_guard=lambda: market_commit_guard(journal_path.parent),
    )


def write_market_parquet(frame, target: Path) -> None:
    replace_market_parquet_set(
        [(target, frame)],
        journal_path=market_environment_journal_path(target.parent.parent),
    )
