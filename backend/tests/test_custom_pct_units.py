"""自定义源实时行情比例字段单位归一测试 (CONTRIBUTING §3.1)。

契约: change_pct/amplitude/turnover_rate 为小数制 (0.0366 = 3.66%)。
单位只认显式声明 pct_unit: percent|decimal, 不靠数值猜:
  - 声明 percent → 无条件 /100; 声明 decimal → 无条件透传;
  - 未声明 → 所有比例列置 None, 配置校验失败(fail-closed)。
"""

from __future__ import annotations

import polars as pl
import pytest

from app.data_providers.custom.config import CustomSourceConfig, DatasetConfig, config_from_dict
from app.data_providers.custom.provider import (
    GenericHTTPProvider,
    _normalize_pct_units,
    _normalize_volume_units,
)


def _df(pcts, amps=None, turnovers=None):
    data = {"change_pct": pcts}
    if amps is not None:
        data["amplitude"] = amps
    if turnovers is not None:
        data["turnover_rate"] = turnovers
    return pl.DataFrame(data)


# ---- 显式声明: percent ----


def test_declared_percent_divides_all_columns():
    out = _normalize_pct_units(
        _df(
            [1.5, -2.2, 0.9, 2.8, -1.1, 0.6, 3.3, -0.8],
            amps=[2.0, 3.5, 1.8, 4.0, 2.5, 1.2, 5.0, 1.6],
            turnovers=[0.5, 1.2, 0.8, 2.0, 0.9, 0.4, 1.5, 0.7],
        ),
        pct_unit="percent",
    )
    assert out["change_pct"][0] == pytest.approx(0.015)
    assert out["amplitude"][0] == pytest.approx(0.02)
    assert out["turnover_rate"][0] == pytest.approx(0.005)


def test_declared_percent_wins_even_when_values_look_decimal():
    # 百分制低波动日: 0.25 表示 0.25%, 数值落在小数制区间内——声明优先, 不靠猜
    out = _normalize_pct_units(
        _df(
            [0.25, 0.30, 0.28, 0.27, 0.26, 0.22],
            amps=[0.4, 0.5, 0.45, 0.6, 0.5, 0.4],
            turnovers=[0.05, 0.08, 0.06, 0.1, 0.07, 0.05],
        ),
        pct_unit="percent",
    )
    assert out["change_pct"][0] == pytest.approx(0.0025)
    assert out["amplitude"][0] == pytest.approx(0.004)
    assert out["turnover_rate"][0] == pytest.approx(0.0005)


# ---- 显式声明: decimal ----


def test_declared_decimal_passes_through():
    pcts = [0.015, -0.022, 0.009, 0.028, -0.011, 0.006, 0.033, -0.008]
    out = _normalize_pct_units(
        _df(pcts, amps=[0.02, 0.035, 0.018, 0.04, 0.025, 0.012, 0.05, 0.016]), pct_unit="decimal"
    )
    assert out["change_pct"].to_list() == pcts
    assert out["amplitude"][0] == pytest.approx(0.02)


def test_declared_decimal_wins_even_when_values_look_percent():
    # 用户声明了小数制就按小数制契约透传, 不替用户"修正"数据
    out = _normalize_pct_units(_df([3.66, -2.15, 0.9, 2.8, 1.1]), pct_unit="decimal")
    assert out["change_pct"][0] == pytest.approx(3.66)


# ---- 未声明: 所有比例列 fail-closed ----


def test_undeclared_low_volatility_percent_values_are_nulled():
    """0.10 表示 0.10% 时与 10% 小数制不可区分, 禁止启发式透传。"""
    out = _normalize_pct_units(
        _df(
            [0.10, 0.15, 0.20, -0.12, 0.08, 0.18],
            amps=[0.20, 0.25, 0.30, 0.22, 0.18, 0.28],
            turnovers=[0.05, 0.08, 0.06, 0.10, 0.07, 0.05],
        )
    )
    assert out["change_pct"].null_count() == 6
    assert out["amplitude"].null_count() == 6
    assert out["turnover_rate"].null_count() == 6


def test_undeclared_values_are_nulled_even_after_transform():
    """transforms 不替代跨字段统一的 pct_unit 契约。"""
    out = _normalize_pct_units(
        _df([1.5, -2.2, 0.9, 2.8, 3.3, 0.6], turnovers=[0.005, 0.012, 0.008, 0.02, 0.015, 0.007]),
    )
    assert out["change_pct"].null_count() == 6
    assert out["turnover_rate"].null_count() == 6


def test_missing_or_null_columns_noop():
    out = _normalize_pct_units(pl.DataFrame({"close": [1.0, 2.0]}))
    assert out.columns == ["close"]
    out2 = _normalize_pct_units(_df([None, None, None, None, None, None]))
    assert out2["change_pct"].null_count() == 6
    # 全 null 的不可判定列保持 null
    out3 = _normalize_pct_units(_df([1.5, -2.2, 0.9, 2.8, 3.3, 0.6], turnovers=[None] * 6))
    assert out3["turnover_rate"].null_count() == 6


