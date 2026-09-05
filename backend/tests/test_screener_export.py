from __future__ import annotations

import csv
import io
import time
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
    return {"as_of": day, "asset_type": "stock", "timeframe": "1d", "scope": "all", "total": 1,
            "computed_at_ns": time.time_ns(),
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
    ({"results": {"alpha": result(scope="symbols")}}, None, None, 409),
    ({"results": {"alpha": result(scope=None)}}, None, None, 409),
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
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.strategy_engine = SimpleNamespace(list_strategies=lambda: [
        {"id": sid, "name": name, "asset_types": ["stock"], "timeframes": ["1d"]}
        for sid, name in NAMES.items()
    ])
    app.state.monitor_engine = SimpleNamespace(latest_strategy_results=lambda **_: {})
    monkeypatch.setattr(api.strategy_config, "list_overrides", lambda _: {})
    return TestClient(app)


def test_http_formats_realtime_and_dates(client):
    data_dir = client.app.state.repo.store.data_dir
    strategy_cache.write_cache(data_dir, DAY, {"alpha": result()})
    realtime = {"alpha": result([{"symbol": "000002.SZ", "score": float("inf")}])}
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
    assert client.get("/api/screener/export").status_code == 404
    strategy_cache.write_cache(client.app.state.repo.store.data_dir, DAY, {"alpha": result([])})
    assert client.get("/api/screener/export").json()["symbols"] == []
    assert client.get("/api/screener/export?format=txt").content == b""


@pytest.mark.parametrize("format", ["json", "csv", "txt"])
def test_explicit_date_uses_matching_disk_snapshot_despite_newer_monitor(client, format):
    strategy_cache.write_cache(client.app.state.repo.store.data_dir, DAY, {"alpha": result()})
    client.app.state.monitor_engine.latest_strategy_results = lambda **_: {
        "alpha": result([{"symbol": "600000.SH"}], day="2026-09-07"),
    }
    response = client.get("/api/screener/export", params={"as_of": DAY, "format": format})
    assert response.status_code == 200
    assert "000001.SZ" in response.text and "600000.SH" not in response.text
    latest = client.get("/api/screener/export").json()
    assert latest["as_of"] == "2026-09-07" and latest["symbols"] == ["600000.SH"]


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


def configure_monitor(client, monkeypatch, *, scope="all"):
    import polars as pl

    from app.strategy import monitor
    from app.strategy.engine import StrategyResult

    monkeypatch.setattr(monitor, "cn_today", lambda: date.fromisoformat(DAY))
    engine = client.app.state.strategy_engine
    engine.get = lambda _: SimpleNamespace(
        meta={"id": "alpha"}, execution_backend="polars_expr", filter_history_fn=None,
    )
    engine.run = lambda sid, context, **_: StrategyResult(
        as_of=date.fromisoformat(DAY), strategy_id=sid,
        rows=context.current.to_dicts(), total=context.current.height,
    )
    instance = monitor.MonitorRuleEngine()
    instance.set_strategy_engine(engine)
    rule = {"id": "test-rule", "type": "strategy", "strategy_id": "alpha",
            "scope": scope, "symbols": ["000001.SZ"], "asset_type": "stock"}
    instance.set_rules([rule])
    client.app.state.monitor_engine = instance
    quotes = pl.DataFrame({"symbol": ["000001.SZ", "600000.SH"], "close": [10., 20.]})
    return instance, rule, quotes


@pytest.mark.parametrize("format", ["json", "csv", "txt"])
def test_partial_monitor_cannot_replace_complete_selection(client, monkeypatch, format):
    instance, _, quotes = configure_monitor(client, monkeypatch, scope="symbols")
    strategy_cache.write_cache(client.app.state.repo.store.data_dir, DAY, {
        "alpha": result(quotes.to_dicts()),
    })
    instance.evaluate(quotes)
    response = client.get("/api/screener/export", params={"format": format, "as_of": DAY})
    assert response.status_code == 200
    assert "000001.SZ" in response.text and "600000.SH" in response.text
    strategy_cache.clear_cache(client.app.state.repo.store.data_dir)
    assert client.get("/api/screener/export").status_code == 404


@pytest.mark.parametrize("format", ["json", "csv", "txt"])
@pytest.mark.parametrize("monitor_day", [DAY, "2026-09-03"])
def test_completed_rerun_wins_over_older_monitor(client, monkeypatch, format, monitor_day):
    from app.strategy import monitor
    from app.strategy.engine import StrategyResult

    instance, _, quotes = configure_monitor(client, monkeypatch)
    monkeypatch.setattr(monitor, "cn_today", lambda: date.fromisoformat(monitor_day))
    instance.evaluate(quotes.head(1))
    engine = client.app.state.strategy_engine
    engine.has = lambda _: True
    engine.run = lambda sid, context, **_: StrategyResult(
        as_of=date.fromisoformat(DAY), strategy_id=sid,
        rows=[{"symbol": "600000.SH"}], total=1,
    )
    monkeypatch.setattr(api.ScreenerService, "build_strategy_context", lambda *_, **__: None)
    assert client.post("/api/screener/run_preset", json={
        "strategy_id": "alpha", "as_of": DAY,
    }).status_code == 200
    for params in [{"format": format}, {"format": format, "as_of": DAY}]:
        response = client.get("/api/screener/export", params=params)
        assert response.status_code == 200
        assert "600000.SH" in response.text and "000001.SZ" not in response.text


@pytest.mark.parametrize("change", ["remove", "clear", "reload", "disable", "replace"])
def test_rule_change_invalidates_export_snapshot(client, monkeypatch, change):
    instance, rule, quotes = configure_monitor(client, monkeypatch)
    strategy_cache.write_cache(client.app.state.repo.store.data_dir, DAY, {
        "alpha": result([{"symbol": "600000.SH"}]),
    })
    instance.evaluate(quotes.head(1))
    assert client.get("/api/screener/export").json()["symbols"] == ["000001.SZ"]
    if change == "remove":
        instance.remove_rule(rule["id"])
        instance.add_rule(dict(rule))
    elif change == "clear":
        instance.clear()
    elif change == "reload":
        instance.set_rules([])
    elif change == "disable":
        instance.add_rule({**rule, "enabled": False})
    else:
        instance.add_rule({**rule, "scope": "symbols"})
    assert client.get("/api/screener/export").json()["symbols"] == ["600000.SH"]


@pytest.mark.parametrize("partial_first", [True, False])
def test_complete_monitor_wins_regardless_of_rule_order(client, monkeypatch, partial_first):
    instance, rule, quotes = configure_monitor(client, monkeypatch)
    partial = {**rule, "id": "partial", "scope": "symbols"}
    instance.set_rules([partial, rule] if partial_first else [rule, partial])
    instance.evaluate(quotes)
    assert client.get("/api/screener/export").json()["symbols"] == ["000001.SZ", "600000.SH"]


def test_rule_removed_during_evaluation_cannot_publish_export(client, monkeypatch):
    instance, rule, quotes = configure_monitor(client, monkeypatch)
    engine = client.app.state.strategy_engine
    original_run = engine.run

    def run_and_replace(*args, **kwargs):
        instance.remove_rule(rule["id"])
        instance.add_rule(dict(rule))
        return original_run(*args, **kwargs)

    engine.run = run_and_replace
    instance.evaluate(quotes)
    assert client.get("/api/screener/export").status_code == 404


def test_source_selection_uses_per_strategy_completion_not_file_update():
    stored = result([{"symbol": "000001.SZ"}], computed_at_ns=10)
    live = result([{"symbol": "600000.SH"}], computed_at_ns=20)
    cached = {"updated_at": 30, "results": {"alpha": stored}}
    assert build_export(cached, NAMES, realtime_results={"alpha": live})["symbols"] == ["600000.SH"]
    cached["results"]["alpha"] = result([{"symbol": "000001.SZ"}], computed_at_ns=30)
    assert build_export(cached, NAMES, realtime_results={"alpha": live})["symbols"] == ["000001.SZ"]


def test_limited_single_run_cannot_be_exported_as_complete(client, monkeypatch):
    from app.strategy.engine import StrategyResult

    engine = client.app.state.strategy_engine
    engine.has = lambda _: True
    engine.run = lambda sid, context, **_: StrategyResult(
        as_of=date.fromisoformat(DAY), strategy_id=sid,
        rows=[{"symbol": "000001.SZ"}], total=1,
    )
    monkeypatch.setattr(api.ScreenerService, "build_strategy_context", lambda *_, **__: None)
    assert client.post("/api/screener/run_preset", json={
        "strategy_id": "alpha", "as_of": DAY, "pool": ["000001.SZ"],
    }).status_code == 200
    assert client.get("/api/screener/export").status_code == 409


@pytest.mark.parametrize("method", ["set_rules", "add_rule"])
def test_rule_publication_cannot_pair_old_rule_with_new_version(client, monkeypatch, method):
    from threading import Event, Thread

    instance, rule, quotes = configure_monitor(client, monkeypatch)
    published, resume = Event(), Event()

    class PausingMonitor(type(instance)):
        def __setattr__(self, name, value):
            super().__setattr__(name, value)
            if name == "_rule_versions":
                published.set()
                assert resume.wait(timeout=5)

    class PausingVersions(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            published.set()
            assert resume.wait(timeout=5)

    replacement = {**rule, "scope": "symbols"}
    if method == "set_rules":
        instance.__class__ = PausingMonitor
        worker = Thread(target=instance.set_rules, args=([replacement],))
    else:
        instance._rule_versions = PausingVersions(instance._rule_versions)
        worker = Thread(target=instance.add_rule, args=(replacement,))
    worker.start()
    try:
        assert published.wait(timeout=5)
        # 规则发布线程暂停时, 行情线程仍可以完成一次旧规则评估。
        instance.evaluate(quotes)
    finally:
        resume.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert client.get("/api/screener/export").status_code == 404
