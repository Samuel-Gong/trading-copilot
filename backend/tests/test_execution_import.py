"""使用隔离合成账本验证桌面成交导入 v1。"""
from __future__ import annotations

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from test_portfolio_api import FakePortfolioRepo, create_account, make_client

from app.services import execution_import, portfolio

URL = "/api/portfolio/execution-imports"


def item(seed="first", **changes):
    return {"source_record_id": hashlib.sha256(seed.encode()).hexdigest(),
            "identity_kind": "row_fingerprint", "executed_at": "2026-07-30T02:00:00.000Z",
            "trade_date": "2026-07-30", "stock_code": "600519", "stock_name": "合成标的",
            "side": "buy", "quantity": 100, "price": 10.5, "amount": 1050,
            "contract_number": None, "order_reference": None, "fee": None, "tax": None, **changes}


def batch(account, items=None, mode="preview"):
    return {"schema_version": 1, "batch_id": str(uuid.uuid4()), "source": "tonghuashun",
            "source_account_id": "synthetic-broker", "account_id": account,
            "mode": mode, "items": items if items is not None else [item()]}


def post(client, body, code=200):
    response = client.post(URL, json=body)
    assert response.status_code == code, response.text
    return response.json()


def statuses(result):
    return [row["status"] for row in result["items"]]