# ---- provider 集成 ----


def _realtime_provider(rows, **ds_kwargs):
    ds_kwargs.setdefault("volume_unit", "lots")
    provider = GenericHTTPProvider(
        CustomSourceConfig(
            name="pct_source",
            display_name="Pct Source",
            datasets={
                "realtime": DatasetConfig(
                    url="https://example.test/realtime",
                    field_map={
                        "code": "symbol",
                        "ts": "timestamp",
                        "price": "last_price",
                        "pre_close": "prev_close",
                        "pct": "change_pct",
                        "amp": "amplitude",
                        "turnover": "turnover_rate",
                    },
                    **ds_kwargs,
                )
            },
        )
    )
    provider._request_rows = lambda cfg, **kwargs: rows
    return provider


_ROWS = [
    {"code": "S1", "ts": 1787542612000, "price": 10.0, "pre_close": 9.85, "pct": 1.52, "amp": 2.4, "turnover": 1.1},
    {"code": "S2", "ts": 1787542612000, "price": 20.0, "pre_close": 20.44, "pct": -2.15, "amp": 3.1, "turnover": 0.8},
    {"code": "S3", "ts": 1787542612000, "price": 30.0, "pre_close": 29.8, "pct": 0.67, "amp": 1.9, "turnover": 0.5},
    {"code": "S4", "ts": 1787542612000, "price": 40.0, "pre_close": 38.9, "pct": 2.83, "amp": 4.2, "turnover": 2.0},
    {"code": "S5", "ts": 1787542612000, "price": 50.0, "pre_close": 50.55, "pct": -1.09, "amp": 2.0, "turnover": 0.9},
    {"code": "S6", "ts": 1787542612000, "price": 60.0, "pre_close": 59.64, "pct": 0.60, "amp": 1.6, "turnover": 0.7},
]


def test_get_realtime_declared_percent_source():
    provider = _realtime_provider(_ROWS, pct_unit="percent")
    try:
        rows = provider.get_realtime()
    finally:
        provider.close()
    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["S1"]["change_pct"] == pytest.approx(0.0152)
    assert by_sym["S1"]["amplitude"] == pytest.approx(0.024)
    assert by_sym["S1"]["turnover_rate"] == pytest.approx(0.011)
    assert by_sym["S2"]["change_pct"] == pytest.approx(-0.0215)


def test_get_realtime_undeclared_nulls_all_ratio_columns():
    provider = _realtime_provider(_ROWS)
    try:
        rows = provider.get_realtime()
    finally:
        provider.close()
    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["S1"]["change_pct"] is None
    assert by_sym["S1"]["amplitude"] is None
    assert by_sym["S1"]["turnover_rate"] is None


def test_get_realtime_discards_entire_snapshot_when_one_timestamp_is_invalid():
    """权威快照不能静默删掉坏行后发布残缺标的集合。"""
    mixed = [dict(_ROWS[0]), {**_ROWS[1], "ts": "invalid"}]
    provider = _realtime_provider(mixed, pct_unit="percent")
    try:
        rows = provider.get_realtime()
    finally:
        provider.close()

    assert rows == []


def test_get_realtime_transformed_turnover_kept_with_decimal_declaration():
    provider = _realtime_provider(
        _ROWS,
        pct_unit="decimal",
        transforms={"turnover_rate": "value / 100"},
    )
    try:
        rows = provider.get_realtime()
    finally:
        provider.close()
    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["S1"]["turnover_rate"] == pytest.approx(0.011)
    assert by_sym["S1"]["amplitude"] == pytest.approx(2.4)


# ---- 配置解析与校验 ----


def test_config_parses_pct_unit():
    cfg = config_from_dict(
        {
            "name": "s",
            "datasets": {
                "realtime": {
                    "url": "https://example.test",
                    "pct_unit": "Percent",
                }
            },
        }
    )
    assert cfg.datasets["realtime"].pct_unit == "percent"


def test_config_rejects_invalid_pct_unit():
    with pytest.raises(ValueError, match="pct_unit"):
        config_from_dict(
            {
                "name": "s",
                "datasets": {
                    "realtime": {
                        "url": "https://example.test",
                        "pct_unit": "basis_point",
                    }
                },
            }
        )


def test_validate_requires_pct_unit_when_ratio_field_is_mapped():
    provider = _realtime_provider(_ROWS)
    try:
        errors = provider.validate()
    finally:
        provider.close()
    assert any("必须声明 pct_unit" in error for error in errors)


def test_validate_flags_pct_unit_on_non_realtime():
    provider = GenericHTTPProvider(
        CustomSourceConfig(
            name="s",
            display_name="S",
            datasets={
                "daily": DatasetConfig(
                    url="https://example.test",
                    field_map={
                        "c": "symbol",
                        "d": "date",
                        "o": "open",
                        "h": "high",
                        "l": "low",
                        "cl": "close",
                        "v": "volume",
                        "a": "amount",
                    },
                    pct_unit="percent",
                    volume_unit="lots",
                )
            },
        )
    )
    try:
        errors = provider.validate()
    finally:
        provider.close()
    assert any("pct_unit" in e and "realtime" in e for e in errors)


