"""交割单导入(费率估算 + 校准 + 幂等)的公开 HTTP 契约。"""
from __future__ import annotations

from datetime import date
from typing import ClassVar

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.portfolio import router
from app.config import settings

_STATEMENT_CSV = """成交日期,证券代码,证券名称,买卖方向,成交数量,成交均价,成交金额,佣金,印花税,过户费
2026-07-01,600519,贵州茅台,买入,100,10,1000,5,0,0.01
2026-07-05,600519,贵州茅台,卖出,100,15,1500,5,0.75,0.02
"""


class FakeRepo:
    names: ClassVar[dict[str, str]] = {
        "600519.SH": "贵州茅台",
        "000001.SZ": "平安银行",
        "510300.SH": "沪深300ETF",
    }
    prices: ClassVar[dict[str, list[tuple[date, float]]]] = {
        "600519.SH": [(date(2026, 7, 31), 15.0)],
    }

    def get_name_map(self, symbols=None):
        symbols = symbols or self.names
        return {symbol: self.names[symbol] for symbol in symbols if symbol in self.names}

    def resolve_asset_type(self, symbol: str) -> str:
        return "etf" if symbol == "510300.SH" else "stock"

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        rows = [
            {"symbol": symbol, "date": trade_date, "close": close}
            for trade_date, close in self.prices.get(symbol, [])
            if start <= trade_date <= end
        ]
        if not rows:
            return pl.DataFrame()
        frame = pl.DataFrame(rows)
        return frame.select([name for name in columns or frame.columns if name in frame.columns])

    def get_daily_close_batch(self, asset_type, symbols, start, end):
        rows = [
            {"symbol": symbol, "date": trade_date, "close": close}
            for symbol in symbols
            for trade_date, close in self.prices.get(symbol, [])
            if start <= trade_date <= end
        ]
        return pl.DataFrame(rows) if rows else pl.DataFrame()


def make_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    instruments = tmp_path / "instruments"
    instruments.mkdir()
    pl.DataFrame(
        {
            "code": ["600519", "000001"],
            "symbol": ["600519.SH", "000001.SZ"],
            "name": ["贵州茅台", "平安银行"],
        }
    ).write_parquet(instruments / "instruments.parquet")
    etf_dir = tmp_path / "instruments_etf"
    etf_dir.mkdir()
    pl.DataFrame(
        {"code": ["510300"], "symbol": ["510300.SH"], "name": ["沪深300ETF"]}
    ).write_parquet(etf_dir / "etf.parquet")
    app = FastAPI()
    app.state.repo = FakeRepo()
    app.include_router(router)
    return TestClient(app)


def create_account(client: TestClient, name: str = "主账户") -> dict:
    response = client.post("/api/portfolio/accounts", json={"name": name})
    assert response.status_code == 201
    return response.json()


