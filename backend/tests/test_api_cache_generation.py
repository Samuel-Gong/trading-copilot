"""接口 TTL 缓存失效与并发重建的代际边界。"""
from __future__ import annotations

import threading
from datetime import date
from types import SimpleNamespace

import polars as pl

from app.api import data, overview, regime


def test_table_stats_invalidation_during_fetch_does_not_refill_stale_cache() -> None:
    entered = threading.Event()
    release = threading.Event()
    first_result: list[dict | None] = []

    def slow_fetch() -> dict:
        entered.set()
        assert release.wait(2)
        return {"value": "stale"}

    data.invalidate_data_cache("daily")
    worker = threading.Thread(
        target=lambda: first_result.append(data._get_table_stats("daily", slow_fetch)),
    )
    worker.start()
    assert entered.wait(1)
    data.invalidate_data_cache("daily")
    release.set()
    worker.join(2)

    fetches = 0

    def fresh_fetch() -> dict:
        nonlocal fetches
        fetches += 1
        return {"value": "fresh"}

    assert not worker.is_alive()
    assert first_result == [{"value": "stale"}]
    assert data._get_table_stats("daily", fresh_fetch) == {"value": "fresh"}
    assert fetches == 1


def test_overview_invalidation_during_build_does_not_refill_stale_cache(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    builds = 0

    def build(_request, _as_of=None) -> dict:
        nonlocal builds
        builds += 1
        if builds == 1:
            entered.set()
            assert release.wait(2)
            return {"value": "stale"}
        return {"value": "fresh"}

    monkeypatch.setattr(overview, "_build_overview", build)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    first_result: list[dict] = []
    overview.invalidate_overview_cache()
    worker = threading.Thread(
        target=lambda: first_result.append(overview.market_overview(request)),
    )
    worker.start()
    assert entered.wait(1)
    overview.invalidate_overview_cache()
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert first_result == [{"value": "stale"}]
    assert overview.market_overview(request) == {"value": "fresh"}
    assert builds == 2


def test_regime_invalidation_during_load_does_not_refill_stale_cache(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    loads = 0

    def load(_data_dir) -> pl.DataFrame:
        nonlocal loads
        loads += 1
        if loads == 1:
            entered.set()
            assert release.wait(2)
            value = "stale"
        else:
            value = "fresh"
        return pl.DataFrame({"date": [date(2026, 9, 1)], "state": [value]})

    monkeypatch.setattr(regime.regime_builder, "load_regime_history", load)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=SimpleNamespace(store=SimpleNamespace(data_dir="unused")),
            ),
        ),
    )
    first_result: list[dict] = []
    regime.invalidate_regime_cache()
    worker = threading.Thread(
        target=lambda: first_result.append(
            regime.regime_history(request, start=None, end=None, limit=120)
        ),
    )
    worker.start()
    assert entered.wait(1)
    regime.invalidate_regime_cache()
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert first_result[0]["rows"][0]["state"] == "stale"
    assert regime.regime_history(
        request,
        start=None,
        end=None,
        limit=120,
    )["rows"][0]["state"] == "fresh"
    assert loads == 2
