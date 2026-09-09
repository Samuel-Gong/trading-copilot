"""同步新增 Matrix 策略的固定样本与参数边界回归。"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from app.backtest.matrix import build_market_data_matrix
from app.strategy.builtin import (
    active_limit_gene,
    breakout_new_high_60d,
    long_lower_shadow_reversal,
    ma_convergence_breakout,
    macd_below_zero_revival,
    platform_consolidation_breakout,
    rsi_midline_pullback,
)


def _market(
    *,
    size: int = 80,
    open_: np.ndarray | None = None,
    high: np.ndarray | None = None,
    low: np.ndarray | None = None,
    close: np.ndarray | None = None,
    fields: dict[str, np.ndarray] | None = None,
):
    close = np.asarray(close if close is not None else np.full(size, 10.0))
    open_ = np.asarray(open_ if open_ is not None else close)
    high = np.asarray(high if high is not None else np.maximum(open_, close) + 0.1)
    low = np.asarray(low if low is not None else np.minimum(open_, close) - 0.1)
    data = {
        "symbol": ["600001.SH"] * size,
        "date": [date(2026, 1, 1) + timedelta(days=i) for i in range(size)],
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": np.full(size, 1_000.0),
    }
    data.update(fields or {})
    return build_market_data_matrix(
        pl.DataFrame(data), field_columns=set(fields or {})
    )


def _assert_only_last_entry(signals) -> None:
    assert signals.entry[-1, 0] == 1
    assert signals.entry[:-1, 0].sum() == 0
    assert signals.entry_signal_ids
    assert signals.exit_signal_ids


def test_active_limit_gene_fixed_sample_and_parameter_boundary() -> None:
    count = np.zeros(80)
    change = np.full(80, 0.08)
    volume_ratio = np.full(80, 2.0)
    count[-1], change[-1], volume_ratio[-1] = 2.0, 0.029, 0.5
    market = _market(fields={
        "limit_up_count_60d": count,
        "change_pct": change,
        "vol_ratio_5d": volume_ratio,
    })

    signals = active_limit_gene.MATRIX_STRATEGY.compute_signals(
        market,
        {"min_limit_count": 2, "vol_ratio_max": 0.5, "max_change_pct": 3.0},
    )

    _assert_only_last_entry(signals)
    assert active_limit_gene.META["asset_types"] == ["stock"]
    assert "consecutive_limit_ups" in active_limit_gene.MATRIX_STRATEGY.required_fields()


def test_breakout_new_high_fixed_sample_and_volume_switch() -> None:
    close = np.full(80, 9.5)
    close[-1] = 10.5
    prior_high = np.full(80, 10.0)
    change = np.zeros(80)
    change[-1] = 0.02
    volume_ratio = np.ones(80)
    market = _market(
        close=close,
        fields={
            "high_60d": prior_high,
            "change_pct": change,
            "vol_ratio_5d": volume_ratio,
            "ma20": np.full(80, 9.0),
        },
    )

    blocked = breakout_new_high_60d.MATRIX_STRATEGY.compute_signals(
        market, {"require_volume": True, "vol_ratio_min": 1.3}
    )
    allowed = breakout_new_high_60d.MATRIX_STRATEGY.compute_signals(
        market, {"require_volume": False, "min_change_pct": 2.0}
    )

    assert blocked.entry[-1, 0] == 0
    _assert_only_last_entry(allowed)
    assert "etf" in breakout_new_high_60d.META["asset_types"]


def test_long_lower_shadow_fixed_sample_and_recovery_switch() -> None:
    open_ = np.full(80, 10.0)
    high = np.full(80, 10.1)
    low = np.full(80, 9.9)
    close = np.full(80, 10.0)
    open_[-1], high[-1], low[-1], close[-1] = 9.8, 10.0, 9.4, 9.6
    close_position = np.zeros(80)
    momentum = np.zeros(80)
    momentum[-1] = -0.06
    market = _market(
        open_=open_,
        high=high,
        low=low,
        close=close,
        fields={
            "prev_close": np.full(80, 10.0),
            "momentum_5d": momentum,
            "vol_ratio_5d": np.full(80, 1.2),
            "close_position": close_position,
            "ma5": np.full(80, 9.0),
        },
    )

    blocked = long_lower_shadow_reversal.MATRIX_STRATEGY.compute_signals(
        market, {"shadow_pct_min": 1.0, "require_recovery": True}
    )
    allowed = long_lower_shadow_reversal.MATRIX_STRATEGY.compute_signals(
        market, {"shadow_pct_min": 1.0, "require_recovery": False}
    )

    assert blocked.entry[-1, 0] == 0
    _assert_only_last_entry(allowed)


def test_ma_convergence_breakout_fixed_sample_and_squeeze_boundary() -> None:
    close = np.full(80, 10.0)
    close[-1] = 10.3
    change = np.zeros(80)
    change[-1] = 0.02
    volume_ratio = np.ones(80)
    volume_ratio[-1] = 1.2
    market = _market(
        close=close,
        fields={
            "ma5": np.full(80, 10.0),
            "ma10": np.full(80, 10.0),
            "ma20": np.full(80, 10.0),
            "change_pct": change,
            "vol_ratio_5d": volume_ratio,
        },
    )

    signals = ma_convergence_breakout.MATRIX_STRATEGY.compute_signals(
        market,
        {
            "spread_pct_max": 0.5,
            "squeeze_days": 3,
            "min_change_pct": 2.0,
            "require_volume": True,
        },
    )

    _assert_only_last_entry(signals)
    assert "etf" in ma_convergence_breakout.META["asset_types"]


def test_macd_below_zero_revival_fixed_sample_and_window_boundary() -> None:
    close = np.linspace(12.0, 9.0, 80)
    close[-1] = 8.0
    dif = np.full(80, -0.5)
    dif[-1] = -0.2
    market = _market(close=close, fields={"macd_dif": dif})

    signals = macd_below_zero_revival.MATRIX_STRATEGY.compute_signals(
        market, {"low_window": 10, "revive_days": 5}
    )

    _assert_only_last_entry(signals)
    assert macd_below_zero_revival.META["asset_types"] == ["stock"]


def test_platform_breakout_fixed_sample_and_threshold_boundary() -> None:
    close = np.full(80, 9.8)
    close[-1] = 10.1
    high = np.full(80, 10.0)
    low = np.full(80, 9.5)
    high[-1], low[-1] = 10.2, 9.7
    volume_ratio = np.ones(80)
    volume_ratio[-1] = 1.5
    market = _market(
        high=high,
        low=low,
        close=close,
        fields={"vol_ratio_5d": volume_ratio, "ma20": np.full(80, 9.0)},
    )

    signals = platform_consolidation_breakout.MATRIX_STRATEGY.compute_signals(
        market,
        {"platform_days": 5, "range_pct_max": 8.0, "vol_ratio_min": 1.5},
    )

    _assert_only_last_entry(signals)


def test_rsi_midline_pullback_fixed_sample_and_period_boundary() -> None:
    close = np.full(80, 11.0)
    rsi = np.full(80, 40.0)
    rsi[-2], rsi[-1] = 66.0, 45.0
    market = _market(
        close=close,
        fields={
            "rsi_6": rsi,
            "ma60": np.full(80, 10.0),
            "ma20": np.full(80, 10.0),
        },
    )

    signals = rsi_midline_pullback.MATRIX_STRATEGY.compute_signals(
        market, {"rsi_period": 6, "mid_low": 45.0, "mid_high": 60.0}
    )

    _assert_only_last_entry(signals)
    assert "etf" in rsi_midline_pullback.META["asset_types"]
