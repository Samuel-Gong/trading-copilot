"""daily-batch 混合资产分组测试。"""
import datetime as _dt

import polars as pl
import pytest

from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture()
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def test_daily_batch_groups_index_symbols(repo, monkeypatch):
    from app.api import kline as kline_api

    calls = {"stock_batch": [], "index": []}

    def fake_stock_batch(symbols, start, end, columns=None):
        calls["stock_batch"].append(list(symbols))
        return pl.DataFrame()

    def fake_index_daily(symbol, start, end, columns=None):
        calls["index"].append(symbol)
        return pl.DataFrame({
            "symbol": [symbol], "date": [_dt.date(2026, 7, 24)],
            "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1],
        })

    monkeypatch.setattr(repo, "get_daily_batch", fake_stock_batch)
    monkeypatch.setattr(repo, "get_index_daily", fake_index_daily)
    monkeypatch.setattr(repo, "get_index_symbol_set", lambda: {"000001.SH"})
    monkeypatch.setattr(repo, "get_etf_symbol_set", lambda: set())

    state = type("S", (), {"repo": repo})()
    req = type("R", (), {"app": type("A", (), {"state": state})()})()

    out = kline_api.get_daily_batch(req, {"symbols": ["600000.SH", "000001.SH"], "days": 12})
    assert calls["stock_batch"] == [["600000.SH"]]
    assert calls["index"] == ["000001.SH"]
    assert "000001.SH" in out["data"]


def test_daily_close_batch_reads_stock_and_etf_without_indicator_compute(
    repo, tmp_path, monkeypatch
):
    pl.DataFrame(
        {
            "symbol": ["600519.SH", "000001.SZ"],
            "date": [_dt.date(2026, 7, 31), _dt.date(2026, 7, 30)],
            "open": [1590.0, 10.4],
            "high": [1610.0, 10.6],
            "close": [1600.0, 10.5],
            "volume": [100, 200],
        }
    ).write_parquet(tmp_path / "kline_daily_enriched" / "part.parquet")
    pl.DataFrame(
        {
            "symbol": ["510300.SH"],
            "date": [_dt.date(2026, 7, 31)],
            "close": [4.2],
            "volume": [300],
        }
    ).write_parquet(tmp_path / "kline_etf_enriched" / "part.parquet")
    # 兼容 ETF 独立目录落地前保存在指数 enriched 中的历史数据。
    pl.DataFrame(
        {
            "symbol": ["159915.SZ"],
            "date": [_dt.date(2026, 7, 30)],
            "close": [2.1],
            "volume": [400],
        }
    ).write_parquet(tmp_path / "kline_index_enriched" / "part.parquet")

    def fail_indicator_compute(*args, **kwargs):
        raise AssertionError("批量估值价格读取不应计算指标")

    monkeypatch.setattr(repo, "_compute_enriched_range", fail_indicator_compute)
    monkeypatch.setattr(repo, "_compute_index_enriched_range", fail_indicator_compute)

    stock = repo.get_daily_close_batch(
        "stock",
        ["600519.SH", "000001.SZ"],
        _dt.date(2026, 7, 1),
        _dt.date(2026, 8, 1),
    )
    etf = repo.get_daily_close_batch(
        "etf",
        ["510300.SH", "159915.SZ"],
        _dt.date(2026, 7, 1),
        _dt.date(2026, 8, 1),
    )
    legacy_only = repo.get_daily_close_batch(
        "etf",
        ["159915.SZ"],
        _dt.date(2026, 7, 1),
        _dt.date(2026, 8, 1),
    )

    assert stock.columns == ["symbol", "date", "close"]
    assert stock.sort("symbol").to_dicts() == [
        {"symbol": "000001.SZ", "date": _dt.date(2026, 7, 30), "close": 10.5},
        {"symbol": "600519.SH", "date": _dt.date(2026, 7, 31), "close": 1600.0},
    ]
    assert etf.sort("symbol").to_dicts() == [
        {"symbol": "159915.SZ", "date": _dt.date(2026, 7, 30), "close": 2.1},
        {"symbol": "510300.SH", "date": _dt.date(2026, 7, 31), "close": 4.2},
    ]
    assert legacy_only.to_dicts() == [
        {"symbol": "159915.SZ", "date": _dt.date(2026, 7, 30), "close": 2.1},
    ]


