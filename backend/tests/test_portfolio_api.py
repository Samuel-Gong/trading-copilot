"""持仓模块公开 HTTP 契约测试。"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, timedelta
from pathlib import Path
from stat import S_IMODE
from types import SimpleNamespace
from typing import ClassVar

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.portfolio import router
from app.api.stock_analysis import router as stock_analysis_router
from app.api.watchlist import router as watchlist_router
from app.config import settings
from app.services import portfolio as portfolio_service
from app.strategy import monitor_rules


class CaptureMonitorEngine:
    def __init__(self) -> None:
        self.rules: list[dict] = []

    def set_rules(self, rules: list[dict]) -> None:
        self.rules = rules


class FakePortfolioRepo:
    """只替换外部行情仓库边界。持仓业务仍走真实 API 与持久化。"""

    names: ClassVar[dict[str, str]] = {
        "600519.SH": "贵州茅台",
        "000001.SZ": "平安银行",
        "510300.SH": "沪深300ETF",
        "000001.SH": "上证指数",
    }
    asset_types: ClassVar[dict[str, str]] = {
        "600519.SH": "stock",
        "000001.SZ": "stock",
        "510300.SH": "etf",
        "000001.SH": "index",
    }
    prices: ClassVar[dict[str, list[tuple[date, float]]]] = {
        "600519.SH": [(date(2026, 7, 31), 1600.0)],
        "510300.SH": [(date(2026, 7, 30), 4.2)],
    }

    def get_name_map(self, symbols=None):
        if symbols is None:
            return dict(self.names)
        return {symbol: self.names[symbol] for symbol in symbols if symbol in self.names}

    def resolve_asset_type(self, symbol: str) -> str:
        return self.asset_types.get(symbol, "stock")

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        if columns is None and symbol == "600519.SH":
            first = date(2026, 5, 3)
            rows = []
            for offset in range(90):
                close = 1511.0 + offset
                rows.append(
                    {
                        "symbol": symbol,
                        "date": first + timedelta(days=offset),
                        "open": close - 2,
                        "high": close + 10,
                        "low": close - 10,
                        "close": close,
                        "volume": 100000 + offset,
                        "turnover_rate": 1.0,
                    }
                )
            return pl.DataFrame(rows)
        rows = [
            {"symbol": symbol, "date": trade_date, "close": close}
            for trade_date, close in self.prices.get(symbol, [])
            if start <= trade_date <= end
        ]
        if not rows:
            return pl.DataFrame()
        frame = pl.DataFrame(rows)
        if columns:
            frame = frame.select([column for column in columns if column in frame.columns])
        return frame

    def get_daily_close_batch(self, asset_type, symbols, start, end):
        rows = [
            {"symbol": symbol, "date": trade_date, "close": close}
            for symbol in symbols
            for trade_date, close in self.prices.get(symbol, [])
            if start <= trade_date <= end
        ]
        return pl.DataFrame(rows) if rows else pl.DataFrame()


class BatchOnlyPortfolioRepo(FakePortfolioRepo):
    """持仓估值只能走批量价格接口,并记录按资产类型分组的调用。"""

    def __init__(self) -> None:
        self.batch_calls: list[tuple[str, tuple[str, ...]]] = []

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        raise AssertionError("持仓总览不应逐标的读取日 K")

    def get_daily_close_batch(self, asset_type, symbols, start, end):
        self.batch_calls.append((asset_type, tuple(symbols)))
        return super().get_daily_close_batch(asset_type, symbols, start, end)


def make_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    app = FastAPI()
    app.state.repo = FakePortfolioRepo()
    app.state.repo.store = SimpleNamespace(data_dir=tmp_path)
    app.state.monitor_engine = CaptureMonitorEngine()
    app.include_router(router)
    app.include_router(stock_analysis_router)
    app.include_router(watchlist_router)
    return TestClient(app)


def create_account(client: TestClient, name: str) -> dict:
    response = client.post("/api/portfolio/accounts", json={"name": name})
    assert response.status_code == 201
    return response.json()


def upsert_position(
    client: TestClient,
    account_id: str,
    symbol: str,
    *,
    quantity: float,
    average_cost: float,
    note: str = "",
    purchase_date: str | None = None,
) -> dict:
    response = client.post(
        "/api/portfolio/trades",
        json={
            "account_id": account_id,
            "symbol": symbol,
            "trade_date": purchase_date or "2026-07-30",
            "side": "buy",
            "quantity": quantity,
            "price": average_cost,
            # 显式零费用: 建仓辅助函数语义是按声明成本持有, 不触发费率估算
            "fee": 0,
            "tax": 0,
            "note": note,
        },
    )
    assert response.status_code == 201
    return response.json()


def save_symbol_rule(
    tmp_path, *, rule_id: str, symbols: list[str], rule_type: str = "price"
) -> dict:
    conditions = (
        [{"field": "close", "op": "<=", "value": 100}]
        if rule_type == "price"
        else [{"field": "signal_test", "op": "truth"}]
    )
    rule = monitor_rules.normalize(
        {
            "id": rule_id,
            "name": rule_id,
            "type": rule_type,
            "scope": "symbols",
            "symbols": symbols,
            "conditions": conditions,
        }
    )
    monitor_rules.validate(rule)
    monitor_rules.save_one(tmp_path, rule)
    return rule


def test_symbol_rule_cleanup_skips_mismatched_rule_file(tmp_path):
    rule = save_symbol_rule(
        tmp_path, rule_id="declared_rule", symbols=["600519.SH"]
    )
    declared_path = tmp_path / "user_data" / "monitor_rules" / "declared_rule.json"
    mismatched_path = declared_path.with_name("unexpected_file.json")
    declared_path.rename(mismatched_path)

    assert monitor_rules.delete_for_symbols(tmp_path, {"600519.SH"}) == []
    assert json.loads(mismatched_path.read_text(encoding="utf-8"))["id"] == rule["id"]


def test_monitor_rule_atomic_save_keeps_webhook_file_private(tmp_path):
    rule = save_symbol_rule(
        tmp_path, rule_id="private_rule", symbols=["600519.SH"]
    )
    path = tmp_path / "user_data" / "monitor_rules" / "private_rule.json"
    path.chmod(0o600)
    rule["webhook_url"] = "https://example.invalid/synthetic-hook"
    previous_umask = os.umask(0o022)
    try:
        monitor_rules.save_one(tmp_path, rule)
    finally:
        os.umask(previous_umask)

    assert S_IMODE(path.stat().st_mode) == 0o600
    assert monitor_rules.load_one(tmp_path, "private_rule")["webhook_url"] == (
        "https://example.invalid/synthetic-hook"
    )


def test_concurrent_symbol_cleanup_serializes_shared_rule_updates(
    tmp_path, monkeypatch
):
    save_symbol_rule(
        tmp_path,
        rule_id="shared_cleanup",
        symbols=["600519.SH", "000001.SZ"],
    )
    first_save_started = threading.Event()
    release_first_save = threading.Event()
    second_finished = threading.Event()
    errors: list[BaseException] = []
    original_save = monitor_rules.save_one

    def blocking_save(data_dir, rule):
        if threading.current_thread().name == "first-cleanup":
            first_save_started.set()
            if not release_first_save.wait(timeout=2):
                raise TimeoutError("first cleanup was not released")
        original_save(data_dir, rule)

    def cleanup(symbol: str, finished: threading.Event | None = None):
        try:
            monitor_rules.delete_for_symbols(tmp_path, {symbol})
        except BaseException as exc:
            errors.append(exc)
        finally:
            if finished is not None:
                finished.set()

    monkeypatch.setattr(monitor_rules, "save_one", blocking_save)
    first = threading.Thread(
        target=cleanup,
        args=("600519.SH",),
        name="first-cleanup",
    )
    second = threading.Thread(
        target=cleanup,
        args=("000001.SZ", second_finished),
        name="second-cleanup",
    )
    first.start()
    assert first_save_started.wait(timeout=1)
    second.start()
    try:
        assert not second_finished.wait(timeout=0.05)
    finally:
        release_first_save.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert monitor_rules.load_one(tmp_path, "shared_cleanup") is None


def test_price_monitor_save_and_position_cleanup_share_rule_lock(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(client, account["id"], "600519.SH", quantity=100, average_cost=1500)
    first_rule_saved = threading.Event()
    release_save = threading.Event()
    save_finished = threading.Event()
    sell_finished = threading.Event()
    responses = {}
    errors: list[BaseException] = []
    original_save = monitor_rules.save_one

    def blocking_save(data_dir, rule):
        original_save(data_dir, rule)
        if rule["id"] == "pf_stop_600519_sh":
            first_rule_saved.set()
            if not release_save.wait(timeout=2):
                raise TimeoutError("price monitor save was not released")

    def save_price_monitor():
        try:
            responses["save"] = client.put(
                "/api/portfolio/positions/600519.SH/price-monitor",
                json={
                    "name": "贵州茅台",
                    "asset_type": "stock",
                    "stop_loss_price": 1450,
                    "add_position_price": 1500,
                },
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            save_finished.set()

    def close_position():
        try:
            responses["sell"] = client.post(
                "/api/portfolio/trades",
                json={
                    "account_id": account["id"],
                    "symbol": "600519.SH",
                    "trade_date": "2026-07-31",
                    "side": "sell",
                    "quantity": 100,
                    "price": 1600,
                    "fee": 0,
                    "tax": 0,
                },
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            sell_finished.set()

    monkeypatch.setattr(monitor_rules, "save_one", blocking_save)
    save_thread = threading.Thread(target=save_price_monitor, name="save-price-monitor")
    sell_thread = threading.Thread(target=close_position, name="close-position")
    save_thread.start()
    try:
        assert first_rule_saved.wait(timeout=1)
        sell_thread.start()
        assert not sell_finished.wait(timeout=0.05)
    finally:
        release_save.set()
    save_thread.join(timeout=2)
    sell_thread.join(timeout=2)

    assert not save_thread.is_alive()
    assert not sell_thread.is_alive()
    assert save_finished.is_set()
    assert errors == []
    assert responses["save"].status_code == 200
    assert responses["sell"].status_code == 201
    assert monitor_rules.load_all(tmp_path) == []
    assert client.app.state.monitor_engine.rules == []


def test_engine_snapshot_reload_and_position_cleanup_share_rule_lock(
    tmp_path, monkeypatch
):
    from app.api.monitor_rules import sync_engine

    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(client, account["id"], "600519.SH", quantity=100, average_cost=1500)
    rule = save_symbol_rule(
        tmp_path, rule_id="snapshot_target", symbols=["600519.SH"]
    )
    engine = client.app.state.monitor_engine
    engine.set_rules([rule])
    snapshot_loaded = threading.Event()
    release_snapshot = threading.Event()
    sell_finished = threading.Event()
    errors: list[BaseException] = []
    responses = {}
    original_set_rules = engine.set_rules

    def blocking_set_rules(rules):
        if threading.current_thread().name == "stale-sync":
            snapshot_loaded.set()
            if not release_snapshot.wait(timeout=2):
                raise TimeoutError("engine snapshot reload was not released")
        original_set_rules(rules)

    def reload_snapshot():
        try:
            sync_engine(SimpleNamespace(app=client.app))
        except BaseException as exc:
            errors.append(exc)

    def close_position():
        try:
            responses["sell"] = client.post(
                "/api/portfolio/trades",
                json={
                    "account_id": account["id"],
                    "symbol": "600519.SH",
                    "trade_date": "2026-07-31",
                    "side": "sell",
                    "quantity": 100,
                    "price": 1600,
                    "fee": 0,
                    "tax": 0,
                },
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            sell_finished.set()

    monkeypatch.setattr(engine, "set_rules", blocking_set_rules)
    sync_thread = threading.Thread(target=reload_snapshot, name="stale-sync")
    sell_thread = threading.Thread(target=close_position, name="close-position")
    sync_thread.start()
    try:
        assert snapshot_loaded.wait(timeout=1)
        sell_thread.start()
        assert not sell_finished.wait(timeout=0.05)
    finally:
        release_snapshot.set()
    sync_thread.join(timeout=2)
    sell_thread.join(timeout=2)

    assert not sync_thread.is_alive()
    assert not sell_thread.is_alive()
    assert errors == []
    assert responses["sell"].status_code == 201
    assert monitor_rules.load_all(tmp_path) == []
    assert engine.rules == []


def test_watch_pool_item_can_be_added_listed_and_removed(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)

    created = client.post(
        "/api/portfolio/watch-pool",
        json={"symbol": "600519.SH"},
    )

    assert created.status_code == 201
    assert created.json() == {
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "asset_type": "stock",
        "added_at": created.json()["added_at"],
    }
    listed = client.get("/api/portfolio/watch-pool")
    assert listed.status_code == 200
    assert listed.json() == {"items": [created.json()]}

    removed = client.delete("/api/portfolio/watch-pool/600519.SH")
    assert removed.status_code == 200
    assert removed.json() == {"ok": True}
    assert client.get("/api/portfolio/watch-pool").json() == {"items": []}


def test_held_symbol_cannot_be_added_to_watch_pool(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(
        client,
        account["id"],
        "600519.SH",
        quantity=100,
        average_cost=1500,
    )

    response = client.post(
        "/api/portfolio/watch-pool",
        json={"symbol": "600519.SH"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "当前仍持有该标的。不能加入观察池"
    assert client.get("/api/portfolio/watch-pool").json() == {"items": []}


def test_opening_position_removes_symbol_from_watch_pool(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    added = client.post(
        "/api/portfolio/watch-pool",
        json={"symbol": "600519.SH"},
    )
    assert added.status_code == 201

    upsert_position(
        client,
        account["id"],
        "600519.SH",
        quantity=100,
        average_cost=1500,
    )

    assert client.get("/api/portfolio/watch-pool").json() == {"items": []}


def test_watch_pool_item_is_also_added_to_watchlist(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/portfolio/watch-pool",
        json={"symbol": "600519.SH"},
    )

    assert response.status_code == 201
    watchlist = client.get("/api/watchlist").json()["symbols"]
    assert [item["symbol"] for item in watchlist] == ["600519.SH"]


def test_adding_watch_pool_item_preserves_existing_watchlist_metadata(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    original = client.post(
        "/api/watchlist",
        json={"symbol": "600519.SH", "note": "保留这条自选备注"},
    ).json()["symbols"][0]

    response = client.post(
        "/api/portfolio/watch-pool",
        json={"symbol": "600519.SH"},
    )

    assert response.status_code == 201
    saved = client.get("/api/watchlist").json()["symbols"][0]
    assert saved["note"] == "保留这条自选备注"
    assert saved["added_at"] == original["added_at"]


def test_statement_import_opening_position_removes_symbol_from_watch_pool(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    assert client.post(
        "/api/portfolio/watch-pool",
        json={"symbol": "600519.SH"},
    ).status_code == 201

    committed = client.post(
        "/api/portfolio/statement-commit",
        json={
            "account_id": account["id"],
            "items": [
                {
                    "mode": "insert",
                    "symbol": "600519.SH",
                    "trade_date": "2026-07-30",
                    "side": "buy",
                    "quantity": 100,
                    "price": 1500,
                    "fee": 0,
                    "tax": 0,
                }
            ],
        },
    )

    assert committed.status_code == 200
    assert committed.json()["inserted"] == 1
    assert client.get("/api/portfolio/watch-pool").json() == {"items": []}


def test_deleting_closing_trade_removes_symbol_from_watch_pool(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(
        client,
        account["id"],
        "600519.SH",
        quantity=100,
        average_cost=1500,
    )
    closing_trade = client.post(
        "/api/portfolio/trades",
        json={
            "account_id": account["id"],
            "symbol": "600519.SH",
            "trade_date": "2026-07-31",
            "side": "sell",
            "quantity": 100,
            "price": 1600,
            "fee": 0,
            "tax": 0,
        },
    ).json()
    assert client.post(
        "/api/portfolio/watch-pool",
        json={"symbol": "600519.SH"},
    ).status_code == 201

    deleted = client.delete(f"/api/portfolio/trades/{closing_trade['id']}")

    assert deleted.status_code == 200
    assert client.get("/api/portfolio/watch-pool").json() == {"items": []}


def test_same_symbol_trades_are_isolated_between_accounts(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    main = create_account(client, "主账户")
    reserve = create_account(client, "备用账户")

    first = upsert_position(
        client, main["id"], "600519.SH", quantity=100, average_cost=1500, note="核心仓"
    )
    updated = upsert_position(
        client, main["id"], "600519.SH", quantity=80, average_cost=1520, note="降低暴露"
    )
    other = upsert_position(
        client, reserve["id"], "600519.SH", quantity=20, average_cost=1400
    )

    assert updated["id"] != first["id"]
    assert other["id"] != first["id"]

    main_snapshot = client.get(
        "/api/portfolio/snapshot",
        params={"as_of": "2026-08-01", "account_id": main["id"]},
    ).json()
    reserve_snapshot = client.get(
        "/api/portfolio/snapshot",
        params={"as_of": "2026-08-01", "account_id": reserve["id"]},
    ).json()
    assert main_snapshot["accounts"][0]["positions"][0]["quantity"] == 180
    assert reserve_snapshot["accounts"][0]["positions"][0]["quantity"] == 20

    accounts = client.get("/api/portfolio/accounts").json()["items"]
    assert [item["name"] for item in accounts] == ["主账户", "备用账户"]


def test_closing_position_removes_all_symbol_rules_and_syncs_engine(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(client, account["id"], "600519.SH", quantity=100, average_cost=1500)
    target_rules = [
        save_symbol_rule(tmp_path, rule_id="target_price", symbols=["600519.SH"]),
        save_symbol_rule(
            tmp_path,
            rule_id="target_signal",
            symbols=["600519.SH"],
            rule_type="signal",
        ),
    ]
    other_rule = save_symbol_rule(
        tmp_path, rule_id="other_symbol", symbols=["000001.SZ"]
    )
    client.app.state.monitor_engine.set_rules([*target_rules, other_rule])

    response = client.post(
        "/api/portfolio/trades",
        json={
            "account_id": account["id"],
            "symbol": "600519.SH",
            "trade_date": "2026-07-31",
            "side": "sell",
            "quantity": 100,
            "price": 1600,
            "fee": 0,
            "tax": 0,
        },
    )

    assert response.status_code == 201
    assert [rule["id"] for rule in monitor_rules.load_all(tmp_path)] == ["other_symbol"]
    assert [rule["id"] for rule in client.app.state.monitor_engine.rules] == [
        "other_symbol"
    ]


def test_closing_one_symbol_preserves_other_targets_in_shared_rule(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(client, account["id"], "600519.SH", quantity=100, average_cost=1500)
    rule = monitor_rules.normalize(
        {
            "id": "shared_targets",
            "name": "shared_targets",
            "type": "signal",
            "scope": "symbols",
            "symbols": ["600519.SH", "000001.SZ"],
            "conditions": [
                {"field": "signal_intraday_price_above", "op": "truth"}
            ],
            "intraday_price_levels": {
                "600519.SH": 1550,
                "000001.SZ": 12,
            },
        }
    )
    monitor_rules.validate(rule)
    monitor_rules.save_one(tmp_path, rule)
    client.app.state.monitor_engine.set_rules([rule])

    response = client.post(
        "/api/portfolio/trades",
        json={
            "account_id": account["id"],
            "symbol": "600519.SH",
            "trade_date": "2026-07-31",
            "side": "sell",
            "quantity": 100,
            "price": 1600,
            "fee": 0,
            "tax": 0,
        },
    )

    assert response.status_code == 201
    saved = monitor_rules.load_one(tmp_path, "shared_targets")
    assert saved is not None
    assert saved["symbols"] == ["000001.SZ"]
    assert saved["intraday_price_levels"] == {"000001.SZ": 12}
    assert client.app.state.monitor_engine.rules[0]["symbols"] == ["000001.SZ"]
    assert client.app.state.monitor_engine.rules[0]["intraday_price_levels"] == {
        "000001.SZ": 12
    }


def test_trade_mutation_guard_prevents_buy_during_closed_rule_cleanup(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(client, account["id"], "600519.SH", quantity=100, average_cost=1500)
    save_symbol_rule(tmp_path, rule_id="concurrent_reentry", symbols=["600519.SH"])
    original_cleanup = monitor_rules.delete_for_symbols
    buy_started = threading.Event()
    buy_finished = threading.Event()
    buy_errors: list[BaseException] = []
    buy_threads: list[threading.Thread] = []
    blocked_during_cleanup: list[bool] = []

    def buy_again():
        buy_started.set()
        try:
            portfolio_service.record_trade(
                client.app.state.repo,
                account_id=account["id"],
                symbol="600519.SH",
                trade_date=date(2026, 8, 1),
                side="buy",
                quantity=100,
                price=1590,
                fee=0,
                tax=0,
            )
        except BaseException as exc:
            buy_errors.append(exc)
        finally:
            buy_finished.set()

    def cleanup_while_buy_waits(data_dir, symbols):
        thread = threading.Thread(target=buy_again, name="concurrent-buy")
        buy_threads.append(thread)
        thread.start()
        if not buy_started.wait(timeout=1):
            raise TimeoutError("concurrent buy did not start")
        blocked_during_cleanup.append(not buy_finished.wait(timeout=0.05))
        return original_cleanup(data_dir, symbols)

    monkeypatch.setattr(monitor_rules, "delete_for_symbols", cleanup_while_buy_waits)
    response = client.post(
        "/api/portfolio/trades",
        json={
            "account_id": account["id"],
            "symbol": "600519.SH",
            "trade_date": "2026-07-31",
            "side": "sell",
            "quantity": 100,
            "price": 1600,
            "fee": 0,
            "tax": 0,
        },
    )
    for thread in buy_threads:
        thread.join(timeout=2)

    assert response.status_code == 201
    assert blocked_during_cleanup == [True]
    assert all(not thread.is_alive() for thread in buy_threads)
    assert buy_errors == []
    assert portfolio_service.held_symbols() == {"600519.SH"}
    assert monitor_rules.load_one(tmp_path, "concurrent_reentry") is None


def test_partial_rule_cleanup_failure_still_syncs_successful_changes(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(client, account["id"], "600519.SH", quantity=100, average_cost=1500)
    successful = save_symbol_rule(
        tmp_path, rule_id="cleanup_a", symbols=["600519.SH"]
    )
    failed = save_symbol_rule(
        tmp_path, rule_id="cleanup_b", symbols=["600519.SH"]
    )
    client.app.state.monitor_engine.set_rules([successful, failed])
    original_unlink = Path.unlink

    def fail_second_rule(path: Path, missing_ok: bool = False):
        if path.name == "cleanup_b.json":
            raise OSError("simulated rule deletion failure")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_second_rule)
    response = client.post(
        "/api/portfolio/trades",
        json={
            "account_id": account["id"],
            "symbol": "600519.SH",
            "trade_date": "2026-07-31",
            "side": "sell",
            "quantity": 100,
            "price": 1600,
            "fee": 0,
            "tax": 0,
        },
    )

    assert response.status_code == 201
    assert [rule["id"] for rule in monitor_rules.load_all(tmp_path)] == ["cleanup_b"]
    assert [rule["id"] for rule in client.app.state.monitor_engine.rules] == [
        "cleanup_b"
    ]


def test_partial_or_cross_account_sale_preserves_symbol_rules(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    main = create_account(client, "主账户")
    reserve = create_account(client, "备用账户")
    upsert_position(client, main["id"], "600519.SH", quantity=100, average_cost=1500)
    upsert_position(client, reserve["id"], "600519.SH", quantity=20, average_cost=1400)
    rule = save_symbol_rule(tmp_path, rule_id="shared_holding", symbols=["600519.SH"])
    client.app.state.monitor_engine.set_rules([rule])

    partial = client.post(
        "/api/portfolio/trades",
        json={
            "account_id": main["id"],
            "symbol": "600519.SH",
            "trade_date": "2026-07-31",
            "side": "sell",
            "quantity": 50,
            "price": 1600,
            "fee": 0,
            "tax": 0,
        },
    )
    closed_main = client.post(
        "/api/portfolio/trades",
        json={
            "account_id": main["id"],
            "symbol": "600519.SH",
            "trade_date": "2026-08-01",
            "side": "sell",
            "quantity": 50,
            "price": 1610,
            "fee": 0,
            "tax": 0,
        },
    )

    assert partial.status_code == 201
    assert closed_main.status_code == 201
    assert monitor_rules.load_one(tmp_path, "shared_holding") is not None
    assert [saved["id"] for saved in client.app.state.monitor_engine.rules] == [
        "shared_holding"
    ]


def test_closing_position_without_rules_is_safe(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(client, account["id"], "600519.SH", quantity=100, average_cost=1500)

    response = client.post(
        "/api/portfolio/trades",
        json={
            "account_id": account["id"],
            "symbol": "600519.SH",
            "trade_date": "2026-07-31",
            "side": "sell",
            "quantity": 100,
            "price": 1600,
            "fee": 0,
            "tax": 0,
        },
    )

    assert response.status_code == 201
    assert monitor_rules.load_all(tmp_path) == []
    assert client.app.state.monitor_engine.rules == []


def test_statement_closing_position_removes_symbol_rules(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(client, account["id"], "600519.SH", quantity=100, average_cost=1500)
    save_symbol_rule(tmp_path, rule_id="statement_target", symbols=["600519.SH"])

    response = client.post(
        "/api/portfolio/statement-commit",
        json={
            "account_id": account["id"],
            "items": [
                {
                    "mode": "insert",
                    "symbol": "600519.SH",
                    "trade_date": "2026-07-31",
                    "side": "sell",
                    "quantity": 100,
                    "price": 1600,
                    "fee": 0,
                    "tax": 0,
                }
            ],
        },
    )

    assert response.status_code == 200
    assert monitor_rules.load_all(tmp_path) == []
    assert client.app.state.monitor_engine.rules == []


def test_statement_cleanup_failure_does_not_report_persisted_trade_as_failed(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(client, account["id"], "600519.SH", quantity=100, average_cost=1500)
    save_symbol_rule(tmp_path, rule_id="statement_target", symbols=["600519.SH"])

    def fail_cleanup(_data_dir, _symbols):
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr(monitor_rules, "delete_for_symbols", fail_cleanup)
    response = client.post(
        "/api/portfolio/statement-commit",
        json={
            "account_id": account["id"],
            "items": [
                {
                    "mode": "insert",
                    "symbol": "600519.SH",
                    "trade_date": "2026-07-31",
                    "side": "sell",
                    "quantity": 100,
                    "price": 1600,
                    "fee": 0,
                    "tax": 0,
                }
            ],
        },
    )

    assert response.status_code == 200
    trades = client.get(
        "/api/portfolio/trades", params={"account_id": account["id"]}
    ).json()["items"]
    assert len(trades) == 2
    assert [trade["side"] for trade in trades] == ["sell", "buy"]


def test_statement_engine_sync_failure_returns_success_and_recovers_runtime_rules(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(client, account["id"], "600519.SH", quantity=100, average_cost=1500)
    rule = save_symbol_rule(
        tmp_path, rule_id="statement_target", symbols=["600519.SH"]
    )
    engine = client.app.state.monitor_engine
    engine.set_rules([rule])
    original_set_rules = engine.set_rules
    sync_recovered = threading.Event()
    sync_attempts = 0

    def fail_sync_once(rules):
        nonlocal sync_attempts
        sync_attempts += 1
        if sync_attempts == 1:
            raise ValueError("simulated engine sync failure")
        original_set_rules(rules)
        sync_recovered.set()

    from app.api import portfolio as portfolio_api

    monkeypatch.setattr(
        portfolio_api, "_MONITOR_ENGINE_SYNC_RETRY_INITIAL_SECONDS", 0.01
    )
    monkeypatch.setattr(engine, "set_rules", fail_sync_once)
    response = client.post(
        "/api/portfolio/statement-commit",
        json={
            "account_id": account["id"],
            "items": [
                {
                    "mode": "insert",
                    "symbol": "600519.SH",
                    "trade_date": "2026-07-31",
                    "side": "sell",
                    "quantity": 100,
                    "price": 1600,
                    "fee": 0,
                    "tax": 0,
                }
            ],
        },
    )

    assert response.status_code == 200
    trades = client.get(
        "/api/portfolio/trades", params={"account_id": account["id"]}
    ).json()["items"]
    assert len(trades) == 2
    assert monitor_rules.load_all(tmp_path) == []
    assert sync_recovered.wait(timeout=1)
    assert engine.rules == []
    assert sync_attempts == 2


def test_monitor_engine_sync_retry_hands_off_new_generation_atomically(
    tmp_path, monkeypatch
):
    from app.api import portfolio as portfolio_api

    client = make_client(tmp_path, monkeypatch)
    request = SimpleNamespace(app=client.app)
    state = portfolio_api._monitor_engine_sync_retry_state(request)
    handoff_window = threading.Event()
    release_first_worker = threading.Event()
    second_sync_completed = threading.Event()
    sync_attempts = 0

    class HandoffLock:
        def __init__(self, lock):
            self._lock = lock
            self._worker_exits = 0

        def __enter__(self):
            self._lock.acquire()
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            should_block = False
            if threading.current_thread().name == "monitor-engine-sync-retry":
                self._worker_exits += 1
                should_block = self._worker_exits == 2
            self._lock.release()
            if should_block:
                handoff_window.set()
                if not release_first_worker.wait(timeout=2):
                    raise TimeoutError("retry worker handoff was not released")

    def capture_sync(rules):
        nonlocal sync_attempts
        sync_attempts += 1
        client.app.state.monitor_engine.rules = rules
        if sync_attempts >= 2:
            second_sync_completed.set()

    state.lock = HandoffLock(state.lock)
    monkeypatch.setattr(
        portfolio_api, "_MONITOR_ENGINE_SYNC_RETRY_INITIAL_SECONDS", 0.01
    )
    monkeypatch.setattr(client.app.state.monitor_engine, "set_rules", capture_sync)
    try:
        portfolio_api._schedule_monitor_engine_sync_retry(request)
        assert handoff_window.wait(timeout=1)
        portfolio_api._schedule_monitor_engine_sync_retry(request)
        release_first_worker.set()
        assert second_sync_completed.wait(timeout=1)
        assert sync_attempts == 2
    finally:
        release_first_worker.set()
        portfolio_api.stop_monitor_engine_sync_retry(client.app)


def test_monitor_engine_sync_retry_stops_on_app_shutdown(tmp_path, monkeypatch):
    from app.api import portfolio as portfolio_api

    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(client, account["id"], "600519.SH", quantity=100, average_cost=1500)
    rule = save_symbol_rule(
        tmp_path, rule_id="shutdown_target", symbols=["600519.SH"]
    )
    engine = client.app.state.monitor_engine
    engine.set_rules([rule])
    retry_attempted = threading.Event()
    sync_attempts = 0

    def fail_sync(_rules):
        nonlocal sync_attempts
        sync_attempts += 1
        if sync_attempts >= 2:
            retry_attempted.set()
        raise ValueError("simulated persistent engine sync failure")

    monkeypatch.setattr(
        portfolio_api, "_MONITOR_ENGINE_SYNC_RETRY_INITIAL_SECONDS", 0.01
    )
    monkeypatch.setattr(engine, "set_rules", fail_sync)
    response = client.post(
        "/api/portfolio/trades",
        json={
            "account_id": account["id"],
            "symbol": "600519.SH",
            "trade_date": "2026-07-31",
            "side": "sell",
            "quantity": 100,
            "price": 1600,
            "fee": 0,
            "tax": 0,
        },
    )

    try:
        assert response.status_code == 201
        assert retry_attempted.wait(timeout=1)
        state = getattr(
            client.app.state,
            portfolio_api._MONITOR_ENGINE_SYNC_RETRY_STATE_ATTR,
        )
        worker = state.worker
        assert worker is not None
        portfolio_api.stop_monitor_engine_sync_retry(client.app)
        assert state.stop_event.is_set()
        assert not worker.is_alive()
        assert state.worker is None
        portfolio_api.reset_monitor_engine_sync_retry(client.app)
        restarted_state = getattr(
            client.app.state,
            portfolio_api._MONITOR_ENGINE_SYNC_RETRY_STATE_ATTR,
        )
        assert restarted_state is not state
        assert not restarted_state.stop_event.is_set()
    finally:
        portfolio_api.stop_monitor_engine_sync_retry(client.app)


def test_closing_position_sync_skips_invalid_unrelated_rule(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(client, account["id"], "600519.SH", quantity=100, average_cost=1500)
    rule = save_symbol_rule(
        tmp_path, rule_id="statement_target", symbols=["600519.SH"]
    )
    engine = client.app.state.monitor_engine
    engine.set_rules([rule])
    invalid_path = tmp_path / "user_data" / "monitor_rules" / "invalid_rule.json"
    invalid_path.write_text(
        json.dumps(
            {
                "name": "缺少 ID 的规则",
                "type": "price",
                "scope": "symbols",
                "symbols": ["000001.SZ"],
                "conditions": [{"field": "close", "op": "<=", "value": 10}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = client.post(
        "/api/portfolio/trades",
        json={
            "account_id": account["id"],
            "symbol": "600519.SH",
            "trade_date": "2026-07-31",
            "side": "sell",
            "quantity": 100,
            "price": 1600,
            "fee": 0,
            "tax": 0,
        },
    )

    assert response.status_code == 201
    assert invalid_path.exists()
    assert monitor_rules.load_one(tmp_path, "statement_target") is None
    assert engine.rules == []


def test_importing_closed_historical_roundtrip_preserves_existing_rule(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    save_symbol_rule(tmp_path, rule_id="watching_only", symbols=["600519.SH"])

    response = client.post(
        "/api/portfolio/statement-commit",
        json={
            "account_id": account["id"],
            "items": [
                {
                    "mode": "insert",
                    "symbol": "600519.SH",
                    "trade_date": "2026-07-01",
                    "side": "buy",
                    "quantity": 100,
                    "price": 1500,
                    "fee": 0,
                    "tax": 0,
                },
                {
                    "mode": "insert",
                    "symbol": "600519.SH",
                    "trade_date": "2026-07-31",
                    "side": "sell",
                    "quantity": 100,
                    "price": 1600,
                    "fee": 0,
                    "tax": 0,
                },
            ],
        },
    )

    assert response.status_code == 200
    assert monitor_rules.load_one(tmp_path, "watching_only") is not None


def test_trade_quantity_edit_that_closes_position_removes_symbol_rules(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(client, account["id"], "600519.SH", quantity=100, average_cost=1500)
    sale = client.post(
        "/api/portfolio/trades",
        json={
            "account_id": account["id"],
            "symbol": "600519.SH",
            "trade_date": "2026-07-31",
            "side": "sell",
            "quantity": 50,
            "price": 1600,
            "fee": 0,
            "tax": 0,
        },
    ).json()
    save_symbol_rule(tmp_path, rule_id="edited_sale", symbols=["600519.SH"])

    response = client.patch(
        f"/api/portfolio/trades/{sale['id']}",
        json={"quantity": 100, "price": 1600},
    )

    assert response.status_code == 200
    assert monitor_rules.load_all(tmp_path) == []
    assert client.app.state.monitor_engine.rules == []


def test_deleting_only_buy_trade_removes_symbol_rules(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    purchase = upsert_position(
        client, account["id"], "600519.SH", quantity=100, average_cost=1500
    )
    save_symbol_rule(tmp_path, rule_id="deleted_purchase", symbols=["600519.SH"])

    response = client.delete(f"/api/portfolio/trades/{purchase['id']}")

    assert response.status_code == 200
    assert monitor_rules.load_all(tmp_path) == []
    assert client.app.state.monitor_engine.rules == []


def test_snapshot_batches_prices_by_asset_type_and_reuses_cross_account_symbol(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    main = create_account(client, "主账户")
    reserve = create_account(client, "备用账户")
    upsert_position(client, main["id"], "600519.SH", quantity=100, average_cost=1500)
    upsert_position(client, reserve["id"], "600519.SH", quantity=20, average_cost=1400)
    upsert_position(client, main["id"], "510300.SH", quantity=1000, average_cost=4)

    repo = BatchOnlyPortfolioRepo()
    client.app.state.repo = repo
    response = client.get("/api/portfolio/snapshot", params={"as_of": "2026-08-01"})

    assert response.status_code == 200
    assert sorted(repo.batch_calls) == [
        ("etf", ("510300.SH",)),
        ("stock", ("600519.SH",)),
    ]
    snapshot = response.json()
    assert snapshot["position_count"] == 3
    assert snapshot["priced_position_count"] == 3


def test_oldest_remaining_buy_date_is_exposed_in_snapshot(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")

    created = upsert_position(
        client,
        account["id"],
        "600519.SH",
        quantity=100,
        average_cost=1500,
        purchase_date="2026-07-30",
    )
    updated = upsert_position(
        client,
        account["id"],
        "600519.SH",
        quantity=80,
        average_cost=1520,
    )
    snapshot = client.get(
        "/api/portfolio/snapshot", params={"as_of": "2026-08-01"}
    ).json()

    assert created["trade_date"] == "2026-07-30"
    assert updated["trade_date"] == "2026-07-30"
    assert snapshot["accounts"][0]["positions"][0]["purchase_date"] == "2026-07-30"


def test_snapshot_uses_latest_close_on_or_before_business_date(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(client, account["id"], "600519.SH", quantity=100, average_cost=1500)

    response = client.get("/api/portfolio/snapshot", params={"as_of": "2026-08-01"})
    assert response.status_code == 200
    snapshot = response.json()

    assert snapshot["as_of"] == "2026-08-01"
    assert snapshot["total_cost"] == 150000.0
    assert snapshot["market_value"] == 160000.0
    assert snapshot["unrealized_pnl"] == 10000.0
    assert snapshot["unrealized_return_ratio"] == 0.066667
    position = snapshot["accounts"][0]["positions"][0]
    assert position["current_price"] == 1600.0
    assert position["price_date"] == "2026-07-31"
    assert position["price_available"] is True
    assert position["price_stale"] is True


def test_snapshot_exact_price_date_does_not_fall_back_to_earlier_close(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(client, account["id"], "600519.SH", quantity=100, average_cost=1500)

    snapshot = portfolio_service.get_snapshot(
        client.app.state.repo,
        date(2026, 8, 1),
        exact_price_date=True,
    )
    position = snapshot["accounts"][0]["positions"][0]

    assert position["current_price"] is None
    assert position["price_date"] is None
    assert position["price_available"] is False


def test_missing_price_stays_visible_and_is_excluded_from_valuation(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(client, account["id"], "000001.SZ", quantity=1000, average_cost=10)

    snapshot = client.get(
        "/api/portfolio/snapshot", params={"as_of": "2026-08-01"}
    ).json()
    position = snapshot["accounts"][0]["positions"][0]

    assert position["current_price"] is None
    assert position["market_value"] is None
    assert position["unrealized_pnl"] is None
    assert position["price_available"] is False
    assert snapshot["total_cost"] == 10000.0
    assert snapshot["market_value"] == 0.0
    assert snapshot["unrealized_pnl"] == 0.0
    assert snapshot["priced_position_count"] == 0
    assert snapshot["missing_price_count"] == 1


def test_non_empty_account_delete_fails_closed_and_index_position_is_rejected(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(client, account["id"], "600519.SH", quantity=100, average_cost=1500)

    blocked = client.delete(f"/api/portfolio/accounts/{account['id']}")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "账户仍有交易记录。请先删除交易"

    unsupported = client.post(
        "/api/portfolio/trades",
        json={
            "account_id": account["id"],
            "symbol": "000001.SH",
            "trade_date": "2026-07-30",
            "side": "buy",
            "quantity": 1,
            "price": 3000,
        },
    )
    assert unsupported.status_code == 400
    assert unsupported.json()["detail"] == "交易记录只支持 A 股和场内 ETF"


def test_position_rejects_non_finite_quantity(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")

    response = client.post(
        "/api/portfolio/trades",
        content=(
            f'{{"account_id":"{account["id"]}","symbol":"600519.SH",'
            '"trade_date":"2026-07-30","side":"buy","quantity":1e999,"price":1500}'
        ),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422


def test_unexpected_repository_error_is_not_exposed(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)

    def fail_with_sensitive_detail(*args, **kwargs):
        raise RuntimeError("/private/config/provider-secret.json")

    monkeypatch.setattr(
        client.app.state.repo,
        "get_daily_close_batch",
        fail_with_sensitive_detail,
    )
    account = create_account(client, "主账户")
    upsert_position(client, account["id"], "600519.SH", quantity=100, average_cost=1500)

    response = client.get("/api/portfolio/snapshot", params={"as_of": "2026-08-01"})

    assert response.status_code == 500
    assert response.json()["detail"] == "持仓操作失败"


def test_position_analysis_reuses_stock_ndjson_and_keeps_context_inside_provider(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    upsert_position(
        client,
        account["id"],
        "600519.SH",
        quantity=100,
        average_cost=1500,
        purchase_date="2026-07-30",
        note="只观察量价风险",
    )
    captured: dict = {}

    async def fake_stream_ai_text(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        yield "# 客观持仓分析\n\n仅描述技术状态。"

    monkeypatch.setattr("app.services.ai_provider.stream_ai_text", fake_stream_ai_text)
    response = client.post(
        f"/api/portfolio/accounts/{account['id']}/positions/600519.SH/analyze",
        json={"focus": "关注量价背离"},
    )

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert [event["type"] for event in events] == ["meta", "delta", "done"]
    assert events[1]["content"].startswith("# 客观持仓分析")
    assert all("portfolio_context" not in event for event in events)
    assert account["id"] not in response.text
    assert "绝对不输出" in captured["messages"][0]["content"]
    prompt = captured["messages"][1]["content"]
    assert '"quantity": 100.0' in prompt
    assert '"average_cost": 1500.0' in prompt
    assert '"purchase_date": "2026-07-30"' in prompt
    assert "只观察量价风险" in prompt


def test_position_analysis_missing_holding_fails_before_stream_creation(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")

    response = client.post(
        f"/api/portfolio/accounts/{account['id']}/positions/600519.SH/analyze",
        json={"focus": ""},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "持仓不存在"


def test_portfolio_report_persists_only_low_sensitivity_source_reference(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "主账户")
    source_ref = f"{account['id']}:600519.SH"

    saved = client.post(
        "/api/stock-analysis/reports",
        json={
            "symbol": "600519.SH",
            "name": "贵州茅台",
            "focus": "关注量价背离",
            "content": "# 客观报告",
            "source": "portfolio",
            "source_ref": source_ref,
            "portfolio_context": {"quantity": 100, "average_cost": 1500},
        },
    )

    assert saved.status_code == 200
    report = saved.json()["report"]
    assert report["source"] == "portfolio"
    assert report["source_ref"] == source_ref
    assert "portfolio_context" not in report


def test_public_report_api_rejects_forged_daily_review_provenance(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/stock-analysis/reports",
        json={
            "symbol": "600519.SH",
            "name": "贵州茅台",
            "content": "# 伪造复盘报告",
            "source": "daily_review",
            "source_ref": "forged-account:600519.SH",
            "daily_review_date": "2026-08-01",
        },
    )

    assert response.status_code == 422
