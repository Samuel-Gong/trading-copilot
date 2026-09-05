from __future__ import annotations

import csv
import io
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import screener as api
from app.services import strategy_cache
from app.services.screener_export import ExportError, build_export, export_csv

DAY = "2026-09-04"
NAMES = {"alpha": "测试策略甲", "beta": "测试策略乙"}


def result(rows=None, day=DAY, **extra):
    return {"as_of": day, "asset_type": "stock", "timeframe": "1d", "total": 1,
            "rows": rows if rows is not None else [{"symbol": "000001.SZ", "name": "合成股票"}], **extra}


def test_export_preserves_order_units_membership_and_cache():
    cached = {"results": {
        "alpha": result([{"symbol": "000001.SZ", "change_pct": -0.025,
                          "turnover_rate": 5, "score": float("nan")}]),
        "beta": result([{"symbol": "000001.SZ", "score": 90}, {"symbol": "600000.SH"}]),
    }, "today_ever_rows": {"alpha": {"999999.SZ": {"symbol": "999999.SZ"}}}}
    payload = build_export(cached, NAMES, ["beta", "alpha", "beta"])
    assert list(payload["results"]) == ["beta", "alpha"]
    assert payload["symbols"] == ["000001.SZ", "600000.SH"]
    assert payload["total"] == 2 and payload["as_of"] == DAY
    assert payload["results"]["alpha"]["rows"][0] == {
        "symbol": "000001.SZ", "change_pct": -0.025, "turnover_rate": 5, "score": None,
    }
    payload["results"]["beta"]["rows"][0]["score"] = 0
    assert cached["results"]["beta"]["rows"][0]["score"] == 90


def test_csv_encoding_escaping_formulas_and_empty():
    payload = build_export({"results": {"alpha": result([
        {"symbol": "000001.SZ", "name": '测试,"股票"\n换行', "change_pct": -0.05},
        {"symbol": "600000.SH", "name": ' \t=HYPERLINK("x")'},
    ])}}, {"alpha": "+公式策略"})
    encoded = export_csv(payload)
    assert encoded.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(encoded.decode("utf-8-sig"))))
    assert rows[0]["symbol"] == "000001.SZ"
    assert rows[0]["name"] == '测试,"股票"\n换行'
    assert rows[0]["strategy_name"] == "'+公式策略"
    assert rows[0]["change_pct"] == "-0.05" and rows[0]["score"] == ""
    assert rows[1]["name"].startswith("'")
    empty = build_export({"results": {"alpha": result([])}}, NAMES)
    assert empty["total"] == 0 and empty["symbols"] == []
    assert len(export_csv(empty).decode("utf-8-sig").splitlines()) == 1


@pytest.mark.parametrize(("cached", "ids", "as_of", "status"), [
    ({}, None, None, 404),
    ({"results": {"deleted": result()}}, None, None, 404),
    ({"results": {"alpha": result()}}, ["unknown"], None, 404),
    ({"results": {"alpha": result()}}, ["alpha", "beta"], None, 409),
    ({"results": {"alpha": result()}}, None, date(2026, 9, 3), 409),
    ({"results": {"alpha": result(), "beta": result(day="2026-09-03")}}, None, None, 409),
    ({"results": {"alpha": result(asset_type="etf")}}, None, None, 409),
    ({"results": {"alpha": result(timeframe="1m")}}, None, None, 409),
    ({"results": {"alpha": {"as_of": DAY, "rows": []}}}, None, None, 409),
    ({"results": {"alpha": result(day="invalid")}}, None, None, 409),
    ({"results": {"alpha": result([{"symbol": 1}])}}, None, None, 409),
    ({"results": {"alpha": result([{"symbol": "000001.SZ\n600000.SH"}])}}, None, None, 409),
])
def test_export_rejects_incomplete_or_wrong_context(cached, ids, as_of, status):
    with pytest.raises(ExportError) as exc:
        build_export(cached, NAMES, ids, as_of)
    assert exc.value.status_code == status


@pytest.fixture
def client(tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        enriched_latest_date=lambda: date.fromisoformat(DAY),
        get_enriched_latest_asset=lambda _asset_type: (None, None),
    )
    app.state.strategy_engine = SimpleNamespace(list_strategies=lambda: [
        {"id": sid, "name": name, "asset_types": ["stock"], "timeframes": ["1d"]}
        for sid, name in NAMES.items()
    ])
    app.state.monitor_engine = SimpleNamespace(latest_strategy_results=lambda **_: {})
    monkeypatch.setattr(api.strategy_config, "list_overrides", lambda _: {})
    return TestClient(app)


