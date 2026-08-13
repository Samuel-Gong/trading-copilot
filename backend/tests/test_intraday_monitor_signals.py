from __future__ import annotations

from datetime import datetime

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.kline_sync import fetch_intraday_monitor_batch, intraday_monitor_support
from app.strategy import monitor_rules
from app.strategy.intraday_signals import IntradaySignalEvaluator
from app.strategy.monitor import MonitorRuleEngine
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


def _minute_rows(prices: list[float]) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600000.SH"] * len(prices),
        "datetime": [datetime(2026, 7, 17, 9, 30 + i) for i in range(len(prices))],
        "close": prices,
        "volume": [1.0] * len(prices),
        "amount": [price * 100.0 for price in prices],
    })


def test_intraday_crosses_are_edge_triggered_and_not_replayed():
    evaluator = IntradaySignalEvaluator()
    kwargs = {
        "symbols": {"600000.SH"},
        "prev_close": {"600000.SH": 10.0},
        "asset_type": "stock",
    }

    # 首次只建立基线, 不补发当前已有的穿越。
    assert evaluator.evaluate(_minute_rows([9.0]), now=datetime(2026, 7, 17, 9, 32), **kwargs) == []

    up = evaluator.evaluate(_minute_rows([9.0, 11.0]), now=datetime(2026, 7, 17, 9, 33), **kwargs)
    assert len(up) == 1
    assert up[0]["signal_intraday_avg_cross_up"] is True
    assert up[0]["signal_intraday_zero_cross_up"] is True

    # 同一根已完成分钟线不得重复触发。
    assert evaluator.evaluate(_minute_rows([9.0, 11.0]), now=datetime(2026, 7, 17, 9, 33, 30), **kwargs) == []

    down = evaluator.evaluate(_minute_rows([9.0, 11.0, 9.0]), now=datetime(2026, 7, 17, 9, 34), **kwargs)
    assert len(down) == 1
    assert down[0]["signal_intraday_avg_cross_down"] is True
    assert down[0]["signal_intraday_zero_cross_down"] is True


def test_intraday_signals_flow_through_monitor_engine():
    evaluator = IntradaySignalEvaluator()
    kwargs = {
        "symbols": {"600000.SH"},
        "prev_close": {"600000.SH": 10.0},
        "asset_type": "stock",
    }
    evaluator.evaluate(_minute_rows([9.0]), now=datetime(2026, 7, 17, 9, 32), **kwargs)
    signals = evaluator.evaluate(_minute_rows([9.0, 11.0]), now=datetime(2026, 7, 17, 9, 33), **kwargs)
    enriched = pl.DataFrame({
        "symbol": ["600000.SH"], "close": [11.0], "change_pct": [0.1],
    })
    engine = MonitorRuleEngine()
    engine.set_rules([{**_intraday_rule(), "cooldown_seconds": 0}])
    events = engine.evaluate(evaluator.inject(enriched, signals))
    assert len(events) == 1
    assert events[0]["rule_id"] == "intraday_rule"
    assert events[0]["signals"] == ["signal_intraday_avg_cross_up"]


def test_intraday_signal_state_resets_between_trading_days():
    evaluator = IntradaySignalEvaluator()
    evaluator.evaluate(
        _minute_rows([9.0]), symbols={"600000.SH"},
        prev_close={"600000.SH": 10.0}, asset_type="stock",
        now=datetime(2026, 7, 17, 9, 32),
    )
    next_day = pl.DataFrame({
        "symbol": ["600000.SH"],
        "datetime": [datetime(2026, 7, 18, 9, 30)],
        "close": [11.0], "volume": [1.0], "amount": [1100.0],
    })
    assert evaluator.evaluate(
        next_day, symbols={"600000.SH"},
        prev_close={"600000.SH": 10.0}, asset_type="stock",
        now=datetime(2026, 7, 18, 9, 32),
    ) == []