@pytest.fixture
def context(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    account = create_account(client, "合成测试账户")
    return client, account["id"], tmp_path / "user_data" / "portfolio.json"


def manual(account, **changes):
    return portfolio.record_trade(FakePortfolioRepo(), **{
        "account_id": account, "symbol": "600519.SH", "trade_date": date(2026, 7, 30),
        "side": "buy", "quantity": 100, "price": 10.5, "fee": 9, "tax": 2, **changes})


def test_preview_commit_lost_response_restart_and_new_batch(context):
    client, account, path = context
    body = batch(account)
    assert statuses(post(client, body)) == ["ready"]
    assert json.loads(path.read_text())["trades"] == []
    body["mode"] = "commit"
    inserted = post(client, body)
    assert statuses(inserted) == ["inserted"]
    trade_id = inserted["items"][0]["trade_id"]
    restarted = execution_import.receive(FakePortfolioRepo(), execution_import.ExecutionImportRequest.model_validate(body))
    assert statuses(restarted) == ["duplicate"]
    assert restarted["items"][0]["trade_id"] == trade_id
    body["batch_id"] = str(uuid.uuid4())
    assert post(client, body)["items"][0]["trade_id"] == trade_id
    document = json.loads(path.read_text())
    assert len(document["trades"]) == len(document["execution_imports"]["bindings"]) == 1
    trade = document["trades"][0]
    assert trade["cost_source"] == "estimated" and trade["fee"] > 0
    assert trade["amount"] == 1050
    assert trade["executed_at"] == "2026-07-30T02:00:00+00:00"


@pytest.mark.parametrize("change", ["target", "content", "source", "order"])
def test_batch_content_locked_by_preview(context, change):
    client, account, path = context
    body = batch(account, [item(), item("second", stock_code="510300")])
    post(client, body)
    if change == "target":
        body["account_id"] = create_account(client, "另一合成账户")["id"]
    elif change == "content":
        body["items"][0]["stock_name"] = "修改名称"
    elif change == "source":
        body["source_account_id"] = "another-synthetic-broker"
    else:
        body["items"].reverse()
    before = path.read_bytes()
    body["mode"] = "commit"
    assert statuses(post(client, body, 409)) == ["conflict", "conflict"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", ["target", "content"])
def test_source_binding_conflicts_across_batches(context, change):
    client, account, path = context
    body = batch(account, mode="commit")
    post(client, body)
    body["batch_id"] = str(uuid.uuid4())
    if change == "target":
        body["account_id"] = create_account(client, "另一合成账户")["id"]
    else:
        body["items"][0].update(price=11, amount=1100)
    before = path.read_bytes()
    assert statuses(post(client, body, 409)) == ["conflict"]
    assert path.read_bytes() == before


def test_manual_history_conflict_preserves_costs(context):
    client, account, path = context
    manual(account)
    body = batch(account)
    result = post(client, body)
    assert statuses(result) == ["conflict"]
    assert "Trading Copilot" in result["items"][0]["message"]
    before = path.read_bytes()
    body["mode"] = "commit"
    post(client, body, 409)
    assert path.read_bytes() == before
    assert portfolio.list_trades()[0]["fee"] == 9
    assert portfolio.list_trades()[0]["tax"] == 2


@pytest.mark.parametrize("ambiguity", ["same_fingerprint", "same_second", "contract", "order"])
def test_identity_ambiguity_blocks_entire_batch(context, ambiguity):
    client, account, path = context
    first = item()
    second = item("second", executed_at="2026-07-30T02:01:00Z", quantity=200, amount=2100)
    if ambiguity == "same_fingerprint":
        second["source_record_id"] = first["source_record_id"]
    elif ambiguity == "same_second":
        second["executed_at"] = "2026-07-30T02:00:00.500Z"
    elif ambiguity == "natural":
        second.update(quantity=100, amount=1050)
    else:
        field = "contract_number" if ambiguity == "contract" else "order_reference"
        first[field] = second[field] = "synthetic-reference"
    body = batch(account, [first, second])
    assert statuses(post(client, body)) == ["conflict", "conflict"]
    before = path.read_bytes()
    body["mode"] = "commit"
    post(client, body, 409)
    assert path.read_bytes() == before


def test_later_cumulative_update_conflicts(context):
    client, account, path = context
    post(client, batch(account, [item(contract_number="synthetic-contract")], "commit"))
    body = batch(account, [item("cumulative", contract_number="synthetic-contract", quantity=200,
                                amount=2100, executed_at="2026-07-30T02:01:00Z")], "commit")
    before = path.read_bytes()
    assert statuses(post(client, body, 409)) == ["conflict"]
    assert path.read_bytes() == before


def test_time_order_not_buy_first_and_fifo_atomicity(context):
    client, account, path = context
    sell = item("sell", side="sell", executed_at="2026-07-30T01:00:00Z")
    before = path.read_bytes()
    assert statuses(post(client, batch(account, [item(), sell], "commit"), 409)) == ["conflict", "conflict"]
    assert path.read_bytes() == before
    body = batch(account, [item("sell", side="sell", executed_at="2026-07-30T03:00:00Z"), item()], "commit")
    assert statuses(post(client, body)) == ["inserted", "inserted"]
    assert [t["side"] for t in sorted(portfolio.list_trades(), key=lambda t: t["seq"])] == ["buy", "sell"]
    assert portfolio.held_symbols() == set()


def test_out_of_order_batches_with_older_manual_history(context):
    client, account, _ = context
    manual(account, trade_date=date(2026, 7, 29), price=5, fee=0, tax=0)
    post(client, batch(account, [item("late", price=20, amount=2000, executed_at="2026-07-30T04:00:00Z")], "commit"))
    post(client, batch(account, [item("early", side="sell", fee=0, tax=0)], "commit"))
    trades = sorted(portfolio.list_trades(), key=portfolio._trade_sort_key)
    assert [t["price"] for t in trades] == [5, 10.5, 20]
    assert portfolio._replay(trades)[1]["realized_pnl"] == 550


def test_commit_revalidates_after_preview(context):
    client, account, path = context
    body = batch(account)
    assert statuses(post(client, body)) == ["ready"]
    manual(account, quantity=50, price=5)
    before = path.read_bytes()
    body["mode"] = "commit"
    post(client, body, 409)
    assert path.read_bytes() == before


@pytest.mark.parametrize("changes", [{"stock_code": "999999"}, {"trade_date": "2026-07-29"},
                                     {"executed_at": "2099-01-01T00:00:00Z", "trade_date": "2099-01-01"},
                                     {"amount": 1100}])
def test_unsupported_mixed_batch_atomicity(context, changes):
    client, account, path = context
    body = batch(account, [item(), item("unsupported", **changes)])
    assert post(client, body)["items"][1]["status"] == "unsupported"
    before = path.read_bytes()
    body["mode"] = "commit"
    post(client, body, 409)
    assert path.read_bytes() == before


def test_directory_disambiguates_index_and_rejects_multiple_markets(context, monkeypatch):
    client, account, _ = context
    post(client, batch(account, [item(stock_code="000001")], "commit"))
    assert portfolio.list_trades()[0]["symbol"] == "000001.SZ"
    monkeypatch.setattr(FakePortfolioRepo, "names", {"600519.SH": "合成一", "600519.SZ": "合成二"})
    assert statuses(post(client, batch(account, [item("ambiguous")]))) == ["unsupported"]


def test_amount_rounding_and_partial_unknown_fees(context, monkeypatch):
    client, account, _ = context
    calls = []
    monkeypatch.setattr(execution_import.trade_fees, "estimate_trade_cost", lambda **kw: (calls.append(kw) or (7, 1)))
    post(client, batch(account, [item(price=10.123, amount=1012.34, fee=2)], "commit"))
    trades = portfolio.list_trades()
    assert (trades[0]["fee"], trades[0]["tax"], trades[0]["cost_source"]) == (2, 1, "estimated")
    assert calls[-1]["price"] == 10.1234
    assert portfolio._replay(trades)[0][0]["average_cost"] == 10.1534


def test_duplicate_preserves_calibrated_fees_and_deletion_tombstone(context):
    client, account, _ = context
    body = batch(account, mode="commit")
    trade_id = post(client, body)["items"][0]["trade_id"]
    portfolio.update_trade_cost(trade_id, fee=12, tax=3)
    assert statuses(post(client, body)) == ["duplicate"]
    assert portfolio.list_trades()[0]["fee"] == 12
    portfolio.delete_trade(trade_id)
    post(client, body, 409)
    assert portfolio.list_trades() == []


def test_source_execution_and_date_not_manually_rewritten(context):
    client, account, _ = context
    trade_id = post(client, batch(account, mode="commit"))["items"][0]["trade_id"]
    assert client.patch(f"/api/portfolio/trades/{trade_id}", json={"quantity": 200, "price": 10}).status_code == 409
    assert client.patch(f"/api/portfolio/trades/{trade_id}/date", json={"trade_date": "2026-07-29"}).status_code == 409


@pytest.mark.parametrize("changes", [{"schema_version": 2}, {"batch_id": "invalid"}, {"source_account_id": " "}, {"items": []}])
def test_invalid_envelope_does_not_echo_account(context, changes):
    client, account, _ = context
    response = client.post(URL, json={**batch(account), **changes})
    assert response.status_code == 422
    assert account not in response.text and "synthetic-broker" not in response.text


@pytest.mark.parametrize("field,value", [("quantity", 0), ("price", -1), ("fee", -1),
                                         ("executed_at", "2026-07-30T02:00:00"), ("quantity", "NaN")])
def test_invalid_row_sanitized(context, field, value):
    client, account, _ = context
    post(client, batch(account, [item(**{field: value})]), 422)


def test_concurrent_batches_exactly_once(context):
    _, account, _ = context
    bodies = [execution_import.ExecutionImportRequest.model_validate(batch(account, mode="commit")) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda body: execution_import.receive(FakePortfolioRepo(), body), bodies))
    assert sorted(statuses(r)[0] for r in results) == ["duplicate"] * 7 + ["inserted"]
    assert len({r["items"][0]["trade_id"] for r in results}) == 1
    assert len(portfolio.list_trades()) == 1


def test_atomic_replace_failure_rolls_back(context, monkeypatch):
    client, account, path = context
    before = path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(portfolio.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("合成落盘失败")))
        post(client, batch(account, mode="commit"), 500)
    assert path.read_bytes() == before
    assert statuses(post(client, batch(account, mode="commit"))) == ["inserted"]