def test_validate_flags_invalid_pct_unit_value():
    provider = GenericHTTPProvider(
        CustomSourceConfig(
            name="s",
            display_name="S",
            datasets={
                "realtime": DatasetConfig(
                    url="https://example.test",
                    field_map={
                        "c": "symbol",
                        "p": "last_price",
                        "pc": "prev_close",
                        "o": "open",
                        "h": "high",
                        "l": "low",
                        "v": "volume",
                    },
                    pct_unit="bp",
                )
            },
        )
    )
    try:
        errors = provider.validate()
    finally:
        provider.close()
    assert any("pct_unit" in e for e in errors)


def _financial_provider(rows, *, pct_unit=None, field_map=None):
    mapping = field_map or {
        "code": "symbol",
        "period": "period_end",
        "announced": "announce_date",
        "return_on_equity": "roe",
        "margin": "gross_margin",
    }
    provider = GenericHTTPProvider(
        CustomSourceConfig(
            name="financial_source",
            display_name="Financial Source",
            datasets={
                "financial": DatasetConfig(
                    url="https://example.test/financial",
                    field_map=mapping,
                    pct_unit=pct_unit,
                )
            },
        )
    )
    provider._request_rows = lambda _cfg, **_kwargs: rows
    return provider


def test_financial_decimal_percentages_are_normalized_to_percent_values():
    provider = _financial_provider(
        [{
            "code": "600000.SH",
            "period": "2026-06-30",
            "announced": "2026-08-20",
            "return_on_equity": 0.2,
            "margin": 0.356,
        }],
        pct_unit="decimal",
    )
    try:
        frame = provider.get_financials("metrics", ["600000.SH"])
    finally:
        provider.close()

    assert frame["roe"].item() == pytest.approx(20.0)
    assert frame["gross_margin"].item() == pytest.approx(35.6)
    assert str(frame["period_end"].item()) == "2026-06-30"
    assert str(frame["announce_date"].item()) == "2026-08-20"


def test_financial_percent_values_remain_percent_values():
    frame = GenericHTTPProvider._normalize_financial(
        pl.DataFrame({
            "symbol": ["600000.SH"],
            "period_end": ["2026-06-30"],
            "announce_date": ["2026-08-20"],
            "roe": [20.0],
        }),
        "metrics",
        "percent",
    )
    assert frame["roe"].item() == pytest.approx(20.0)


def test_financial_missing_announce_date_fails_closed():
    provider = _financial_provider(
        [{"code": "600000.SH", "period": "2026-06-30"}],
        pct_unit="percent",
        field_map={"code": "symbol", "period": "period_end"},
    )
    try:
        assert any("announce_date" in error for error in provider.validate())
        frame = provider.get_financials("metrics", ["600000.SH"])
    finally:
        provider.close()
    assert frame.is_empty()


def test_financial_ratio_mapping_requires_explicit_unit():
    provider = _financial_provider([])
    try:
        errors = provider.validate()
    finally:
        provider.close()
    assert any("financial" in error and "pct_unit" in error for error in errors)


@pytest.mark.parametrize("dataset", ["daily", "realtime", "minute"])
@pytest.mark.parametrize(
    ("unit", "raw", "expected"),
    [("lots", 123.0, 123.0), ("shares", 12_300.0, 123.0)],
)
def test_provider_volume_units_are_normalized_to_lots(
    dataset, unit, raw, expected
):
    frame = _normalize_volume_units(
        pl.DataFrame({"volume": [raw]}), unit, dataset
    )
    assert frame["volume"].item() == pytest.approx(expected)


def test_provider_volume_without_unit_fails_closed():
    assert _normalize_volume_units(
        pl.DataFrame({"volume": [100.0]}), None, "daily"
    ).is_empty()


def test_cumulative_adj_factor_is_converted_to_event_ratios():
    frame = GenericHTTPProvider._cumulative_to_event_ratios(
        pl.DataFrame({
            "symbol": ["600000.SH"] * 3,
            "trade_date": ["2026-01-01", "2026-02-01", "2026-03-01"],
            "ex_factor": [1.0, 1.1, 1.2],
        })
    )
    assert frame["ex_factor"].to_list() == pytest.approx(
        [1.0, 1.1, 1.2 / 1.1]
    )


def test_adj_factor_mapping_requires_declared_semantics():
    provider = GenericHTTPProvider(
        CustomSourceConfig(
            name="adj_source",
            display_name="Adj Source",
            datasets={
                "adj_factor": DatasetConfig(
                    url="https://example.test/adj",
                    field_map={
                        "code": "symbol",
                        "day": "trade_date",
                        "factor": "ex_factor",
                    },
                )
            },
        )
    )
    try:
        errors = provider.validate()
    finally:
        provider.close()
    assert any("adj_factor_kind" in error for error in errors)