def test_http_formats_and_dates(client):
    data_dir = client.app.state.repo.store.data_dir
    strategy_cache.write_cache(data_dir, DAY, {"alpha": result([{"symbol": "000002.SZ", "score": float("inf")}])})
    realtime = {"alpha": result([{"symbol": "000003.SZ"}])}
    client.app.state.monitor_engine.latest_strategy_results = lambda **_: realtime
    response = client.get("/api/screener/export", params={"strategy_id": "alpha", "as_of": DAY})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["symbols"] == ["000002.SZ"]
    assert response.json()["results"]["alpha"]["rows"][0]["score"] is None
    txt = client.get("/api/screener/export?strategy_id=alpha&format=txt")
    assert txt.content == b"000002.SZ\r\n"
    assert txt.headers["content-disposition"] == f'attachment; filename="screener-{DAY}.txt"'
    csv_response = client.get("/api/screener/export?strategy_id=alpha&format=csv")
    assert csv_response.content.startswith(b"\xef\xbb\xbf")
    assert csv_response.headers["content-type"] == "text/csv; charset=utf-8"
    for query, status in [("as_of=2026-09-03", 409), ("format=xlsx", 422),
                          ("as_of=not-a-date", 422), ("strategy_id=alpha&strategy_id=beta", 409)]:
        assert client.get(f"/api/screener/export?{query}").status_code == status
    client.app.state.strategy_engine = None
    assert client.get("/api/screener/export").status_code == 503


def test_http_empty_vs_missing(client):
    client.app.state.monitor_engine.latest_strategy_results = lambda **_: {"alpha": result()}
    assert client.get("/api/screener/export").status_code == 404
    strategy_cache.write_cache(client.app.state.repo.store.data_dir, DAY, {"alpha": result([])})
    assert client.get("/api/screener/export").json()["symbols"] == []
    assert client.get("/api/screener/export?format=txt").content == b""


@pytest.mark.parametrize("format", ["json", "csv", "txt"])
@pytest.mark.parametrize("monitor_day", ["2026-09-03", DAY, "2026-09-07"])
def test_any_monitor_snapshot_cannot_change_saved_export(client, format, monitor_day):
    strategy_cache.write_cache(client.app.state.repo.store.data_dir, DAY, {"alpha": result()})
    client.app.state.monitor_engine.latest_strategy_results = lambda **_: {
        "alpha": result([{"symbol": "600000.SH"}], day=monitor_day),
    }
    response = client.get("/api/screener/export", params={"as_of": DAY, "format": format})
    assert response.status_code == 200
    assert "000001.SZ" in response.text and "600000.SH" not in response.text
    latest = client.get("/api/screener/export").json()
    assert latest["as_of"] == DAY and latest["symbols"] == ["000001.SZ"]


def test_export_requires_existing_session(client, monkeypatch):
    from app.main import auth_middleware
    from app.services import auth

    client.app.middleware("http")(auth_middleware)
    monkeypatch.setattr(auth, "is_configured", lambda: True)
    monkeypatch.setattr(auth, "is_valid_session", lambda token: token == "synthetic-session")
    strategy_cache.write_cache(client.app.state.repo.store.data_dir, DAY, {"alpha": result()})
    assert client.get("/api/screener/export").status_code == 401
    client.cookies.set("tf_session", "invalid")
    assert client.get("/api/screener/export?format=csv").status_code == 401
    client.cookies.set("tf_session", "synthetic-session")
    assert client.get("/api/screener/export").status_code == 200


def test_first_single_run_persists_and_other_assets_cannot_overwrite(client, monkeypatch):
    from app.services.screener import ScreenerResult

    engine = client.app.state.strategy_engine
    engine.has = lambda _: True
    engine.run = lambda sid, ctx, **_: ScreenerResult(
        as_of=date.fromisoformat(DAY), strategy=sid, rows=[{"symbol": "000001.SZ"}], total=1,
    )
    monkeypatch.setattr(api.ScreenerService, "build_strategy_context", lambda *_, **__: None)
    monkeypatch.setattr(api, "_load_ext_value_maps", lambda *_: {})
    for asset, timeframe in [("stock", "1d"), ("etf", "1d"), ("stock", "1m")]:
        response = client.post("/api/screener/run_preset", json={
            "strategy_id": "alpha", "as_of": DAY, "asset_type": asset, "timeframe": timeframe,
        })
        assert response.status_code == 200
        cached = strategy_cache.read_cache(client.app.state.repo.store.data_dir)
        if asset == "stock" and timeframe == "1d":
            original = cached
        else:
            assert cached == original
    assert client.get("/api/screener/export?strategy_id=alpha").json()["symbols"] == ["000001.SZ"]