def test_legacy_migration_preserves_binding_after_other_mutations(context):
    client, account, path = context
    path.write_text(json.dumps({"schema_version": 1, "accounts": [{"id": account, "name": "合成"}],
                               "positions": [{"id": "synthetic-legacy", "account_id": account,
                                              "symbol": "600519.SH", "quantity": 100, "average_cost": 5,
                                              "purchase_date": "2026-07-29"}]}))
    body = batch(account, [item(side="sell")], "commit")
    assert statuses(post(client, body)) == ["inserted"]
    portfolio.update_account(account, "合成新名称")
    assert statuses(post(client, body)) == ["duplicate"]
    assert path.with_name("portfolio.pre-trade-ledger-v1.json").exists()
    assert len(json.loads(path.read_text())["trades"]) == 2


@pytest.mark.parametrize("bad", ['{"trades":', '[]', '{"accounts": {}}', '{"execution_imports": null}'])
def test_corrupt_ledger_not_overwritten(context, bad):
    client, account, path = context
    path.write_text(bad)
    post(client, batch(account, mode="commit"), 409)
    assert path.read_text() == bad


def test_auth_middleware_requires_session(context, monkeypatch):
    client, account, path = context
    from app.main import auth_middleware
    from app.services import auth
    monkeypatch.setattr(auth, "is_configured", lambda: True)
    monkeypatch.setattr(auth, "is_valid_session", lambda token: token == "synthetic-session")
    client.app.middleware_stack = None
    client.app.middleware("http")(auth_middleware)
    before = path.read_bytes()
    assert client.post(URL, json=batch(account, mode="commit")).status_code == 401
    assert path.read_bytes() == before
    client.cookies.set("tf_session", "synthetic-session")
    assert statuses(post(client, batch(account, mode="commit"))) == ["inserted"]


