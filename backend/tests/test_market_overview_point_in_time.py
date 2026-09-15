from __future__ import annotations

from datetime import date

import polars as pl

from app.services import market_overview_builder as builder
from app.services.index_const import CORE_INDEX_SYMBOLS


class _Repo:
    def execute_all(self, _query, _params):
        return [(CORE_INDEX_SYMBOLS[0], date(2026, 9, 4), 100.0, 90.0)]


class _QuoteService:
    def __init__(self) -> None:
        self.index_calls = 0

    def status(self) -> dict:
        return {"enabled": True, "running": True}

    def get_index_quotes(self, _symbols) -> pl.DataFrame:
        self.index_calls += 1
        return pl.DataFrame({
            "symbol": [CORE_INDEX_SYMBOLS[0]],
            "last_price": [200.0],
            "change_pct": [9.9],
        })


class _Screener:
    def __init__(self, _repo) -> None:
        return None

    def latest_date(self) -> date:
        return date(2026, 9, 4)

    def _load_enriched_for_date(self, _as_of) -> pl.DataFrame:
        return pl.DataFrame({
            "symbol": ["600000.SH"],
            "name": ["浦发银行"],
            "close": [10.0],
            "change_pct": [0.01],
            "amount": [1000.0],
            "turnover_rate": [0.02],
            "volume": [100.0],
        })


def test_latest_overview_keeps_lagging_daily_date_point_in_time(monkeypatch):
    quote_service = _QuoteService()
    dimension_dates: list[date | None] = []
    monkeypatch.setattr(builder, "ScreenerService", _Screener)
    monkeypatch.setattr(builder, "cn_today", lambda: date(2026, 9, 7))

    def dimension_rank(*_args, as_of=None, **_kwargs):
        dimension_dates.append(as_of)
        return {"leading": [], "lagging": []}

    monkeypatch.setattr(builder, "_dimension_rank", dimension_rank)

    result = builder.build_market_overview(_Repo(), quote_service=quote_service)

    assert result["as_of"] == "2026-09-04"
    assert result["indices"][0]["last_price"] == 100.0
    assert quote_service.index_calls == 0
    assert dimension_dates == [date(2026, 9, 4), date(2026, 9, 4)]
