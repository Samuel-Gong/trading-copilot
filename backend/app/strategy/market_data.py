"""策略可访问的指数/ETF 日K读取模块 — 白名单放行的只读数据入口。

供 Custom/AI 策略在 filter_history 内读取任意指数(及 ETF)的完整日K。
策略通过白名单 import 本模块, 调用纯读函数; 禁止写操作或任意文件访问。

设计要点:
  - 模块自身是框架侧信任代码, 对策略的沙箱逃逸拦截(ai_generator._validate_safety)照旧生效。
  - repo 句柄与执行时点只存在于非白名单内部模块, 公开模块无法取得底层对象。
  - 历史执行会把所有读取严格截断到 as_of, 显式未来 end 也不能越界。
  - 未知 symbol / 数据缺失 → 返回空 DataFrame(不抛), 与 repo 语义一致。
"""
from __future__ import annotations

import logging as _logging
from datetime import date as _date
from typing import Any as _Any

import polars as _pl

_logger = _logging.getLogger(__name__)

# 完整历史默认区间下界(A股数据远晚于此, 仅作"全量"占位)。
_FULL_START = _date(1990, 1, 1)


# ── 参数规范化 ─────────────────────────────────────────
def _norm_date(value, default: _date) -> _date:
    if value is None:
        return default
    if isinstance(value, str):
        return _date.fromisoformat(value)
    return value


def _validate_symbol(symbol: _Any) -> bool:
    return isinstance(symbol, str) and bool(symbol.strip())


def _range(start, end) -> tuple[_date, _date]:
    from app.strategy._market_data_runtime import bounded_end

    requested_end = _norm_date(end, _date.max) if end is not None else None
    return _norm_date(start, _FULL_START), bounded_end(requested_end)


def _repo():
    from app.strategy._market_data_runtime import get_repo

    return get_repo()


# ── 公开只读 API ───────────────────────────────────────
def get_index_daily(symbol, start=None, end=None, columns=None):
    """读取指数日K(含技术指标)。未知 symbol / 无数据返回空 DataFrame。"""
    if not _validate_symbol(symbol):
        _logger.warning("market_data: 非法指数 symbol %r", symbol)
        return _pl.DataFrame()
    try:
        s, e = _range(start, end)
        if s > e:
            return _pl.DataFrame()
        return _repo().get_index_daily(symbol, s, e, columns)
    except Exception as exc:
        _logger.warning("market_data get_index_daily failed %s: %s", symbol, exc)
        return _pl.DataFrame()


def get_etf_daily(symbol, start=None, end=None, columns=None):
    """读取 ETF 日K(含技术指标)。同 get_index_daily 语义。"""
    if not _validate_symbol(symbol):
        _logger.warning("market_data: 非法 ETF symbol %r", symbol)
        return _pl.DataFrame()
    try:
        s, e = _range(start, end)
        if s > e:
            return _pl.DataFrame()
        return _repo().get_etf_daily(symbol, s, e, columns)
    except Exception as exc:
        _logger.warning("market_data get_etf_daily failed %s: %s", symbol, exc)
        return _pl.DataFrame()


def get_daily(symbol, start=None, end=None, columns=None):
    """按资产类型自动分派读取日K: 指数 → get_index_daily; ETF → get_etf_daily; 股票 → get_daily。"""
    if not _validate_symbol(symbol):
        _logger.warning("market_data: 非法 symbol %r", symbol)
        return _pl.DataFrame()
    try:
        s, e = _range(start, end)
        if s > e:
            return _pl.DataFrame()
        repo = _repo()
        asset_type = repo.resolve_asset_type(symbol)
        if asset_type == "index":
            return repo.get_index_daily(symbol, s, e, columns)
        if asset_type == "etf":
            return repo.get_etf_daily(symbol, s, e, columns)
        return repo.get_daily(symbol, s, e, columns)
    except Exception as exc:
        _logger.warning("market_data get_daily failed %s: %s", symbol, exc)
        return _pl.DataFrame()


__all__ = [
    "get_daily",
    "get_etf_daily",
    "get_index_daily",
]