def test_batch_run_marks_stock_daily_results_and_does_not_cache_other_contexts(client, monkeypatch):
    from app.services.screener import ScreenerResult

    engine = client.app.state.strategy_engine
    engine.has = lambda _: True
    engine.run_all = lambda *_, **__: {"alpha": ScreenerResult(
        as_of=date.fromisoformat(DAY), strategy="alpha", rows=[{"symbol": "000001.SZ"}], total=1,
    )}
    monkeypatch.setattr(api.ScreenerService, "build_strategy_context", lambda *_, **__: None)
    for asset, timeframe in [("stock", "1d"), ("etf", "1d"), ("stock", "1m")]:
        response = client.post("/api/screener/run_all", json={
            "strategy_ids": ["alpha"], "as_of": DAY, "asset_type": asset,
            "timeframe": timeframe, "summary_only": True,
        })
        assert response.status_code == 200
        cached = strategy_cache.read_cache(client.app.state.repo.store.data_dir)
        if asset == "stock" and timeframe == "1d":
            original = cached
            assert cached["results"]["alpha"]["asset_type"] == "stock"
            assert cached["results"]["alpha"]["timeframe"] == "1d"
        else:
            assert cached == original


@pytest.mark.parametrize(("overrides", "expected_count"), [
    ({}, 2), ({"display_limit": 3}, 3), ({"display_limit": None}, 5),
])
@pytest.mark.parametrize("format", ["json", "csv", "txt"])
@pytest.mark.parametrize(("run_kind", "pool"), [
    ("single", None), ("single", ["000002.SZ", "000004.SZ"]), ("batch", None),
])
def test_export_matches_real_engine_result_limits(client, monkeypatch, overrides, expected_count, format, run_kind, pool):
    import polars as pl

    from app.strategy.engine import StrategyDataContext, StrategyDef, StrategyEngine

    engine = StrategyEngine(strategy_dirs=[])
    engine._strategies["alpha"] = StrategyDef(
        meta={"id": "alpha", "name": "合成策略", "limit": 2, "scoring": {}},
        basic_filter={"enabled": False}, entry_signals=[], exit_signals=[],
        stop_loss=None, trailing_stop=None, trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None, max_hold_days=None, alerts=[],
        filter_fn=lambda df, params: pl.col("close") > 0, filter_history_fn=None,
        lookback_days=1, source="custom",
    )
    client.app.state.strategy_engine = engine
    quotes = pl.DataFrame({"symbol": [f"{i:06d}.SZ" for i in range(1, 6)], "close": [10.] * 5})
    context = StrategyDataContext("stock", "1d", date.fromisoformat(DAY), current=quotes)
    monkeypatch.setattr(api.ScreenerService, "build_strategy_context", lambda *_, **__: context)
    monkeypatch.setattr(api.strategy_config, "load_override", lambda *_: overrides)
    if run_kind == "single":
        run = client.post("/api/screener/run_preset", json={"strategy_id": "alpha", "as_of": DAY, "pool": pool})
    else:
        monkeypatch.setattr(api.strategy_config, "list_overrides", lambda *_: {"alpha": overrides})
        run = client.post("/api/screener/run_all", json={"strategy_ids": ["alpha"], "as_of": DAY})
    assert run.status_code == 200
    rows = run.json()["rows"] if run_kind == "single" else run.json()["results"]["alpha"]["rows"]
    selected = [row["symbol"] for row in rows]
    assert len(selected) == (min(expected_count, len(pool)) if pool else expected_count)
    if pool:
        assert selected == pool[:expected_count]
    exported = client.get("/api/screener/export", params={"format": format})
    assert exported.status_code == 200
    if format == "json":
        assert exported.json()["symbols"] == selected
    elif format == "csv":
        rows = csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig")))
        assert [row["symbol"] for row in rows] == selected
    else:
        assert exported.text.splitlines() == selected


