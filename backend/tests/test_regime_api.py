"""市场环境 API 的时间窗口契约回归测试。"""
from __future__ import annotations

import threading
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


def test_recompute_defaults_to_cn_today_and_replaces_complete_ranges(
    tmp_path,
    monkeypatch,
):
    """两个重算入口使用北京时间，并以请求范围完整替换旧结果。"""
    start = date(2099, 1, 1)
    business_today = date(2099, 1, 2)
    regime_ranges: list[tuple[date, date, bool]] = []
    mainline_ranges: list[tuple[str, date, date]] = []

    monkeypatch.setattr(
        regime,
        "cn_today",
        lambda: business_today,
        raising=False,
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "earliest_enriched_date",
        lambda repo: start,
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "run_regime_batch",
        lambda repo, start, end: pl.DataFrame(),
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "replace_regime_history_range",
        lambda data_dir, rows, *, start, end: regime_ranges.append(
            (start, end, rows.is_empty())
        ),
        raising=False,
    )
    monkeypatch.setattr(regime.regime_builder, "refresh_phase_labels", lambda data_dir: 0)
    monkeypatch.setattr(
        market_mainline,
        "compute_mainline_range",
        lambda repo, data_dir, start, end, *, kind: (
            mainline_ranges.append((kind, start, end)) or pl.DataFrame()
        ),
    )
    monkeypatch.setattr(
        market_mainline,
        "replace_mainline_history_range",
        lambda *args, **kwargs: None,
    )

    regime.regime_recompute(_request(tmp_path))
    regime.mainline_recompute(_request(tmp_path))

    assert regime_ranges == [(start, business_today, True)]
    assert mainline_ranges == [
        ("concept", start, business_today),
        ("industry", start, business_today),
        ("concept", start, business_today),
        ("industry", start, business_today),
    ]


def test_manual_mainline_recompute_waits_for_pipeline_regime_update(
    tmp_path,
    monkeypatch,
):
    """后台 regime 计算与手动主线重算必须共享完整计算区间锁。"""
    target = date(2026, 8, 25)
    regime_entered = threading.Event()
    release_regime = threading.Event()
    mainline_entered = threading.Event()
    errors: list[BaseException] = []
    request = _request(tmp_path)

    monkeypatch.setattr(
        regime.regime_builder,
        "enriched_date_set",
        lambda repo: {target},
    )

    def run_regime_batch(repo, start, end):
        regime_entered.set()
        assert release_regime.wait(2)
        return pl.DataFrame()

    monkeypatch.setattr(
        regime.regime_builder,
        "run_regime_batch",
        run_regime_batch,
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "replace_regime_history_range",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "refresh_phase_labels",
        lambda data_dir: 0,
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "earliest_enriched_date",
        lambda repo: target,
    )
    monkeypatch.setattr(
        market_mainline,
        "compute_mainline_range",
        lambda *args, **kwargs: (
            mainline_entered.set() or pl.DataFrame()
        ),
    )
    monkeypatch.setattr(
        market_mainline,
        "replace_mainline_history_range",
        lambda *args, **kwargs: None,
    )

    def invoke(fn) -> None:
        try:
            fn()
        except BaseException as exc:  # pragma: no cover - 断言线程异常
            errors.append(exc)

    pipeline_thread = threading.Thread(
        target=lambda: invoke(
            lambda: regime.regime_builder.compute_regime_incremental(
                request.app.state.repo,
                tmp_path,
                today=target,
            )
        ),
    )
    manual_thread = threading.Thread(
        target=lambda: invoke(lambda: regime.mainline_recompute(request)),
    )

    pipeline_thread.start()
    assert regime_entered.wait(1)
    manual_thread.start()
    blocked = not mainline_entered.wait(0.2)
    release_regime.set()
    pipeline_thread.join(2)
    manual_thread.join(2)

    assert blocked
    assert mainline_entered.is_set()
    assert not pipeline_thread.is_alive()
    assert not manual_thread.is_alive()
    assert errors == []


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
