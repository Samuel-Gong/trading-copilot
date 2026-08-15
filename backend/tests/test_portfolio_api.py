"""持仓模块公开 HTTP 契约测试。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import ClassVar

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.portfolio import router
from app.api.stock_analysis import router as stock_analysis_router
from app.api.watchlist import router as watchlist_router
from app.config import settings
from app.services import portfolio as portfolio_service


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