def test_watch_pool_and_closed_monitor_retry(context, monkeypatch):
    client, account, _ = context
    from app.api import portfolio as api
    calls = []
    monkeypatch.setattr(api, "_cleanup_closed_position_rules", lambda request, symbols: calls.append(symbols))
    portfolio.add_watch_pool_item(FakePortfolioRepo(), "600519.SH")
    post(client, batch(account, mode="commit"))
    assert portfolio.list_watch_pool() == []
    body = batch(account, [item("sell", side="sell", executed_at="2026-07-30T03:00:00Z")], "commit")
    post(client, body)
    post(client, body)
    assert calls[-2:] == [{"600519.SH"}, {"600519.SH"}]
    assert portfolio.held_symbols() == set()


def test_distinct_executions_same_quantity_price_are_supported(context):
    client, account, _ = context
    body = batch(account, [item(contract_number="synthetic-one"), item("second", contract_number="synthetic-two", executed_at="2026-07-30T02:01:00Z")], "commit")
    assert statuses(post(client, body)) == ["inserted", "inserted"]
    assert portfolio._replay(portfolio.list_trades())[0][0]["quantity"] == 200


def test_natural_candidate_in_another_source_account_conflicts(context):
    client, account, path = context
    post(client, batch(account, mode="commit"))
    body = batch(account, [item("another", executed_at="2026-07-30T03:00:00Z")], "commit")
    body["source_account_id"] = "synthetic-other"
    before = path.read_bytes()
    post(client, body, 409)
    assert path.read_bytes() == before


def test_input_limit_and_response_bijection(context):
    client, account, _ = context
    post(client, batch(account, [item()] * 2001), 422)
    body = batch(account, [item(str(i)) for i in range(2000)])
    result = post(client, body)
    assert len(result["items"]) == 2000
    assert [r["source_record_id"] for r in result["items"]] == [r["source_record_id"] for r in body["items"]]
    assert set(statuses(result)) == {"conflict"}


def test_new_process_reads_durable_binding(context):
    import os
    import subprocess
    import sys
    client, account, path = context
    body = batch(account, mode="commit")
    trade_id = post(client, body)["items"][0]["trade_id"]
    script = """
import json, sys
from pathlib import Path
sys.path.insert(0, 'tests')
from app.config import settings
from app.services.execution_import import ExecutionImportRequest, receive
from test_portfolio_api import FakePortfolioRepo
settings.data_dir = Path(sys.argv[1])
print(json.dumps(receive(FakePortfolioRepo(), ExecutionImportRequest.model_validate(json.load(sys.stdin)))))
"""
    env = {k: v for k, v in os.environ.items() if k.lower() not in {"http_proxy", "https_proxy", "all_proxy"}}
    result = subprocess.run([sys.executable, "-c", script, str(path.parent.parent)],
                            input=json.dumps(body), text=True, capture_output=True, check=True, env=env)
    restarted = json.loads(result.stdout)
    assert statuses(restarted) == ["duplicate"]
    assert restarted["items"][0]["trade_id"] == trade_id


def test_missing_source_metadata_fails_closed(context):
    client, account, path = context
    body = batch(account, mode="commit")
    post(client, body)
    document = json.loads(path.read_text())
    document.pop("execution_imports")
    path.write_text(json.dumps(document))
    before = path.read_bytes()
    post(client, body, 409)
    assert path.read_bytes() == before


def test_cleanup_only_uses_actual_committed_market(context, monkeypatch):
    from app.api import portfolio as api
    client, account, _ = context
    calls = []
    monkeypatch.setattr(api, "_cleanup_closed_position_rules", lambda request, symbols: calls.append(symbols))
    post(client, batch(account, [item(stock_code="000001")], "commit"))
    body = batch(account, [item("sell", stock_code="000001", side="sell", executed_at="2026-07-30T03:00:00Z")], "commit")
    post(client, body)
    post(client, body)
    assert calls[-2:] == [{"000001.SZ"}, {"000001.SZ"}]
    assert "000001.SH" not in calls[-1]