def record_trade(client: TestClient, account_id: str, **payload) -> dict:
    body = {"account_id": account_id, "symbol": "600519.SH", **payload}
    response = client.post("/api/portfolio/trades", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def upload_statement(client: TestClient, account_id: str, content: bytes, filename="statement.csv"):
    return client.post(
        "/api/portfolio/statement-preview",
        files={"file": (filename, content, "text/csv")},
        data={"account_id": account_id},
    )


def test_estimate_endpoint_returns_profile(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    response = client.post(
        "/api/portfolio/trades/estimate",
        json={"symbol": "600519.SH", "side": "sell", "quantity": 100, "price": 1000},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["fee"] == pytest.approx(26.0)
    assert data["tax"] == pytest.approx(50.0)
    assert data["profile"]["commission_rate"] == pytest.approx(0.00025)


def test_blank_costs_are_estimated_and_explicit_zero_is_manual(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    estimated = record_trade(
        client,
        account["id"],
        trade_date="2026-07-01",
        side="buy",
        quantity=100,
        price=1000,
    )
    assert estimated["fee"] == pytest.approx(26.0)
    assert estimated["tax"] == 0.0
    assert estimated["cost_source"] == "estimated"

    manual = record_trade(
        client,
        account["id"],
        trade_date="2026-07-02",
        side="buy",
        quantity=100,
        price=1000,
        fee=0,
        tax=0,
    )
    assert manual["fee"] == 0.0
    assert manual["cost_source"] == "manual"


def test_statement_import_insert_then_preview_is_idempotent(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)

    preview = upload_statement(client, account["id"], _STATEMENT_CSV.encode("utf-8"))
    assert preview.status_code == 200, preview.text
    items = preview.json()["items"]
    assert [item["mode"] for item in items] == ["insert", "insert"]
    assert items[0]["fee"] == pytest.approx(5.01)
    assert items[1]["fee"] == pytest.approx(5.02)
    assert items[1]["tax"] == pytest.approx(0.75)

    commit = client.post(
        "/api/portfolio/statement-commit",
        json={"account_id": account["id"], "items": items},
    )
    assert commit.status_code == 200, commit.text
    assert commit.json() == {"inserted": 2, "calibrated": 0, "skipped": 0}

    trades = client.get("/api/portfolio/trades", params={"account_id": account["id"]}).json()["items"]
    assert len(trades) == 2
    assert all(trade["cost_source"] == "imported" for trade in trades)

    second = upload_statement(client, account["id"], _STATEMENT_CSV.encode("utf-8"))
    assert [item["mode"] for item in second.json()["items"]] == ["skip", "skip"]


def test_statement_calibrates_estimated_costs(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    record_trade(client, account["id"], trade_date="2026-07-01", side="buy", quantity=100, price=10)
    record_trade(client, account["id"], trade_date="2026-07-05", side="sell", quantity=100, price=15)

    preview = upload_statement(client, account["id"], _STATEMENT_CSV.encode("utf-8"))
    assert preview.status_code == 200, preview.text
    items = preview.json()["items"]
    assert [item["mode"] for item in items] == ["skip", "calibrate"]
    calibrated_row = items[1]
    assert calibrated_row["current_fee"] == pytest.approx(5.01)
    assert calibrated_row["current_cost_source"] == "estimated"
    assert calibrated_row["fee"] == pytest.approx(5.02)

    commit = client.post(
        "/api/portfolio/statement-commit",
        json={
            "account_id": account["id"],
            "items": [item for item in items if item["mode"] != "skip"],
        },
    )
    assert commit.status_code == 200, commit.text
    assert commit.json() == {"inserted": 0, "calibrated": 1, "skipped": 0}

    trades = client.get("/api/portfolio/trades", params={"account_id": account["id"]}).json()["items"]
    sell = next(trade for trade in trades if trade["side"] == "sell")
    assert sell["fee"] == pytest.approx(5.02)
    assert sell["tax"] == pytest.approx(0.75)
    assert sell["cost_source"] == "calibrated"
    buy = next(trade for trade in trades if trade["side"] == "buy")
    assert buy["fee"] == pytest.approx(5.01)
    assert buy["cost_source"] == "estimated"

    snapshot = client.get(
        "/api/portfolio/snapshot", params={"account_id": account["id"]}
    ).json()
    assert snapshot["total_fee"] == pytest.approx(10.03)
    assert snapshot["total_tax"] == pytest.approx(0.75)


def test_statement_matching_consumes_duplicate_trades_one_by_one(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    record_trade(
        client,
        account["id"],
        trade_date="2026-07-01",
        side="buy",
        quantity=300,
        price=10,
        fee=0,
        tax=0,
    )
    for _ in range(2):
        record_trade(
            client,
            account["id"],
            trade_date="2026-07-05",
            side="sell",
            quantity=100,
            price=15,
            fee=0,
            tax=0,
        )

    csv_text = """成交日期,证券代码,证券名称,买卖方向,成交数量,成交均价,成交金额,佣金,印花税,过户费
2026-07-05,600519,贵州茅台,卖出,100,15,1500,5,0.75,0.02
"""
    preview = upload_statement(client, account["id"], csv_text.encode("utf-8"))
    items = preview.json()["items"]
    assert [item["mode"] for item in items] == ["calibrate"]

    commit = client.post("/api/portfolio/statement-commit", json={"account_id": account["id"], "items": items})
    assert commit.json() == {"inserted": 0, "calibrated": 1, "skipped": 0}

    trades = client.get("/api/portfolio/trades", params={"account_id": account["id"]}).json()["items"]
    sell_sources = sorted(trade["cost_source"] for trade in trades if trade["side"] == "sell")
    assert sell_sources == ["calibrated", "manual"]


def test_statement_commit_conflict_is_atomic(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    csv_text = """成交日期,证券代码,证券名称,买卖方向,成交数量,成交均价,成交金额,佣金,印花税,过户费
2026-07-05,600519,贵州茅台,卖出,100,15,1500,5,0.75,0.02
"""
    preview = upload_statement(client, account["id"], csv_text.encode("utf-8"))
    items = preview.json()["items"]
    commit = client.post("/api/portfolio/statement-commit", json={"account_id": account["id"], "items": items})
    assert commit.status_code == 409
    trades = client.get("/api/portfolio/trades", params={"account_id": account["id"]}).json()["items"]
    assert trades == []


def test_statement_preview_rejects_unknown_columns(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    response = upload_statement(client, account["id"], b"a,b,c\n1,2,3\n")
    assert response.status_code == 400


def test_statement_preview_reads_gbk_csv(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    preview = upload_statement(client, account["id"], _STATEMENT_CSV.encode("gbk"))
    assert preview.status_code == 200, preview.text
    assert [item["mode"] for item in preview.json()["items"]] == ["insert", "insert"]


def test_statement_rejects_future_trade_date(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    csv_text = """成交日期,证券代码,证券名称,买卖方向,成交数量,成交均价,成交金额,佣金,印花税,过户费
2099-01-01,600519,贵州茅台,买入,100,10,1000,5,0,0.01
"""
    preview = upload_statement(client, account["id"], csv_text.encode("utf-8"))
    commit = client.post(
        "/api/portfolio/statement-commit",
        json={"account_id": account["id"], "items": preview.json()["items"]},
    )
    assert commit.status_code == 400
