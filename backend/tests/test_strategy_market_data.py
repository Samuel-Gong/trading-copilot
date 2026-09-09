"""策略指数K线访问模块 — 测试。"""
import datetime

import polars as pl
import pytest

from app.strategy import _market_data_runtime as market_data_runtime
from app.strategy import market_data
from app.strategy.ai_generator import AIStrategyGenerator


def test_strategy_import_whitelist_blocks_market_data():
    """向量/历史后端都无法保证跨日期调用的逐行 PIT，因此统一禁止。"""
    with pytest.raises(ValueError, match="白名单"):
        AIStrategyGenerator._validate_safety(
            "from app.strategy.market_data import get_index_daily, get_daily"
        )
    AIStrategyGenerator._validate_safety("self._scoring = {'close': 1.0}")


@pytest.mark.parametrize("backend,entrypoint", [
    ("polars_expr", "def filter(df, params):\n    return pl.lit(True)"),
    ("python_history_legacy", "def filter_history(df, params):\n    return df"),
])
def test_market_data_is_rejected_for_every_strategy_backend(backend, entrypoint):
    code = f'''
import polars as pl
from app.strategy.market_data import get_index_daily
EXECUTION_BACKEND = "{backend}"
META = {{"id": "unsafe", "params": [], "scoring": {{}}}}
{entrypoint}
'''
    result = AIStrategyGenerator().validate_code(code)
    assert result["valid"] is False
    assert "白名单" in result["error"]


def test_whitelist_still_blocks_dangerous():
    with pytest.raises(ValueError):
        AIStrategyGenerator._validate_safety("import os")
    with pytest.raises(ValueError):
        AIStrategyGenerator._validate_safety("from os import path")
    with pytest.raises(ValueError):
        AIStrategyGenerator._validate_safety("getattr(obj, '__globals__')")
    with pytest.raises(ValueError, match="白名单"):
        AIStrategyGenerator._validate_safety(
            "from app.strategy.market_data import _repo"
        )
    with pytest.raises(ValueError, match="白名单"):
        AIStrategyGenerator._validate_safety(
            "import app.strategy.market_data as md\nmd._repo()"
        )
    with pytest.raises(ValueError, match="白名单"):
        AIStrategyGenerator._validate_safety(
            "from app.strategy._market_data_runtime import get_repo"
        )
    with pytest.raises(ValueError, match="白名单"):
        AIStrategyGenerator._validate_safety(
            "from app.strategy.market_data import list_index_symbols"
        )


class _FakeRepo:
    """最小 fake: 只实现 market_data 用到的接口。"""
    def __init__(self, index_df=None):
        self.calls: list[tuple] = []
        self._asset = {"000001.SH": "index", "510300.SH": "etf", "600000.SH": "stock"}
        self._index_df = index_df if index_df is not None else pl.DataFrame(
            {"date": [datetime.date(2026, 1, 2)], "close": [3000.0], "macd_dif": [1.0], "macd_dea": [2.0]}
        )
        self._empty = pl.DataFrame()

    def resolve_asset_type(self, symbol):
        self.calls.append(("resolve", symbol))
        return self._asset.get(symbol, "stock")

    def get_index_daily(self, symbol, start=None, end=None, columns=None):
        self.calls.append(("index", symbol, start, end, columns))
        if symbol != "000001.SH":
            return self._empty
        return self._index_df.filter(
            (pl.col("date") >= start) & (pl.col("date") <= end)
        )

    def get_etf_daily(self, symbol, start=None, end=None, columns=None):
        self.calls.append(("etf", symbol, start, end, columns))
        return self._empty

    def get_daily(self, symbol, start=None, end=None, columns=None):
        self.calls.append(("stock", symbol, start, end, columns))
        return self._empty

    def get_instruments_asset(self, asset_type):
        return pl.DataFrame({"symbol": ["000001.SH"], "name": ["上证指数"]})


@pytest.fixture()
def fake_repo():
    fake = _FakeRepo()
    market_data_runtime.set_repo(fake)
    with market_data_runtime.execution_as_of(datetime.date(2026, 1, 31)):
        yield fake
    market_data_runtime.reset_repo()


def test_get_index_daily_delegates_and_normalizes_dates(fake_repo):
    df = market_data.get_index_daily(
        "000001.SH", start="2026-01-01", end="2026-01-31", columns=["date", "close"]
    )
    assert df.height == 1 and df["close"][0] == 3000.0
    _, sym, s, e, cols = fake_repo.calls[-1]
    assert sym == "000001.SH"
    assert s == datetime.date(2026, 1, 1)
    assert e == datetime.date(2026, 1, 31)
    assert cols == ["date", "close"]


@pytest.mark.parametrize("symbol,expected_kind", [
    ("000001.SH", "index"),
    ("510300.SH", "etf"),
    ("600000.SH", "stock"),
])
def test_get_daily_dispatch_by_asset_type(fake_repo, symbol, expected_kind):
    market_data.get_daily(symbol)
    last = fake_repo.calls[-1]
    assert last[0] == expected_kind
    assert last[1] == symbol


def test_bad_symbol_returns_empty_without_calling_repo(fake_repo):
    assert market_data.get_index_daily("").is_empty()
    assert market_data.get_index_daily(None).is_empty()
    assert market_data.get_etf_daily("").is_empty()
    assert market_data.get_daily(None).is_empty()
    assert fake_repo.calls == []


def test_missing_symbol_returns_empty_no_raise(fake_repo):
    assert market_data.get_index_daily("999999.SH").is_empty()


def test_history_read_is_bounded_by_execution_as_of(fake_repo):
    fake_repo._index_df = pl.DataFrame({
        "date": [datetime.date(2026, 1, 2), datetime.date(2026, 2, 1)],
        "close": [3000.0, 9999.0],
    })

    with market_data_runtime.execution_as_of(datetime.date(2026, 1, 15)):
        frame = market_data.get_index_daily(
            "000001.SH",
            start="2026-01-01",
            end="2026-12-31",
        )

    assert frame["close"].to_list() == [3000.0]
    assert fake_repo.calls[-1][3] == datetime.date(2026, 1, 15)