@pytest.mark.parametrize("format", ["json", "csv", "txt"])
@pytest.mark.parametrize("run_kind", ["single", "batch"])
def test_historical_run_keeps_newer_pool_cache(client, monkeypatch, format, run_kind):
    from app.strategy.engine import StrategyResult

    data_dir = client.app.state.repo.store.data_dir
    strategy_cache.write_cache(data_dir, DAY, {
        "alpha": result([{"symbol": "000001.SZ"}]),
        "beta": result([{"symbol": "600000.SH"}]),
    })
    original = strategy_cache.read_cache(data_dir)
    engine = client.app.state.strategy_engine
    engine.has = lambda _: True
    historical_result = StrategyResult(
        as_of=date(2026, 9, 3), strategy_id="alpha",
        rows=[{"symbol": "000003.SZ"}], total=1,
    )
    if run_kind == "single":
        engine.run = lambda sid, context, **_: historical_result
    else:
        engine.run_all = lambda *_, **__: {"alpha": historical_result}
    monkeypatch.setattr(api.ScreenerService, "build_strategy_context", lambda *_, **__: None)
    if run_kind == "single":
        historical = client.post("/api/screener/run_preset", json={
            "strategy_id": "alpha", "as_of": "2026-09-03",
        })
        rows = historical.json()["rows"]
    else:
        historical = client.post("/api/screener/run_all", json={
            "strategy_ids": ["alpha"], "as_of": "2026-09-03",
        })
        rows = historical.json()["results"]["alpha"]["rows"]
    assert historical.status_code == 200
    assert rows[0]["symbol"] == "000003.SZ"
    assert strategy_cache.read_cache(data_dir) == original
    exported = client.get("/api/screener/export", params={"format": format})
    assert exported.status_code == 200
    assert "000001.SZ" in exported.text and "600000.SH" in exported.text
    assert "000003.SZ" not in exported.text
    assert client.get("/api/screener/export?as_of=2026-09-03").status_code == 409


@pytest.mark.parametrize("run_kind", ["single", "batch"])
def test_empty_input_date_cannot_replace_export_cache(client, monkeypatch, run_kind):
    import polars as pl

    data_dir = client.app.state.repo.store.data_dir
    strategy_cache.write_cache(data_dir, DAY, {"alpha": result()})
    original = strategy_cache.read_cache(data_dir)
    engine = client.app.state.strategy_engine
    engine.has = lambda _: True
    calls: list[str] = []

    def unexpected_run(*_args, **_kwargs):
        calls.append(run_kind)
        raise AssertionError("空输入不应执行策略")

    if run_kind == "single":
        engine.run = unexpected_run
    else:
        engine.run_all = unexpected_run
    monkeypatch.setattr(
        api.ScreenerService,
        "build_strategy_context",
        lambda *_args, **_kwargs: SimpleNamespace(current=pl.DataFrame()),
    )

    if run_kind == "single":
        response = client.post("/api/screener/run_preset", json={
            "strategy_id": "alpha", "as_of": "2099-01-01",
        })
    else:
        response = client.post("/api/screener/run_all", json={
            "strategy_ids": ["alpha"], "as_of": "2099-01-01",
        })

    assert response.status_code == 400
    assert "无可用选股数据" in response.json()["detail"]
    assert calls == []
    assert strategy_cache.read_cache(data_dir) == original


def test_historical_batch_run_without_cache_is_not_export_snapshot(client, monkeypatch):
    import polars as pl

    from app.services.screener import ScreenerResult

    engine = client.app.state.strategy_engine
    engine.has = lambda _: True
    engine.run_all = lambda *_, **__: {
        "alpha": ScreenerResult(
            as_of=date(2026, 9, 3),
            strategy="alpha",
            rows=[{"symbol": "000003.SZ"}],
            total=1,
        ),
    }
    monkeypatch.setattr(
        api.ScreenerService,
        "build_strategy_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            current=pl.DataFrame({"symbol": ["000003.SZ"]}),
        ),
    )

    response = client.post("/api/screener/run_all", json={
        "strategy_ids": ["alpha"], "as_of": "2026-09-03",
    })

    assert response.status_code == 200
    assert response.json()["results"]["alpha"]["rows"][0]["symbol"] == "000003.SZ"
    assert strategy_cache.read_cache(client.app.state.repo.store.data_dir) is None
    assert client.get("/api/screener/export").status_code == 404
