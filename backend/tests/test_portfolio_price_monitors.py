"""持仓价格监控公开 API 契约测试。"""
from __future__ import annotations

from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.portfolio import router
from app.services import portfolio_price_monitors
from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine


class CaptureMonitorEngine:
    def __init__(self) -> None:
        self.rules: list[dict] = []

    def set_rules(self, rules: list[dict]) -> None:
        self.rules = rules


def make_client(tmp_path) -> tuple[TestClient, CaptureMonitorEngine]:
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    engine = CaptureMonitorEngine()
    app.state.monitor_engine = engine
    app.include_router(router)
    return TestClient(app), engine


def test_stop_loss_is_required(tmp_path):
    client, _engine = make_client(tmp_path)

    response = client.put(
        "/api/portfolio/positions/600519.SH/price-monitor",
        json={
            "name": "贵州茅台",
            "asset_type": "stock",
            "add_position_price": 1480,
        },
    )

    assert response.status_code == 422
    assert monitor_rules.load_all(tmp_path) == []


def test_save_creates_distinct_stop_loss_and_add_position_rules(tmp_path):
    client, engine = make_client(tmp_path)

    response = client.put(
        "/api/portfolio/positions/600519.SH/price-monitor",
        json={
            "name": "贵州茅台",
            "asset_type": "stock",
            "stop_loss_price": 1450,
            "add_position_price": 1500,
            "webhook_channels": ["feishu"],
        },
    )

    assert response.status_code == 200
    item = response.json()
    assert item == {
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "asset_type": "stock",
        "stop_loss_price": 1450.0,
        "stop_loss_enabled": True,
        "add_position_price": 1500.0,
        "add_position_enabled": True,
        "webhook_channels": ["feishu"],
    }

    saved = {rule["id"]: rule for rule in monitor_rules.load_all(tmp_path)}
    assert set(saved) == {"pf_stop_600519_sh", "pf_add_600519_sh"}
    assert saved["pf_stop_600519_sh"]["conditions"] == [
        {"field": "last_price", "op": "<=", "value": 1450.0}
    ]
    assert saved["pf_stop_600519_sh"]["severity"] == "critical"
    assert saved["pf_stop_600519_sh"]["direction"] == "exit"
    assert saved["pf_stop_600519_sh"]["cooldown_seconds"] == 1200
    assert saved["pf_add_600519_sh"]["conditions"] == [
        {"field": "last_price", "op": "<=", "value": 1500.0}
    ]
    assert saved["pf_add_600519_sh"]["severity"] == "warn"
    assert saved["pf_add_600519_sh"]["direction"] == "entry"
    assert saved["pf_add_600519_sh"]["cooldown_seconds"] == 1200
    assert {rule["id"] for rule in engine.rules} == set(saved)

    listed = client.get("/api/portfolio/price-monitors")
    assert listed.status_code == 200
    assert listed.json() == {"items": [item]}


def test_intraday_last_price_controls_trigger_not_daily_close(tmp_path):
    client, _capture = make_client(tmp_path)
    response = client.put(
        "/api/portfolio/positions/600519.SH/price-monitor",
        json={
            "name": "贵州茅台",
            "asset_type": "stock",
            "stop_loss_price": 1450,
        },
    )
    assert response.status_code == 200

    engine = MonitorRuleEngine()
    engine.set_rules(monitor_rules.load_all(tmp_path))

    # 历史收盘价已经低于止损价，但盘中最新价仍在止损价上方，不应触发。  # noqa: RUF003
    above = pl.DataFrame({
        "symbol": ["600519.SH"],
        "close": [1400.0],
        "last_price": [1500.0],
        "change_pct": [0.01],
    })
    assert engine.evaluate(above) == []

    # 只有盘中最新价跌至阈值下方才触发，告警价格也必须取盘中最新价。  # noqa: RUF003
    below = pl.DataFrame({
        "symbol": ["600519.SH"],
        "close": [1500.0],
        "last_price": [1400.0],
        "change_pct": [-0.01],
    })
    events = engine.evaluate(below)
    assert len(events) == 1
    assert events[0]["price"] == 1400.0
    assert events[0]["conditions"] == [
        {"field": "last_price", "op": "<=", "value": 1450.0}
    ]


def test_add_position_price_must_be_above_stop_loss(tmp_path):
    client, _engine = make_client(tmp_path)

    response = client.put(
        "/api/portfolio/positions/600519.SH/price-monitor",
        json={
            "name": "贵州茅台",
            "asset_type": "stock",
            "stop_loss_price": 1500,
            "add_position_price": 1450,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "加仓价必须高于止损价"
    assert monitor_rules.load_all(tmp_path) == []


def test_update_without_add_position_removes_only_optional_rule(tmp_path):
    client, engine = make_client(tmp_path)
    url = "/api/portfolio/positions/510300.SH/price-monitor"

    created = client.put(
        url,
        json={
            "name": "沪深300ETF",
            "asset_type": "etf",
            "stop_loss_price": 3.8,
            "add_position_price": 4.0,
        },
    )
    assert created.status_code == 200

    updated = client.put(
        url,
        json={
            "name": "沪深300ETF",
            "asset_type": "etf",
            "stop_loss_price": 3.85,
            "add_position_price": None,
        },
    )

    assert updated.status_code == 200
    assert updated.json()["stop_loss_price"] == 3.85
    assert updated.json()["add_position_price"] is None
    assert monitor_rules.load_one(tmp_path, "pf_add_510300_sh") is None
    stop_rule = monitor_rules.load_one(tmp_path, "pf_stop_510300_sh")
    assert stop_rule is not None
    assert stop_rule["asset_type"] == "etf"
    assert stop_rule["conditions"][0]["value"] == 3.85
    assert {rule["id"] for rule in engine.rules} == {"pf_stop_510300_sh"}


def test_disabled_stop_loss_is_reported_as_incomplete(tmp_path):
    client, _engine = make_client(tmp_path)
    rule = monitor_rules.normalize(
        {
            "id": "pf_stop_600519_sh",
            "name": "持仓止损 · 贵州茅台",
            "enabled": False,
            "type": "price",
            "asset_type": "stock",
            "scope": "symbols",
            "symbols": ["600519.SH"],
            "direction": "exit",
            "conditions": [{"field": "close", "op": "<=", "value": 1450}],
            "logic": "and",
            "severity": "critical",
        }
    )
    monitor_rules.save_one(tmp_path, rule)

    response = client.get("/api/portfolio/price-monitors")

    assert response.status_code == 200
    assert response.json()["items"][0]["stop_loss_price"] == 1450.0
    assert response.json()["items"][0]["stop_loss_enabled"] is False


def test_migrate_legacy_rule_makes_intraday_price_basis_explicit(tmp_path):
    legacy = monitor_rules.normalize(
        {
            "id": "pf_stop_600519_sh",
            "name": "持仓止损 · 贵州茅台",
            "type": "price",
            "scope": "symbols",
            "symbols": ["600519.SH"],
            "direction": "exit",
            "conditions": [{"field": "close", "op": "<=", "value": 1450}],
        }
    )
    monitor_rules.save_one(tmp_path, legacy)

    assert portfolio_price_monitors.migrate_legacy_rules(tmp_path) == 1
    migrated = monitor_rules.load_one(tmp_path, "pf_stop_600519_sh")
    assert migrated is not None
    assert migrated["conditions"] == [
        {"field": "last_price", "op": "<=", "value": 1450}
    ]
    assert migrated["message"] == "贵州茅台 分时最新价已跌至止损价 1450"
    assert portfolio_price_monitors.migrate_legacy_rules(tmp_path) == 0
