"""交易流水驱动持仓的公开 HTTP 契约。"""
from __future__ import annotations

import json
from datetime import date
from typing import ClassVar

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.portfolio import router
from app.config import settings


class FakeRepo:
    names: ClassVar[dict[str, str]] = {
        "600519.SH": "贵州茅台",
        "000001.SZ": "平安银行",
        "000001.SH": "上证指数",
    }
    prices: ClassVar[dict[str, list[tuple[date, float]]]] = {
        "600519.SH": [
            (date(2026, 7, 5), 11.0),
            (date(2026, 7, 31), 25.0),
            (date(2026, 8, 1), 26.0),
        ],
    }

    def get_name_map(self, symbols=None):
        symbols = symbols or self.names
        return {symbol: self.names[symbol] for symbol in symbols if symbol in self.names}

    def resolve_asset_type(self, symbol: str) -> str:
        return "index" if symbol == "000001.SH" else "stock"

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


def make_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    app = FastAPI()
    app.state.repo = FakeRepo()
    app.include_router(router)
    return TestClient(app)


def create_account(client: TestClient, name: str = "主账户") -> dict:
    response = client.post("/api/portfolio/accounts", json={"name": name})
    assert response.status_code == 201
    return response.json()


def record_trade(
    client: TestClient,
    account_id: str,
    *,
    trade_date: str,
    side: str,
    quantity: float,
    price: float,
    symbol: str = "600519.SH",
    fee: float = 0,
    tax: float = 0,
    note: str = "",
) -> dict:
    response = client.post(
        "/api/portfolio/trades",
        json={
            "account_id": account_id,
            "symbol": symbol,
            "trade_date": trade_date,
            "side": side,
            "quantity": quantity,
            "price": price,
            "fee": fee,
            "tax": tax,
            "note": note,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_snapshot_replays_only_trades_on_or_before_as_of_with_fifo_cost(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    record_trade(
        client,
        account["id"],
        trade_date="2026-07-01",
        side="buy",
        quantity=100,
        price=10,
        fee=10,
    )
    record_trade(
        client,
        account["id"],
        trade_date="2026-07-10",
        side="buy",
        quantity=100,
        price=20,
        fee=20,
    )
    sell = record_trade(
        client,
        account["id"],
        trade_date="2026-07-20",
        side="sell",
        quantity=120,
        price=30,
        fee=12,
        tax=3,
        note="分批卖出",
    )
    record_trade(
        client,
        account["id"],
        trade_date="2026-08-01",
        side="buy",
        quantity=10,
        price=24,
    )

    early = client.get("/api/portfolio/snapshot", params={"as_of": "2026-07-05"}).json()
    assert early["position_count"] == 1
    assert early["accounts"][0]["positions"][0]["quantity"] == 100
    assert early["accounts"][0]["positions"][0]["average_cost"] == 10.1

    historical = client.get(
        "/api/portfolio/snapshot", params={"as_of": "2026-07-31"}
    ).json()
    position = historical["accounts"][0]["positions"][0]
    assert position["quantity"] == 80
    assert position["average_cost"] == 20.2
    assert position["total_cost"] == 1616.0
    assert position["purchase_date"] == "2026-07-10"
    assert position["current_price"] == 25.0
    assert historical["realized_pnl"] == 2171.0
    assert historical["trade_count"] == 3
    assert sell["note"] == "分批卖出"

    latest = client.get("/api/portfolio/snapshot", params={"as_of": "2026-08-01"}).json()
    assert latest["accounts"][0]["positions"][0]["quantity"] == 90
    assert latest["trade_count"] == 4


def test_future_trade_is_not_present_in_historical_daily_review_scope(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    record_trade(
        client,
        account["id"],
        trade_date="2026-08-01",
        side="buy",
        quantity=100,
        price=1500,
    )

    snapshot = client.get(
        "/api/portfolio/snapshot", params={"as_of": "2026-07-31"}
    ).json()

    assert snapshot["position_count"] == 0
    assert snapshot["trade_count"] == 0


def test_oversell_and_deleting_a_buy_needed_by_later_sell_fail_closed(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    buy = record_trade(
        client,
        account["id"],
        trade_date="2026-07-01",
        side="buy",
        quantity=100,
        price=10,
    )

    oversell = client.post(
        "/api/portfolio/trades",
        json={
            "account_id": account["id"],
            "symbol": "600519.SH",
            "trade_date": "2026-07-02",
            "side": "sell",
            "quantity": 101,
            "price": 11,
        },
    )
    assert oversell.status_code == 409
    assert "可卖数量" in oversell.json()["detail"]

    record_trade(
        client,
        account["id"],
        trade_date="2026-07-02",
        side="sell",
        quantity=80,
        price=11,
    )
    blocked_delete = client.delete(f"/api/portfolio/trades/{buy['id']}")
    assert blocked_delete.status_code == 409
    assert "后续卖出" in blocked_delete.json()["detail"]


def test_trade_list_filters_and_trade_delete_rebuilds_position(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    main = create_account(client, "主账户")
    reserve = create_account(client, "备用账户")
    first = record_trade(
        client,
        main["id"],
        trade_date="2026-07-01",
        side="buy",
        quantity=100,
        price=10,
    )
    record_trade(
        client,
        main["id"],
        trade_date="2026-07-10",
        side="buy",
        quantity=20,
        price=20,
    )
    record_trade(
        client,
        reserve["id"],
        trade_date="2026-07-10",
        side="buy",
        quantity=5,
        price=20,
    )

    listed = client.get(
        "/api/portfolio/trades",
        params={"account_id": main["id"], "date_from": "2026-07-05"},
    )
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1

    deleted = client.delete(f"/api/portfolio/trades/{first['id']}")
    assert deleted.status_code == 200
    snapshot = client.get("/api/portfolio/snapshot", params={"as_of": "2026-07-31"}).json()
    assert snapshot["accounts"][0]["positions"][0]["quantity"] == 20


def test_legacy_position_is_migrated_to_opening_buy_trade_without_data_loss(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    data_path = tmp_path / "user_data" / "portfolio.json"
    data_path.parent.mkdir(parents=True)
    data_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "id": "legacy-account",
                        "name": "旧账户",
                        "created_at": "2026-07-01T09:00:00+08:00",
                        "updated_at": "2026-07-01T09:00:00+08:00",
                    }
                ],
                "positions": [
                    {
                        "id": "legacy-position",
                        "account_id": "legacy-account",
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "asset_type": "stock",
                        "quantity": 100,
                        "average_cost": 10,
                        "purchase_date": "2026-07-01",
                        "note": "原备注",
                        "created_at": "2026-07-01T09:00:00+08:00",
                        "updated_at": "2026-07-01T09:00:00+08:00",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    snapshot = client.get("/api/portfolio/snapshot", params={"as_of": "2026-07-31"})
    assert snapshot.status_code == 200
    assert snapshot.json()["accounts"][0]["positions"][0]["quantity"] == 100
    backup_path = data_path.with_name("portfolio.pre-trade-ledger-v1.json")
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    assert backup["positions"][0]["id"] == "legacy-position"
    assert "schema_version" not in backup
    document = json.loads(data_path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 4
    assert "positions" not in document
    assert document["trades"][0]["migration_source"] == "legacy_position"
    assert document["trades"][0]["note"] == "原备注"
    assert document["trades"][0]["seq"] == 1


def test_trades_from_v2_without_seq_are_backfilled_in_recorded_order(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    data_path = tmp_path / "user_data" / "portfolio.json"
    data_path.parent.mkdir(parents=True)
    data_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "accounts": [
                    {
                        "id": "acc-1",
                        "name": "主账户",
                        "created_at": "2026-07-01T09:00:00+08:00",
                        "updated_at": "2026-07-01T09:00:00+08:00",
                    }
                ],
                "trades": [
                    {
                        "id": "later-recorded",
                        "account_id": "acc-1",
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "asset_type": "stock",
                        "trade_date": "2026-07-01",
                        "side": "buy",
                        "quantity": 50,
                        "price": 20,
                        "fee": 0,
                        "tax": 0,
                        "note": "",
                        "created_at": "2026-07-02T10:00:00+08:00",
                    },
                    {
                        "id": "earlier-recorded",
                        "account_id": "acc-1",
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "asset_type": "stock",
                        "trade_date": "2026-07-01",
                        "side": "buy",
                        "quantity": 100,
                        "price": 10,
                        "fee": 0,
                        "tax": 0,
                        "note": "",
                        "created_at": "2026-07-01T09:30:00+08:00",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    listed = client.get("/api/portfolio/trades")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == ["later-recorded", "earlier-recorded"]
    document = json.loads(data_path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 4
    seq_by_id = {item["id"]: item["seq"] for item in document["trades"]}
    assert seq_by_id == {"earlier-recorded": 1, "later-recorded": 2}


def reorder(client: TestClient, trade_ids: list[str]):
    return client.post("/api/portfolio/trades/reorder", json={"trade_ids": trade_ids})


def test_same_day_reorder_changes_fifo_batches_and_realized_pnl(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    buy_10 = record_trade(
        client, account["id"], trade_date="2026-08-01", side="buy",
        quantity=100, price=10,
    )
    buy_20 = record_trade(
        client, account["id"], trade_date="2026-08-01", side="buy",
        quantity=100, price=20,
    )
    sell = record_trade(
        client, account["id"], trade_date="2026-08-01", side="sell",
        quantity=100, price=15,
    )

    before = client.get("/api/portfolio/snapshot", params={"as_of": "2026-08-01"}).json()
    assert before["accounts"][0]["positions"][0]["average_cost"] == 20.0
    assert before["realized_pnl"] == 500.0

    # 列表首 = 最早回放:先消耗 20 元批次,剩余 10 元批次,已实现盈亏转负
    response = reorder(client, [buy_20["id"], buy_10["id"], sell["id"]])
    assert response.status_code == 200
    after = client.get("/api/portfolio/snapshot", params={"as_of": "2026-08-01"}).json()
    assert after["accounts"][0]["positions"][0]["average_cost"] == 10.0
    assert after["realized_pnl"] == -500.0

    listed = client.get("/api/portfolio/trades").json()["items"]
    assert [item["id"] for item in listed] == [sell["id"], buy_10["id"], buy_20["id"]]


def test_reorder_rejects_sequence_that_breaks_later_sell(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    record_trade(
        client, account["id"], trade_date="2026-07-01", side="buy",
        quantity=100, price=10,
    )
    buy_then = record_trade(
        client, account["id"], trade_date="2026-07-02", side="buy",
        quantity=100, price=10,
    )
    sell_then = record_trade(
        client, account["id"], trade_date="2026-07-02", side="sell",
        quantity=200, price=12,
    )

    response = reorder(client, [sell_then["id"], buy_then["id"]])
    assert response.status_code == 409
    assert "可用数量" in response.json()["detail"]
    # 校验失败不落盘:快照与交易顺序保持原样
    listed = client.get("/api/portfolio/trades").json()["items"]
    assert [item["id"] for item in listed[:2]] == [sell_then["id"], buy_then["id"]]


def test_reorder_keeps_other_accounts_slots_and_validates_payload(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    main = create_account(client, "主账户")
    reserve = create_account(client, "备用账户")
    first = record_trade(
        client, main["id"], trade_date="2026-07-10", side="buy",
        quantity=100, price=10,
    )
    second = record_trade(
        client, reserve["id"], trade_date="2026-07-10", side="buy",
        quantity=20, price=20,
    )
    third = record_trade(
        client, main["id"], trade_date="2026-07-10", side="buy",
        quantity=30, price=30,
    )

    # 主账户视角只交换自己两笔,备用账户的一笔停留在原槽位
    response = reorder(client, [third["id"], first["id"]])
    assert response.status_code == 200
    listed = client.get("/api/portfolio/trades").json()["items"]
    assert [item["id"] for item in listed] == [first["id"], second["id"], third["id"]]

    unknown = reorder(client, ["missing-1", "missing-2"])
    assert unknown.status_code == 404
    cross_day = record_trade(
        client, main["id"], trade_date="2026-07-11", side="buy",
        quantity=5, price=30,
    )
    mixed_days = reorder(client, [first["id"], cross_day["id"]])
    assert mixed_days.status_code == 400
    assert "同一交易日" in mixed_days.json()["detail"]
    single = reorder(client, [first["id"]])
    assert single.status_code == 422


def test_direct_position_mutation_is_not_part_of_the_public_contract(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    response = client.put(
        f"/api/portfolio/accounts/{account['id']}/positions/600519.SH",
        json={"quantity": 100, "average_cost": 10},
    )
    assert response.status_code == 404


def test_account_with_trade_history_cannot_be_deleted(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    record_trade(
        client,
        account["id"],
        trade_date="2026-07-01",
        side="buy",
        quantity=10,
        price=10,
    )

    response = client.delete(f"/api/portfolio/accounts/{account['id']}")
    assert response.status_code == 409
    assert response.json()["detail"] == "账户仍有交易记录。请先删除交易"


def test_trade_rejects_unsupported_instrument_and_non_finite_numbers(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    unsupported = client.post(
        "/api/portfolio/trades",
        json={
            "account_id": account["id"],
            "symbol": "000001.SH",
            "trade_date": "2026-07-01",
            "side": "buy",
            "quantity": 1,
            "price": 3000,
        },
    )
    assert unsupported.status_code == 400

    non_finite = client.post(
        "/api/portfolio/trades",
        content=(
            f'{{"account_id":"{account["id"]}","symbol":"600519.SH",'
            '"trade_date":"2026-07-01","side":"buy","quantity":1e999,"price":10}'
        ),
        headers={"Content-Type": "application/json"},
    )
    assert non_finite.status_code == 422


def test_trade_price_can_be_corrected_and_reflows_into_replay_cost(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    wrong = record_trade(
        client, account["id"], trade_date="2026-07-30", side="buy",
        quantity=100, price=10,
    )
    record_trade(
        client, account["id"], trade_date="2026-07-31", side="sell",
        quantity=50, price=15,
    )

    response = client.patch(
        f"/api/portfolio/trades/{wrong['id']}/price", json={"price": 12.34567}
    )
    assert response.status_code == 200
    assert response.json()["price"] == 12.346

    listed = client.get("/api/portfolio/trades").json()["items"]
    assert [item["price"] for item in listed] == [15, 12.346]

    snapshot = client.get("/api/portfolio/snapshot", params={"as_of": "2026-08-01"}).json()
    position = snapshot["accounts"][0]["positions"][0]
    assert position["quantity"] == 50
    assert abs(position["average_cost"] - 12.346) < 1e-9
    assert abs(position["total_cost"] - 617.3) < 1e-6
    assert abs(snapshot["realized_pnl"] - 132.7) < 1e-6


def test_trade_price_update_rejects_unknown_id_and_non_positive_price(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    trade = record_trade(
        client, account["id"], trade_date="2026-07-30", side="buy",
        quantity=100, price=10,
    )

    unknown = client.patch("/api/portfolio/trades/not-exist/price", json={"price": 1})
    assert unknown.status_code == 404
    zero = client.patch(f"/api/portfolio/trades/{trade['id']}/price", json={"price": 0})
    assert zero.status_code == 422
    negative = client.patch(f"/api/portfolio/trades/{trade['id']}/price", json={"price": -2})
    assert negative.status_code == 422
    text = client.patch(f"/api/portfolio/trades/{trade['id']}/price", json={"price": "abc"})
    assert text.status_code == 422


def test_trade_quantity_and_price_can_be_corrected_together_and_replayed(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    wrong = record_trade(
        client, account["id"], trade_date="2026-07-30", side="buy",
        quantity=100, price=10,
    )
    record_trade(
        client, account["id"], trade_date="2026-07-31", side="sell",
        quantity=50, price=15,
    )

    response = client.patch(
        f"/api/portfolio/trades/{wrong['id']}",
        json={"quantity": 120, "price": 12.34567},
    )

    assert response.status_code == 200
    assert response.json()["quantity"] == 120
    assert response.json()["price"] == 12.346
    listed = client.get("/api/portfolio/trades").json()["items"]
    assert [(item["quantity"], item["price"]) for item in listed] == [
        (50, 15),
        (120, 12.346),
    ]
    snapshot = client.get(
        "/api/portfolio/snapshot", params={"as_of": "2026-08-01"}
    ).json()
    position = snapshot["accounts"][0]["positions"][0]
    assert position["quantity"] == 70
    assert abs(position["average_cost"] - 12.346) < 1e-9
    assert abs(position["total_cost"] - 864.22) < 1e-6
    assert abs(snapshot["realized_pnl"] - 132.7) < 1e-6


def test_trade_quantity_update_rejects_oversell_without_partial_write(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    buy = record_trade(
        client, account["id"], trade_date="2026-07-30", side="buy",
        quantity=100, price=10,
    )
    record_trade(
        client, account["id"], trade_date="2026-07-31", side="sell",
        quantity=80, price=15,
    )

    blocked = client.patch(
        f"/api/portfolio/trades/{buy['id']}",
        json={"quantity": 79, "price": 11},
    )

    assert blocked.status_code == 409
    assert "卖出超过可用数量" in blocked.json()["detail"]
    persisted = client.get("/api/portfolio/trades").json()["items"][-1]
    assert (persisted["quantity"], persisted["price"]) == (100, 10)


def test_trade_execution_update_rejects_unknown_id_and_invalid_values(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    trade = record_trade(
        client, account["id"], trade_date="2026-07-30", side="buy",
        quantity=100, price=10,
    )

    unknown = client.patch(
        "/api/portfolio/trades/not-exist", json={"quantity": 1, "price": 1}
    )
    assert unknown.status_code == 404
    zero_quantity = client.patch(
        f"/api/portfolio/trades/{trade['id']}", json={"quantity": 0, "price": 1}
    )
    assert zero_quantity.status_code == 422
    zero_price = client.patch(
        f"/api/portfolio/trades/{trade['id']}", json={"quantity": 1, "price": 0}
    )
    assert zero_price.status_code == 422


def test_trade_execution_update_reestimates_only_system_estimated_costs(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    estimated = client.post(
        "/api/portfolio/trades",
        json={
            "account_id": account["id"],
            "symbol": "600519.SH",
            "trade_date": "2026-07-30",
            "side": "buy",
            "quantity": 100,
            "price": 10,
        },
    ).json()
    manual = record_trade(
        client, account["id"], trade_date="2026-07-31", side="buy",
        quantity=100, price=10, fee=9, tax=1,
    )

    estimated_response = client.patch(
        f"/api/portfolio/trades/{estimated['id']}",
        json={"quantity": 1000, "price": 100},
    )
    manual_response = client.patch(
        f"/api/portfolio/trades/{manual['id']}",
        json={"quantity": 1000, "price": 100},
    )

    assert estimated_response.status_code == 200
    assert (
        estimated_response.json()["fee"],
        estimated_response.json()["tax"],
        estimated_response.json()["cost_source"],
    ) == (26.0, 0.0, "estimated")
    assert manual_response.status_code == 200
    assert (
        manual_response.json()["fee"],
        manual_response.json()["tax"],
        manual_response.json()["cost_source"],
    ) == (9.0, 1.0, "manual")


def test_trade_cost_can_be_corrected_and_reflows_into_replay_cost(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    trade = record_trade(
        client, account["id"], trade_date="2026-07-30", side="buy",
        quantity=100, price=10, fee=0, tax=0,
    )

    response = client.patch(
        f"/api/portfolio/trades/{trade['id']}/cost", json={"fee": 7.55, "tax": 1}
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["fee"], body["tax"], body["cost_source"]) == (7.55, 1, "manual")

    listed = client.get("/api/portfolio/trades").json()["items"]
    assert (listed[0]["fee"], listed[0]["tax"], listed[0]["cost_source"]) == (7.55, 1, "manual")

    snapshot = client.get("/api/portfolio/snapshot", params={"as_of": "2026-08-01"}).json()
    position = snapshot["accounts"][0]["positions"][0]
    assert abs(snapshot["total_fee"] - 7.55) < 1e-9
    assert abs(position["total_cost"] - 1008.55) < 1e-6


def test_trade_cost_update_with_missing_field_reestimates_and_marks_estimated(
    tmp_path, monkeypatch
):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    trade = record_trade(
        client, account["id"], trade_date="2026-07-30", side="buy",
        quantity=100, price=10, fee=9, tax=3,
    )

    # 只传 tax: fee 字段缺省 => 按费率配置重新估算 600519.SH 买入 (最低佣金5 + 过户费0.01)
    response = client.patch(
        f"/api/portfolio/trades/{trade['id']}/cost", json={"tax": 0.4}
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["fee"], body["tax"], body["cost_source"]) == (5.01, 0.4, "estimated")


def test_trade_cost_update_rejects_unknown_id_and_invalid_value(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client)
    trade = record_trade(
        client, account["id"], trade_date="2026-07-30", side="buy",
        quantity=100, price=10,
    )

    unknown = client.patch("/api/portfolio/trades/not-exist/cost", json={"fee": 1})
    assert unknown.status_code == 404
    negative = client.patch(f"/api/portfolio/trades/{trade['id']}/cost", json={"fee": -1})
    assert negative.status_code == 422
    text = client.patch(f"/api/portfolio/trades/{trade['id']}/cost", json={"tax": "abc"})
    assert text.status_code == 422