def test_intraday_average_does_not_accumulate_previous_day_bars():
    evaluator = IntradaySignalEvaluator()
    previous_day = pl.DataFrame({
        "symbol": ["600000.SH"],
        "datetime": [datetime(2026, 7, 16, 15, 0)],
        "close": [100.0], "volume": [1000.0], "amount": [10_000_000.0],
    })
    first = pl.concat([previous_day, _minute_rows([9.0])])
    evaluator.evaluate(
        first, symbols={"600000.SH"}, prev_close={"600000.SH": 10.0},
        asset_type="stock", now=datetime(2026, 7, 17, 9, 32),
    )
    second = pl.concat([previous_day, _minute_rows([9.0, 11.0])])
    signals = evaluator.evaluate(
        second, symbols={"600000.SH"}, prev_close={"600000.SH": 10.0},
        asset_type="stock", now=datetime(2026, 7, 17, 9, 33),
    )
    assert signals[0]["signal_intraday_avg_cross_up"] is True


def test_intraday_cutoff_keeps_beijing_time_in_utc_runtime():
    evaluator = IntradaySignalEvaluator()
    kwargs = {
        "symbols": {"600000.SH"},
        "prev_close": {"600000.SH": 10.0},
        "asset_type": "stock",
    }
    assert evaluator.evaluate(
        _minute_rows([9.0]), symbols={"600000.SH"},
        prev_close={"600000.SH": 10.0}, asset_type="stock",
        now=datetime(2026, 7, 17, 9, 32, tzinfo=CN_TZ),
    ) == []
    signals = evaluator.evaluate(
        _minute_rows([9.0, 11.0]), now=datetime(2026, 7, 17, 9, 33, tzinfo=CN_TZ),
        **kwargs,
    )
    assert signals[0]["signal_intraday_zero_cross_up"] is True


def _intraday_rule(scope: str = "symbols") -> dict:
    return {
        "id": "intraday_rule", "name": "分时监控", "enabled": True,
        "type": "signal", "asset_type": "stock", "scope": scope,
        "symbols": ["600000.SH"], "logic": "and",
        "conditions": [{"field": "signal_intraday_avg_cross_up", "op": "truth"}],
    }


def test_intraday_rule_pool_is_derived_from_enabled_rules():
    engine = MonitorRuleEngine()
    disabled = {**_intraday_rule(), "id": "disabled", "enabled": False, "symbols": ["000001.SZ"]}
    engine.set_rules([_intraday_rule(), disabled])
    assert engine.intraday_signal_symbols("stock") == {"600000.SH"}
    assert engine.intraday_signal_symbols("etf") == set()


def test_intraday_rule_rejects_non_symbol_scope():
    with pytest.raises(ValueError, match="仅支持指定标的"):
        monitor_rules.validate(_intraday_rule("all"))


def test_intraday_support_uses_capability_limits(monkeypatch):
    monkeypatch.setattr("app.services.preferences.get_minute_data_provider", lambda: "tickflow")
    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(batch=25, rpm=30)})
    support = intraday_monitor_support(capset)
    assert support["available"] is True
    assert support["source"] == "minute_batch"
    assert support["max_symbols"] == 25

    denied = intraday_monitor_support(CapabilitySet())
    assert denied["available"] is False


def test_intraday_batch_provider_is_normalized_without_network(monkeypatch):
    monkeypatch.setattr("app.services.preferences.get_minute_data_provider", lambda: "tickflow")

    class FakeKlines:
        def intraday_batch(self, symbols, count, as_dataframe, show_progress, batch_size):
            assert symbols == ["600000.SH"]
            assert count == 300
            assert as_dataframe is True
            assert show_progress is False
            assert batch_size == 20
            return pl.DataFrame({
                "symbol": symbols,
                "datetime": [datetime(2026, 7, 17, 9, 30)],
                "open": [10.0], "high": [10.1], "low": [9.9], "close": [10.0],
                "volume": [1.0], "amount": [1000.0],
            })

    class FakeClient:
        klines = FakeKlines()

    monkeypatch.setattr("app.services.kline_sync.get_client", lambda: FakeClient())
    capset = CapabilitySet({Cap.INTRADAY_BATCH: CapabilityLimits(batch=20, rpm=30)})
    result = fetch_intraday_monitor_batch(
        ["600000.SH"], capset, now=datetime(2026, 7, 17, 10, 0, tzinfo=CN_TZ),
    )
    assert result.columns == ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]
    assert result["symbol"].to_list() == ["600000.SH"]


