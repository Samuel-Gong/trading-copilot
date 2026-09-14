"""专用监控路径与规则更新并发时的代际回归测试。"""
from __future__ import annotations

import threading
from datetime import date

import polars as pl

from app.strategy import monitor as monitor_module
from app.strategy.monitor import MonitorRuleEngine


def _date_rule(rule_id: str) -> dict:
    return {
        "id": rule_id,
        "name": rule_id,
        "type": "date",
        "asset_type": "stock",
        "scope": "symbols",
        "symbols": ["600519.SH"],
        "remind_date": "2026-08-30",
        "lead_days": 3,
        "cooldown_seconds": 86400,
        "enabled": True,
    }


def _sector_rule() -> dict:
    target = {
        "key": "index:000001.SH",
        "kind": "index",
        "name": "上证指数",
        "symbol": "000001.SH",
    }
    return {
        "id": "sector-old",
        "name": "旧板块规则",
        "type": "sector",
        "enabled": True,
        "sector_targets": [target],
        "sector_trigger": "change_pct",
        "direction": "up",
        "threshold_pct": 1.0,
        "cooldown_seconds": 0,
    }


def _abnormal_rule() -> dict:
    return {
        "id": "abnormal-old",
        "name": "旧异动规则",
        "type": "abnormal",
        "enabled": True,
        "scope": "all",
        "direction": "both",
        "threshold_pct": 70,
        "abnormal_window": "any",
        "cooldown_seconds": 0,
    }


def _abnormal_row(value: float) -> dict:
    return {
        "symbol": "600000.SH",
        "name": "浦发银行",
        "close": 10.0,
        "rt_pct": 0.01,
        "board": "主板",
        "st": False,
        "windows": {
            "3d": {
                "value": value,
                "threshold": 0.2,
                "closeness": abs(value) / 0.2,
            },
        },
    }


class _SectorService:
    def build_snapshots(
        self,
        _stock_df,
        index_df,
        targets,
        _windows,
        *,
        now,
    ) -> dict[str, dict]:
        del now
        change_pct = float(index_df["change_pct"][0])
        target = targets[0]
        return {
            target["key"]: {
                **target,
                "valid": True,
                "price": 3000.0,
                "change_pct": change_pct,
            },
        }


def test_date_rule_update_discards_inflight_alert_and_cache(monkeypatch) -> None:
    monkeypatch.setattr(monitor_module, "cn_today", lambda: date(2026, 8, 28))
    started = threading.Event()
    release = threading.Event()
    seen: list[dict] = []
    result: list[dict] = []
    errors: list[BaseException] = []
    engine = MonitorRuleEngine(alert_handler=seen.append)
    engine.set_rules([_date_rule("date-old")])
    original = monitor_module.date_rule_in_window

    def blocking_window(*args) -> bool:
        started.set()
        assert release.wait(timeout=2)
        return original(*args)

    monkeypatch.setattr(monitor_module, "date_rule_in_window", blocking_window)

    def evaluate() -> None:
        try:
            result.extend(engine.evaluate_date_rules(now=1000.0))
        except BaseException as exc:  # pragma: no cover - 仅用于传递线程异常
            errors.append(exc)

    worker = threading.Thread(target=evaluate)
    worker.start()
    assert started.wait(timeout=2)
    engine.set_rules([_date_rule("date-new")])
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors == []
    assert result == []
    assert seen == []
    assert [event["rule_id"] for event in engine.evaluate_date_rules(now=1001.0)] == [
        "date-new",
    ]


def test_sector_rule_delete_discards_inflight_state_and_alert(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    seen: list[dict] = []
    result: list[dict] = []
    errors: list[BaseException] = []
    engine = MonitorRuleEngine(alert_handler=seen.append)
    engine.set_sector_monitor_service(_SectorService())
    engine.set_rules([_sector_rule()])
    low = pl.DataFrame({"change_pct": [0.005]})
    high = pl.DataFrame({"change_pct": [0.02]})
    assert engine.evaluate_sectors(pl.DataFrame(), low, now=1000.0) == []
    original = engine._evaluate_sector_rule

    def blocking_evaluate(*args, **kwargs) -> list[dict]:
        started.set()
        assert release.wait(timeout=2)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "_evaluate_sector_rule", blocking_evaluate)

    def evaluate() -> None:
        try:
            result.extend(engine.evaluate_sectors(pl.DataFrame(), high, now=1001.0))
        except BaseException as exc:  # pragma: no cover - 仅用于传递线程异常
            errors.append(exc)

    worker = threading.Thread(target=evaluate)
    worker.start()
    assert started.wait(timeout=2)
    engine.set_rules([])
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors == []
    assert result == []
    assert seen == []
    assert engine._sector_condition_state == {}


def test_abnormal_rule_delete_discards_inflight_state_and_alert(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    seen: list[dict] = []
    result: list[dict] = []
    errors: list[BaseException] = []
    engine = MonitorRuleEngine(alert_handler=seen.append)
    engine.set_rules([_abnormal_rule()])
    assert engine.evaluate_abnormal([_abnormal_row(0.1)], now=1000.0) == []
    original = engine._evaluate_abnormal_rule

    def blocking_evaluate(*args, **kwargs) -> list[dict]:
        started.set()
        assert release.wait(timeout=2)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "_evaluate_abnormal_rule", blocking_evaluate)

    def evaluate() -> None:
        try:
            result.extend(engine.evaluate_abnormal([_abnormal_row(0.16)], now=1001.0))
        except BaseException as exc:  # pragma: no cover - 仅用于传递线程异常
            errors.append(exc)

    worker = threading.Thread(target=evaluate)
    worker.start()
    assert started.wait(timeout=2)
    engine.set_rules([])
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors == []
    assert result == []
    assert seen == []
    assert engine._abnormal_condition_state == {}


def test_rule_change_waits_for_alert_publication(monkeypatch) -> None:
    """规则变更与状态/通知发布线性化，返回后不再出现旧规则事件。"""
    monkeypatch.setattr(monitor_module, "cn_today", lambda: date(2026, 8, 28))
    handler_started = threading.Event()
    release_handler = threading.Event()
    rules_changed = threading.Event()
    seen: list[dict] = []
    errors: list[BaseException] = []

    def blocking_handler(event: dict) -> None:
        seen.append(event)
        handler_started.set()
        assert release_handler.wait(timeout=2)

    engine = MonitorRuleEngine(alert_handler=blocking_handler)
    engine.set_rules([_date_rule("date-old")])

    def evaluate() -> None:
        try:
            engine.evaluate_date_rules(now=1000.0)
        except BaseException as exc:  # pragma: no cover - 仅用于传递线程异常
            errors.append(exc)

    def change_rules() -> None:
        try:
            engine.set_rules([])
            rules_changed.set()
        except BaseException as exc:  # pragma: no cover - 仅用于传递线程异常
            errors.append(exc)

    evaluator = threading.Thread(target=evaluate)
    evaluator.start()
    assert handler_started.wait(timeout=2)
    changer = threading.Thread(target=change_rules)
    changer.start()
    assert not rules_changed.wait(timeout=0.1)
    release_handler.set()
    evaluator.join(timeout=2)
    changer.join(timeout=2)

    assert not evaluator.is_alive() and not changer.is_alive()
    assert errors == []
    assert rules_changed.is_set()
    assert [event["rule_id"] for event in seen] == ["date-old"]
    assert engine.evaluate_date_rules(now=1001.0) == []
