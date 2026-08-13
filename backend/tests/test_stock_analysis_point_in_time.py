"""个股分析输入的 point-in-time 财务边界。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.services import stock_analyzer


class FakeRepo:
    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        rows = []
        for offset in range(30):
            trade_date = end - timedelta(days=29 - offset)
            rows.append(
                {
                    "symbol": symbol,
                    "date": trade_date,
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.0,
                    "volume": 1000,
                }
            )
        return pl.DataFrame(rows)


def test_historical_analysis_uses_announcement_date_and_excludes_future_financials(
    tmp_path, monkeypatch
):
    tables = {
        "metrics": pl.DataFrame(
            {
                "symbol": ["600519.SH", "600519.SH"],
                "period_end": ["2026-03-31", "2026-06-30"],
                "announce_date": ["2026-04-25", "2026-08-15"],
                "fact": ["PAST_FINANCIAL", "FUTURE_FINANCIAL_SENTINEL"],
            }
        ),
        "income": pl.DataFrame(
            {
                "symbol": ["600519.SH"],
                "period_end": ["2026-03-31"],
                "announce_date": ["2026-04-25"],
                "fact": ["PAST_INCOME"],
            }
        ),
    }
    monkeypatch.setattr(
        stock_analyzer,
        "get_financial_df",
        lambda data_dir, table: tables[table],
    )

    result = stock_analyzer.build_stock_analysis_input(
        FakeRepo(),
        tmp_path,
        "600519.SH",
        as_of=date(2026, 7, 31),
    )

    assert "PAST_FINANCIAL" in result.user_prompt
    assert "PAST_INCOME" in result.user_prompt
    assert "FUTURE_FINANCIAL_SENTINEL" not in result.user_prompt


def test_historical_analysis_omits_financial_table_without_announcement_time(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        stock_analyzer,
        "get_financial_df",
        lambda data_dir, table: pl.DataFrame(
            {
                "symbol": ["600519.SH"],
                "period_end": ["2026-03-31"],
                "fact": ["UNPROVEN_TIME_SENTINEL"],
            }
        ),
    )

    result = stock_analyzer.build_stock_analysis_input(
        FakeRepo(),
        tmp_path,
        "600519.SH",
        as_of=date(2026, 7, 31),
    )

    assert "UNPROVEN_TIME_SENTINEL" not in result.user_prompt