# ── 自定义价位条件信号 ────────────────────────────────────

def _price_cross_kwargs(levels: dict[str, list[float]] | None = None) -> dict:
    kw = {
        "symbols": {"600000.SH"},
        "prev_close": {"600000.SH": 10.0},
        "asset_type": "stock",
    }
    if levels is not None:
        kw["price_levels"] = levels
    return kw


def test_price_above_fires_when_price_exceeds_level():
    evaluator = IntradaySignalEvaluator()
    levels = {"600000.SH": [10.5]}
    kw = _price_cross_kwargs(levels)

    signals = evaluator.evaluate(_minute_rows([11.0]), now=datetime(2026, 7, 17, 9, 31), **kw)
    assert len(signals) == 1
    assert signals[0]["signal_intraday_price_above"] is True
    assert signals[0]["signal_intraday_price_below"] is False


def test_price_above_does_not_fire_when_price_below_level():
    evaluator = IntradaySignalEvaluator()
    levels = {"600000.SH": [10.5]}
    kw = _price_cross_kwargs(levels)

    signals = evaluator.evaluate(_minute_rows([9.0]), now=datetime(2026, 7, 17, 9, 31), **kw)
    assert len(signals) == 1
    assert signals[0]["signal_intraday_price_above"] is False


def test_price_below_fires_when_price_under_level():
    evaluator = IntradaySignalEvaluator()
    levels = {"600000.SH": [9.5]}
    kw = _price_cross_kwargs(levels)

    signals = evaluator.evaluate(_minute_rows([9.0]), now=datetime(2026, 7, 17, 9, 31), **kw)
    assert len(signals) == 1
    assert signals[0]["signal_intraday_price_below"] is True
    assert signals[0]["signal_intraday_price_above"] is False


def test_price_below_does_not_fire_when_price_above_level():
    evaluator = IntradaySignalEvaluator()
    levels = {"600000.SH": [9.5]}
    kw = _price_cross_kwargs(levels)

    signals = evaluator.evaluate(_minute_rows([10.0]), now=datetime(2026, 7, 17, 9, 31), **kw)
    assert len(signals) == 1
    assert signals[0]["signal_intraday_price_below"] is False


def test_price_condition_fires_on_first_bar():
    evaluator = IntradaySignalEvaluator()
    levels = {"600000.SH": [10.5]}
    kw = _price_cross_kwargs(levels)

    signals = evaluator.evaluate(_minute_rows([11.0]), now=datetime(2026, 7, 17, 9, 31), **kw)
    assert len(signals) == 1
    assert signals[0]["signal_intraday_price_above"] is True


def test_price_condition_not_triggered_without_levels():
    evaluator = IntradaySignalEvaluator()
    kw = _price_cross_kwargs(None)

    signals = evaluator.evaluate(_minute_rows([11.0]), now=datetime(2026, 7, 17, 9, 31), **kw)
    assert all(s["signal_intraday_price_above"] is False for s in signals)
    assert all(s["signal_intraday_price_below"] is False for s in signals)


def test_price_condition_multiple_levels_any_satisfied():
    evaluator = IntradaySignalEvaluator()
    levels = {"600000.SH": [10.5, 12.0]}
    kw = _price_cross_kwargs(levels)

    signals = evaluator.evaluate(_minute_rows([11.0]), now=datetime(2026, 7, 17, 9, 31), **kw)
    assert len(signals) == 1
    assert signals[0]["signal_intraday_price_above"] is True