def test_daily_close_batch_filters_halted_stock_rows(repo, tmp_path):
    pl.DataFrame(
        {
            "symbol": ["600519.SH", "600519.SH"],
            "date": [_dt.date(2026, 7, 30), _dt.date(2026, 7, 31)],
            "open": [10.0, 0.0],
            "high": [11.0, 0.0],
            "close": [10.5, 10.5],
        }
    ).write_parquet(tmp_path / "kline_daily_enriched" / "part.parquet")
    repo._enriched_cache = pl.DataFrame(
        {
            "symbol": ["600519.SH"],
            "date": [_dt.date(2026, 7, 31)],
            "open": [0.0],
            "high": [0.0],
            "close": [10.5],
        }
    )
    repo._enriched_cache_date = _dt.date(2026, 7, 31)

    frame = repo.get_daily_close_batch(
        "stock",
        ["600519.SH"],
        _dt.date(2026, 7, 1),
        _dt.date(2026, 7, 31),
    )

    assert frame.to_dicts() == [
        {"symbol": "600519.SH", "date": _dt.date(2026, 7, 30), "close": 10.5},
    ]


def test_daily_close_batch_halted_cache_masks_same_date_disk_row(repo, tmp_path):
    pl.DataFrame(
        {
            "symbol": ["600519.SH", "600519.SH"],
            "date": [_dt.date(2026, 7, 30), _dt.date(2026, 7, 31)],
            "open": [10.0, 10.4],
            "high": [11.0, 10.6],
            "close": [10.5, 10.5],
        }
    ).write_parquet(tmp_path / "kline_daily_enriched" / "part.parquet")
    repo._enriched_cache = pl.DataFrame(
        {
            "symbol": ["600519.SH"],
            "date": [_dt.date(2026, 7, 31)],
            "open": [0.0],
            "high": [0.0],
            "close": [10.5],
        }
    )
    repo._enriched_cache_date = _dt.date(2026, 7, 31)

    frame = repo.get_daily_close_batch(
        "stock",
        ["600519.SH"],
        _dt.date(2026, 7, 1),
        _dt.date(2026, 7, 31),
    )

    assert frame.to_dicts() == [
        {"symbol": "600519.SH", "date": _dt.date(2026, 7, 30), "close": 10.5},
    ]


def test_daily_close_batch_overlays_only_symbols_present_in_latest_stock_cache(
    repo, tmp_path
):
    cache_date = _dt.date(2026, 7, 31)
    pl.DataFrame(
        {
            "symbol": ["600519.SH", "000001.SZ"],
            "date": [cache_date, cache_date],
            "open": [1580.0, 10.4],
            "high": [1600.0, 10.6],
            "close": [1590.0, 10.5],
        }
    ).write_parquet(tmp_path / "kline_daily_enriched" / "part.parquet")
    repo._enriched_cache = pl.DataFrame(
        {
            "symbol": ["600519.SH"],
            "date": [cache_date],
            "open": [1590.0],
            "high": [1610.0],
            "close": [1600.0],
        }
    )
    repo._enriched_cache_date = cache_date

    frame = repo.get_daily_close_batch(
        "stock",
        ["600519.SH", "000001.SZ"],
        _dt.date(2026, 7, 1),
        _dt.date(2026, 8, 1),
    )

    assert frame.sort("symbol").to_dicts() == [
        {"symbol": "000001.SZ", "date": cache_date, "close": 10.5},
        {"symbol": "600519.SH", "date": cache_date, "close": 1600.0},
    ]


def test_daily_close_batch_uses_date_from_same_stock_cache_snapshot(repo, tmp_path):
    requested_date = _dt.date(2026, 7, 31)
    future_cache_date = _dt.date(2026, 8, 1)
    pl.DataFrame(
        {
            "symbol": ["600519.SH"],
            "date": [requested_date],
            "open": [1580.0],
            "high": [1600.0],
            "close": [1590.0],
        }
    ).write_parquet(tmp_path / "kline_daily_enriched" / "part.parquet")
    repo._enriched_cache = pl.DataFrame(
        {
            "symbol": ["600519.SH"],
            "date": [future_cache_date],
            "open": [1590.0],
            "high": [1610.0],
            "close": [1600.0],
        }
    )
    # 模拟跨日刷新过程中 DataFrame 已替换而配套日期仍是旧值的瞬间。
    repo._enriched_cache_date = requested_date

    frame = repo.get_daily_close_batch(
        "stock",
        ["600519.SH"],
        _dt.date(2026, 7, 1),
        requested_date,
    )

    assert frame.to_dicts() == [
        {"symbol": "600519.SH", "date": requested_date, "close": 1590.0},
    ]
