"""因子回测按注册表交易日预热元数据加载历史。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.backtest.factor import FactorBacktestService, FactorConfig
from app.factors.registry import FactorSpec, register_factor, unregister_factor


class _CalendarEngine:
    def load_panel(self, _symbols, start, end, **_kwargs):
        days: list[date] = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                days.append(current)
            current += timedelta(days=1)
        size = len(days)
        return pl.DataFrame({
            "symbol": ["600000.SH"] * size,
            "date": days,
            "open": [10.0] * size,
            "high": [10.2] * size,
            "low": [9.8] * size,
            "close": [10.0 + index * 0.001 for index in range(size)],
            "volume": [10_000.0] * size,
            "amount": [10_000_000.0] * size,
            "turnover_rate": [0.5] * size,
        })


def _config(start: date) -> FactorConfig:
    return FactorConfig(
        factor_name="position_240d",
        symbols=["600000.SH"],
        start=start,
        end=start + timedelta(days=3),
    )


def test_builtin_240d_factor_is_warm_on_requested_start() -> None:
    start = date(2026, 9, 7)
    service = FactorBacktestService(_CalendarEngine())
    panel = service._load_factor_panel(_config(start), ["position_240d"])
    first = panel.filter(pl.col("date") == start)["position_240d"]
    assert len(first) == 1
    assert first[0] is not None


def test_nested_1023_bar_dsl_factor_is_warm_on_requested_start() -> None:
    factor_id = "cf_warmup_1023"
    register_factor(FactorSpec(
        id=factor_id,
        label="长窗口预热",
        group="测试",
        formula_text="ts_mean(ts_mean(close, 512), 512)",
        kind="custom",
        dependencies=frozenset({"close"}),
        warmup_bars=1023,
    ))
    try:
        start = date(2026, 9, 7)
        service = FactorBacktestService(_CalendarEngine())
        config = _config(start)
        config.factor_name = factor_id
        panel = service._load_factor_panel(config, [factor_id])
        first = panel.filter(pl.col("date") == start)[factor_id]
        assert len(first) == 1
        assert first[0] is not None
    finally:
        unregister_factor(factor_id)