def test_price_condition_not_replayed_same_bar():
    evaluator = IntradaySignalEvaluator()
    levels = {"600000.SH": [10.5]}
    kw = _price_cross_kwargs(levels)

    evaluator.evaluate(_minute_rows([11.0]), now=datetime(2026, 7, 17, 9, 31), **kw)
    assert evaluator.evaluate(_minute_rows([11.0]), now=datetime(2026, 7, 17, 9, 31, 30), **kw) == []


def test_price_condition_fires_again_on_new_bar():
    evaluator = IntradaySignalEvaluator()
    levels = {"600000.SH": [10.5]}
    kw = _price_cross_kwargs(levels)

    first = evaluator.evaluate(_minute_rows([11.0]), now=datetime(2026, 7, 17, 9, 31), **kw)
    assert len(first) == 1 and first[0]["signal_intraday_price_above"] is True

    second = evaluator.evaluate(_minute_rows([11.0, 12.0]), now=datetime(2026, 7, 17, 9, 32), **kw)
    assert len(second) == 1 and second[0]["signal_intraday_price_above"] is True


def _price_cross_rule(scope: str = "symbols", levels: dict | None = None) -> dict:
    rule = {
        "id": "price_cross_rule", "name": "价位条件", "enabled": True,
        "type": "signal", "asset_type": "stock", "scope": scope,
        "symbols": ["600000.SH"], "logic": "and",
        "conditions": [{"field": "signal_intraday_price_above", "op": "truth"}],
    }
    if levels is not None:
        rule["intraday_price_levels"] = levels
    return rule


def test_price_cross_rule_validates_with_levels():
    monitor_rules.validate(_price_cross_rule(levels={"600000.SH": 10.5}))


def test_price_cross_rule_rejects_missing_levels():
    with pytest.raises(ValueError, match="intraday_price_levels"):
        monitor_rules.validate(_price_cross_rule(levels={}))


def test_price_cross_rule_rejects_levels_without_levels_field():
    rule = _price_cross_rule()
    rule.pop("intraday_price_levels", None)
    with pytest.raises(ValueError, match="intraday_price_levels"):
        monitor_rules.validate(rule)


def test_price_cross_rule_rejects_levels_for_symbol_not_in_symbols():
    with pytest.raises(ValueError, match="未在 symbols"):
        monitor_rules.validate(_price_cross_rule(levels={"000001.SZ": 10.0}))


def test_price_cross_rule_rejects_non_positive_price():
    with pytest.raises(ValueError, match="正数"):
        monitor_rules.validate(_price_cross_rule(levels={"600000.SH": 0}))


def test_intraday_price_levels_collected_from_rules():
    engine = MonitorRuleEngine()
    rule_a = {
        "id": "rule_a", "name": "A", "enabled": True, "type": "signal",
        "asset_type": "stock", "scope": "symbols", "symbols": ["600000.SH"],
        "logic": "and",
        "conditions": [{"field": "signal_intraday_price_above", "op": "truth"}],
        "intraday_price_levels": {"600000.SH": 10.5},
    }
    rule_b = {
        "id": "rule_b", "name": "B", "enabled": True, "type": "signal",
        "asset_type": "stock", "scope": "symbols", "symbols": ["600000.SH", "000001.SZ"],
        "logic": "and",
        "conditions": [{"field": "signal_intraday_price_below", "op": "truth"}],
        "intraday_price_levels": {"600000.SH": 12.0, "000001.SZ": 15.0},
    }
    engine.set_rules([rule_a, rule_b])
    levels = engine.intraday_price_levels("stock")
    assert levels["600000.SH"] == [10.5, 12.0]
    assert levels["000001.SZ"] == [15.0]


def test_intraday_price_levels_excludes_disabled_rules():
    engine = MonitorRuleEngine()
    rule = {
        "id": "rule_a", "name": "A", "enabled": False, "type": "signal",
        "asset_type": "stock", "scope": "symbols", "symbols": ["600000.SH"],
        "logic": "and",
        "conditions": [{"field": "signal_intraday_price_above", "op": "truth"}],
        "intraday_price_levels": {"600000.SH": 10.5},
    }
    engine.set_rules([rule])
    assert engine.intraday_price_levels("stock") == {}
