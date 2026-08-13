"""本地交易日公开 HTTP 契约测试。"""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.data import router


def make_client(tmp_path) -> TestClient:
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(router)
    return TestClient(app)


def test_trading_dates_only_returns_sorted_enriched_partitions(tmp_path):
    enriched = tmp_path / "kline_daily_enriched"
    enriched.mkdir()
    for name in ("date=2026-07-31", "date=2026-07-29", "date=2026-07-30"):
        (enriched / name).mkdir()
    (enriched / "date=not-a-date").mkdir()
    (enriched / "date=2026-08-01").write_text("不是分区目录", encoding="utf-8")

    response = make_client(tmp_path).get("/api/data/trading-dates")

    assert response.status_code == 200
    assert response.json() == {
        "dates": ["2026-07-29", "2026-07-30", "2026-07-31"],
        "earliest_date": "2026-07-29",
        "latest_date": "2026-07-31",
    }
