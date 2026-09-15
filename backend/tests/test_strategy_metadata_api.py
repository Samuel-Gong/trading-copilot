"""helper 消费当前策略说明及关联选股导出的契约。"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import screener, strategy
from app.services import strategy_cache
from app.strategy import config
from app.strategy.engine import StrategyDef, StrategyEngine


def make_strategy(sid, **meta):
    return StrategyDef(
        meta={"id": sid, "name": f"示例{sid}", **meta},
        basic_filter={}, entry_signals=[], exit_signals=[], stop_loss=None,
        trailing_stop=None, trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None, max_hold_days=None,
        filter_fn=None, filter_history_fn=None, lookback_days=1, source="custom",
    )


@pytest.fixture
def client(tmp_path):
    engine = StrategyEngine(strategy_dirs=[])
    engine._strategies = {
        "alpha": make_strategy("alpha", description="默认规则"),
        "beta": make_strategy("beta"),
        "etf": make_strategy("etf", asset_types=["etf"]),
        "minute": make_strategy("minute", timeframes=["1m"]),
        "draft": make_strategy("draft", research_only=True),
    }
    app = FastAPI()
    app.include_router(strategy.router)
    app.include_router(screener.router)
    app.state.strategy_engine = engine
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def metadata(client):
    response = client.get("/api/strategies?asset_type=stock&timeframe=1d")
    assert response.status_code == 200
    return {item["id"]: {key: item[key] for key in ("id", "name", "description")}
            for item in response.json()["strategies"]}


def test_current_metadata_filters_and_joins_saved_export(client):
    data_dir = client.app.state.repo.store.data_dir
    config.save_override(data_dir, "alpha", {"name": "当前名称", "description": "当前规则"})
    strategy_cache.write_cache(data_dir, "2026-09-04", {
        sid: {"as_of": "2026-09-04", "asset_type": "stock", "timeframe": "1d",
              "total": 1, "rows": [{"symbol": "000001.SZ", "name": "合成股票"}]}
        for sid in ("alpha", "beta")
    })
    items = metadata(client)
    assert items == {
        "alpha": {"id": "alpha", "name": "当前名称", "description": "当前规则"},
        "beta": {"id": "beta", "name": "示例beta", "description": ""},
    }
    response = client.get("/api/screener/export?as_of=2026-09-04")
    assert response.status_code == 200
    exported = response.json()
    assert set(exported["results"]) == set(items)
    for sid, result in exported["results"].items():
        assert result["name"] == items[sid]["name"]
        assert result["rows"][0]["symbol"] == "000001.SZ"
    config.save_override(data_dir, "alpha", {"name": "更新名称", "description": "更新规则"})
    assert metadata(client)["alpha"]["description"] == "更新规则"
    assert client.get("/api/screener/export").json()["as_of"] == "2026-09-04"


@pytest.mark.parametrize("description", ["", None])
def test_empty_description_is_a_string(client, description):
    client.app.state.strategy_engine._strategies["alpha"].meta["description"] = description
    assert metadata(client)["alpha"]["description"] == ""


def test_empty_override_keeps_existing_default_semantics(client):
    config.save_override(client.app.state.repo.store.data_dir, "alpha",
                         {"name": "", "description": ""})
    assert metadata(client)["alpha"] == {
        "id": "alpha", "name": "示例alpha", "description": "默认规则",
    }


def test_empty_list_and_engine_unavailable(client):
    client.app.state.strategy_engine._strategies.clear()
    assert metadata(client) == {}
    client.app.state.strategy_engine = None
    response = client.get("/api/strategies?asset_type=stock&timeframe=1d")
    assert response.status_code == 503
    assert isinstance(response.json()["detail"], str)


def test_metadata_requires_existing_session(client, monkeypatch):
    from app.main import auth_middleware
    from app.services import auth

    client.app.middleware("http")(auth_middleware)
    monkeypatch.setattr(auth, "is_configured", lambda: True)
    monkeypatch.setattr(auth, "is_valid_session", lambda token: token == "synthetic-session")
    url = "/api/strategies?asset_type=stock&timeframe=1d"
    assert client.get(url).status_code == 401
    client.cookies.set("tf_session", "invalid")
    assert client.get(url).status_code == 401
    client.cookies.set("tf_session", "synthetic-session")
    assert metadata(client)["alpha"]["description"] == "默认规则"
