"""市场环境 API 的时间窗口契约回归测试。"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl

from app.api import regime
from app.services import market_mainline, preferences


def _request(tmp_path):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
            ),
        ),
    )


def test_regime_phases_limit_uses_latest_trading_days(tmp_path, monkeypatch):
    days = [date(2026, 8, 20), date(2026, 8, 21), date(2026, 8, 24)]
    history = pl.DataFrame({
        "date": days,
        "phase": ["ice", "rally", "rally"],
        "max_consecutive": [1, 3, 4],
        "first_board": [2, 8, 10],
        "ge2_count": [0, 2, 3],
        "promo_rate": [None, 0.2, 0.3],
        "seal_rate": [0.4, 0.6, 0.7],
    })
    monkeypatch.setattr(regime.regime_builder, "load_regime_history", lambda data_dir: history)
    monkeypatch.setattr(market_mainline, "load_mainline_history", lambda data_dir, kind: pl.DataFrame())

    result = regime.regime_phases(_request(tmp_path), limit=2)

    assert result["segments"] == [{
        "phase": "rally",
        "label": "主升",
        "start": "2026-08-21",
        "end": "2026-08-24",
        "days": 2,
        "avg_height": 3.5,
        "avg_first_board": 9.0,
        "avg_ge2": 2.5,
        "avg_promo": 0.25,
        "avg_seal_rate": 0.65,
        "top_mainlines": [],
    }]


def test_regime_mainline_limit_uses_latest_distinct_trading_days(tmp_path, monkeypatch):
    history = pl.DataFrame({
        "date": [
            date(2026, 8, 20), date(2026, 8, 20),
            date(2026, 8, 21), date(2026, 8, 21),
            date(2026, 8, 24), date(2026, 8, 24),
        ],
        "kind": ["concept"] * 6,
        "member": ["旧主线", "次线", "新主线", "次线", "新主线", "次线"],
        "score": [99.0, 60.0, 80.0, 50.0, 90.0, 40.0],
        "limit_up_count": [5, 3, 5, 3, 6, 3],
        "ge2_count": [2, 1, 2, 1, 3, 1],
        "max_boards": [3, 2, 3, 2, 4, 2],
        "rungs_filled": [2, 1, 2, 1, 3, 1],
        "leader_symbol": ["A.SH", "B.SH", "C.SH", "B.SH", "C.SH", "B.SH"],
        "rank": [1, 2, 1, 2, 1, 2],
    })
    monkeypatch.setattr(market_mainline, "load_mainline_history", lambda data_dir, kind: history)
    monkeypatch.setattr(
        preferences,
        "get_mainline_filter_config",
        lambda: {"min_members": 4, "max_members": 600, "blacklist": []},
    )

    result = regime.regime_mainline(_request(tmp_path), limit=2)

    assert {row["date"] for row in result["rows"]} == {"2026-08-21", "2026-08-24"}
    assert result["leaders"][0]["member"] == "新主线"
    assert all(row["member"] != "旧主线" for row in result["rows"])
