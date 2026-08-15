"""交易流水驱动的研究型持仓服务。

账户只保存用户分组,持仓永远由截至 ``as_of`` 的买卖交易按 FIFO 回放得到。
服务不连接券商,也不维护现金、分红、拆并股或外汇。
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings
from app.services import trade_fees

_SCHEMA_VERSION = 4
_SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_LOCK = threading.RLock()
_TIMEZONE = ZoneInfo("Asia/Shanghai")
_EPSILON = 1e-9


class PortfolioNotFoundError(LookupError):
    """账户、交易或持仓不存在。"""


class PortfolioConflictError(RuntimeError):
    """请求与持仓或观察状态冲突。"""


def _path() -> Path:
    path = settings.data_dir / "user_data" / "portfolio.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _empty_document() -> dict:
    return {
        "schema_version": _SCHEMA_VERSION,
        "accounts": [],
        "trades": [],
        "watch_pool": [],
    }


def _legacy_trade(item: dict) -> dict | None:
    """把旧版当前持仓转换为一笔等价的期初买入交易。"""
    try:
        quantity = float(item.get("quantity"))
        price = float(item.get("average_cost"))
    except (TypeError, ValueError):
        return None
    if quantity <= 0 or price < 0 or not item.get("account_id") or not item.get("symbol"):
        return None
    created_at = str(item.get("created_at") or _now_iso())
    trade_date = item.get("purchase_date") or created_at[:10]
    try:
        date.fromisoformat(str(trade_date))
    except ValueError:
        trade_date = today().isoformat()
    legacy_id = str(item.get("id") or uuid.uuid4().hex)
    return {
        "id": f"legacy_{legacy_id}",
        "account_id": item["account_id"],
        "symbol": str(item["symbol"]).strip().upper(),
        "name": str(item.get("name") or item["symbol"]),
        "asset_type": str(item.get("asset_type") or "stock"),
        "trade_date": str(trade_date),
        "side": "buy",
        "quantity": quantity,
        "price": price,
        "fee": 0.0,
        "tax": 0.0,
        "note": str(item.get("note") or ""),
        "migration_source": "legacy_position",
        "created_at": created_at,
    }


def _read() -> dict:
    path = _path()
    if not path.exists():
        return _empty_document()
    try:
        raw_value = path.read_text(encoding="utf-8")
        value = json.loads(raw_value)
    except (OSError, json.JSONDecodeError):
        return _empty_document()
    if not isinstance(value, dict):
        return _empty_document()
    accounts = value.get("accounts") if isinstance(value.get("accounts"), list) else []
    trades = value.get("trades") if isinstance(value.get("trades"), list) else []
    watch_pool = (
        value.get("watch_pool") if isinstance(value.get("watch_pool"), list) else []
    )
    positions = value.get("positions") if isinstance(value.get("positions"), list) else []
    migrated = [_legacy_trade(item) for item in positions]
    migrated = [item for item in migrated if item is not None]
    document = {
        "schema_version": _SCHEMA_VERSION,
        "accounts": accounts,
        "trades": [*trades, *migrated],
        "watch_pool": watch_pool,
    }
    if "positions" in value:
        _write_legacy_backup(raw_value)
    seq_updated = _ensure_seq(document)
    if seq_updated or value.get("schema_version") != _SCHEMA_VERSION or "positions" in value:
        _write(document)
    return document


def _write_legacy_backup(raw_value: str) -> None:
    """在首次迁移前保留旧版原始文件,并且永不覆盖已有备份。"""
    backup = _path().with_name("portfolio.pre-trade-ledger-v1.json")
    if backup.exists():
        return
    tmp = backup.with_suffix(backup.suffix + ".tmp")
    tmp.write_text(raw_value, encoding="utf-8")
    os.replace(tmp, backup)


def _write(document: dict) -> None:
    path = _path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now(_TIMEZONE).isoformat(timespec="seconds")


def _account(document: dict, account_id: str) -> dict:
    for item in document["accounts"]:
        if item.get("id") == account_id:
            return item
    raise PortfolioNotFoundError("持仓账户不存在")


def list_accounts() -> list[dict]:
    with _LOCK:
        return [dict(item) for item in _read()["accounts"]]


def create_account(name: str) -> dict:
    clean_name = name.strip()
    with _LOCK:
        document = _read()
        now = _now_iso()
        item = {
            "id": uuid.uuid4().hex,
            "name": clean_name,
            "created_at": now,
            "updated_at": now,
        }
        document["accounts"].append(item)
        _write(document)
        return dict(item)


def update_account(account_id: str, name: str) -> dict:
    with _LOCK:
        document = _read()
        item = _account(document, account_id)
        item["name"] = name.strip()
        item["updated_at"] = _now_iso()
        _write(document)
        return dict(item)


def delete_account(account_id: str) -> None:
    with _LOCK:
        document = _read()
        _account(document, account_id)
        if any(item.get("account_id") == account_id for item in document["trades"]):
            raise PortfolioConflictError("账户仍有交易记录。请先删除交易")
        document["accounts"] = [
            item for item in document["accounts"] if item.get("id") != account_id
        ]
        _write(document)


def _resolve_instrument(repo, symbol: str) -> tuple[str, str, str]:
    normalized = symbol.strip().upper()
    if not _SYMBOL_RE.fullmatch(normalized):
        raise ValueError("证券代码必须使用标准格式: 600519.SH")
    names = repo.get_name_map([normalized])
    if normalized not in names:
        raise ValueError("证券代码不在当前标的索引中")
    asset_type = repo.resolve_asset_type(normalized)
    if asset_type not in {"stock", "etf"}:
        raise ValueError("交易记录只支持 A 股和场内 ETF")
    return normalized, str(names[normalized] or normalized), str(asset_type)


def list_watch_pool() -> list[dict]:
    with _LOCK:
        return [dict(item) for item in _read()["watch_pool"]]


def add_watch_pool_item(repo, symbol: str) -> dict:
    normalized, name, asset_type = _resolve_instrument(repo, symbol)
    with _LOCK:
        document = _read()
        positions, _ = _replay(document["trades"], today())
        if any(item.get("symbol") == normalized for item in positions):
            raise PortfolioConflictError("当前仍持有该标的。不能加入观察池")
        if any(item.get("symbol") == normalized for item in document["watch_pool"]):
            raise PortfolioConflictError("标的已在观察池中")
        item = {
            "symbol": normalized,
            "name": name,
            "asset_type": asset_type,
            "added_at": _now_iso(),
        }
        document["watch_pool"].append(item)
        _write(document)
        return dict(item)


def remove_watch_pool_item(symbol: str) -> None:
    normalized = symbol.strip().upper()
    with _LOCK:
        document = _read()
        if not any(item.get("symbol") == normalized for item in document["watch_pool"]):
            raise PortfolioNotFoundError("观察标的不存在")
        document["watch_pool"] = [
            item for item in document["watch_pool"] if item.get("symbol") != normalized
        ]
        _write(document)


def _trade_sort_key(item: dict) -> tuple[str, int, str]:
    """回放执行顺序:同一交易日内由用户可调整的 ``seq`` 决定成交先后。"""
    return (
        str(item.get("trade_date") or ""),
        int(item.get("seq") or 0),
        str(item.get("id") or ""),
    )


def _ensure_seq(document: dict) -> bool:
    """为 v2 及更早的账本无 ``seq`` 交易补齐同日内序号,保持原录入顺序不变。"""
    trades = document["trades"]
    if all(isinstance(item.get("seq"), int) and not isinstance(item.get("seq"), bool) for item in trades):
        return False
    by_date: dict[str, list[dict]] = {}
    for item in trades:
        by_date.setdefault(str(item.get("trade_date") or ""), []).append(item)
    for group in by_date.values():
        group.sort(
            key=lambda item: (
                str(item.get("created_at") or ""),
                str(item.get("id") or ""),
            )
        )
        for index, item in enumerate(group):
            item["seq"] = index + 1
    return True


def _replay(trades: list[dict], as_of: date | None = None) -> tuple[list[dict], dict]:
    """按账户和标的回放交易,返回未平仓头寸与账本汇总。"""
    cutoff = as_of.isoformat() if as_of else None
    selected = [
        dict(item)
        for item in trades
        if cutoff is None or str(item.get("trade_date") or "") <= cutoff
    ]
    selected.sort(key=_trade_sort_key)
    states: dict[tuple[str, str], dict] = {}
    realized_pnl = 0.0
    total_fee = 0.0
    total_tax = 0.0
    for trade in selected:
        account_id = str(trade.get("account_id") or "")
        symbol = str(trade.get("symbol") or "")
        key = (account_id, symbol)
        state = states.setdefault(
            key,
            {
                "account_id": account_id,
                "symbol": symbol,
                "name": trade.get("name") or symbol,
                "asset_type": trade.get("asset_type") or "stock",
                "lots": [],
                "note": "",
                "created_at": trade.get("created_at") or _now_iso(),
                "updated_at": trade.get("created_at") or _now_iso(),
            },
        )
        quantity = float(trade["quantity"])
        price = float(trade["price"])
        fee = float(trade.get("fee") or 0)
        tax = float(trade.get("tax") or 0)
        total_fee += fee
        total_tax += tax
        state["name"] = trade.get("name") or state["name"]
        state["asset_type"] = trade.get("asset_type") or state["asset_type"]
        state["updated_at"] = trade.get("created_at") or state["updated_at"]
        if str(trade.get("note") or "").strip():
            state["note"] = str(trade["note"]).strip()
        if trade.get("side") == "buy":
            unit_cost = (quantity * price + fee + tax) / quantity
            state["lots"].append(
                {
                    "quantity": quantity,
                    "unit_cost": unit_cost,
                    "trade_date": trade["trade_date"],
                }
            )
            continue

        available = sum(float(lot["quantity"]) for lot in state["lots"])
        if quantity > available + _EPSILON:
            raise PortfolioConflictError(
                f"{symbol} 在 {trade['trade_date']} 的可卖数量为 {round(available, 6)},"
                f"不能卖出 {round(quantity, 6)}"
            )
        remaining = quantity
        sold_cost = 0.0
        while remaining > _EPSILON:
            lot = state["lots"][0]
            consumed = min(remaining, float(lot["quantity"]))
            sold_cost += consumed * float(lot["unit_cost"])
            lot["quantity"] = float(lot["quantity"]) - consumed
            remaining -= consumed
            if float(lot["quantity"]) <= _EPSILON:
                state["lots"].pop(0)
        realized_pnl += quantity * price - fee - tax - sold_cost

    positions: list[dict] = []
    for (account_id, symbol), state in states.items():
        quantity = sum(float(lot["quantity"]) for lot in state["lots"])
        if quantity <= _EPSILON:
            continue
        total_cost = sum(
            float(lot["quantity"]) * float(lot["unit_cost"]) for lot in state["lots"]
        )
        positions.append(
            {
                "id": f"position:{account_id}:{symbol}",
                "account_id": account_id,
                "symbol": symbol,
                "name": state["name"],
                "asset_type": state["asset_type"],
                "quantity": _round(quantity),
                "average_cost": _round(total_cost / quantity),
                "purchase_date": min(lot["trade_date"] for lot in state["lots"]),
                "note": state["note"],
                "created_at": state["created_at"],
                "updated_at": state["updated_at"],
            }
        )
    positions.sort(key=lambda item: (item["account_id"], item["symbol"]))
    return positions, {
        "trade_count": len(selected),
        "realized_pnl": _round(realized_pnl),
        "total_fee": _round(total_fee),
        "total_tax": _round(total_tax),
    }


def _validate_trades(trades: list[dict]) -> None:
    _replay(trades)


def _remove_held_watch_items(document: dict) -> None:
    positions, _ = _replay(document["trades"], today())
    held_symbols = {str(item.get("symbol") or "") for item in positions}
    document["watch_pool"] = [
        item
        for item in document["watch_pool"]
        if str(item.get("symbol") or "") not in held_symbols
    ]


def estimate_trade_cost_for(
    repo,
    *,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
) -> dict:
    """按标的与费率配置估算费用与税费,供录单预填与留空自动估算共用。"""
    normalized, _name, asset_type = _resolve_instrument(repo, symbol)
    fee, tax = trade_fees.estimate_trade_cost(
        asset_type=asset_type,
        symbol=normalized,
        side=side,
        quantity=quantity,
        price=price,
    )
    return {"fee": fee, "tax": tax}


def record_trade(
    repo,
    *,
    account_id: str,
    symbol: str,
    trade_date: date,
    side: str,
    quantity: float,
    price: float,
    fee: float | None = None,
    tax: float | None = None,
    note: str = "",
) -> dict:
    normalized, name, asset_type = _resolve_instrument(repo, symbol)
    cost_source = "manual"
    if fee is None or tax is None:
        estimated_fee, estimated_tax = trade_fees.estimate_trade_cost(
            asset_type=asset_type,
            symbol=normalized,
            side=side,
            quantity=quantity,
            price=price,
        )
        fee = estimated_fee if fee is None else fee
        tax = estimated_tax if tax is None else tax
        cost_source = "estimated"
    fee_value = float(fee) if fee is not None else 0.0
    tax_value = float(tax) if tax is not None else 0.0
    with _LOCK:
        document = _read()
        _account(document, account_id)
        item = {
            "id": uuid.uuid4().hex,
            "account_id": account_id,
            "symbol": normalized,
            "name": name,
            "asset_type": asset_type,
            "trade_date": trade_date.isoformat(),
            "side": side,
            "quantity": float(quantity),
            # 成交价统一保留 3 位小数,与后续修改成交价的写入口径一致
            "price": round(float(price), 3),
            "fee": fee_value,
            "tax": tax_value,
            "cost_source": cost_source,
            "note": note.strip(),
            "created_at": _now_iso(),
        }
        same_day = [
            existing
            for existing in document["trades"]
            if str(existing.get("trade_date") or "") == item["trade_date"]
        ]
        # 新录入默认排在该交易日最后,与原按 created_at 回放的行为一致;用户可再调整
        item["seq"] = max([int(existing.get("seq") or 0) for existing in same_day] or [0]) + 1
        candidate = [*document["trades"], item]
        _validate_trades(candidate)
        document["trades"] = candidate
        _remove_held_watch_items(document)
        _write(document)
        return dict(item)


def list_trades(
    *,
    account_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict]:
    with _LOCK:
        document = _read()
        if account_id is not None:
            _account(document, account_id)
        items = [
            dict(item)
            for item in document["trades"]
            if (account_id is None or item.get("account_id") == account_id)
            and (date_from is None or str(item.get("trade_date")) >= date_from.isoformat())
            and (date_to is None or str(item.get("trade_date")) <= date_to.isoformat())
        ]
    return sorted(items, key=_trade_sort_key, reverse=True)


def delete_trade(trade_id: str) -> None:
    with _LOCK:
        document = _read()
        target = next((item for item in document["trades"] if item.get("id") == trade_id), None)
        if target is None:
            raise PortfolioNotFoundError("交易记录不存在")
        candidate = [item for item in document["trades"] if item.get("id") != trade_id]
        try:
            _validate_trades(candidate)
        except PortfolioConflictError as exc:
            raise PortfolioConflictError("删除该交易会导致后续卖出超过可用数量") from exc
        document["trades"] = candidate
        _remove_held_watch_items(document)
        _write(document)


def update_trade_execution(
    trade_id: str,
    *,
    quantity: float | None = None,
    price: float | None = None,
) -> dict:
    """原子修改成交数量和价格,并拒绝会破坏后续卖出约束的变更。"""
    with _LOCK:
        document = _read()
        target = next((item for item in document["trades"] if item.get("id") == trade_id), None)
        if target is None:
            raise PortfolioNotFoundError("交易记录不存在")
        next_quantity = float(quantity) if quantity is not None else float(target["quantity"])
        next_price = round(float(price), 3) if price is not None else float(target["price"])
        replacement = {
            **target,
            "quantity": next_quantity,
            "price": next_price,
        }
        if target.get("cost_source") == "estimated":
            fee, tax = trade_fees.estimate_trade_cost(
                asset_type=str(target.get("asset_type") or "stock"),
                symbol=str(target.get("symbol") or ""),
                side=str(target.get("side") or "buy"),
                quantity=next_quantity,
                price=next_price,
            )
            replacement["fee"] = fee
            replacement["tax"] = tax
        candidate = [
            replacement
            if item.get("id") == trade_id
            else item
            for item in document["trades"]
        ]
        try:
            _validate_trades(candidate)
        except PortfolioConflictError as exc:
            raise PortfolioConflictError(
                "修改后的交易数量会导致某笔卖出超过可用数量"
            ) from exc
        document["trades"] = candidate
        _remove_held_watch_items(document)
        _write(document)
        return dict(next(item for item in candidate if item.get("id") == trade_id))


def update_trade_price(trade_id: str, price: float) -> dict:
    """兼容既有的仅修改成交价入口。"""
    return update_trade_execution(trade_id, price=price)


def update_trade_cost(trade_id: str, fee: float | None, tax: float | None) -> dict:
    """修改费用/税费;传入 None 的字段按费率配置重新估算。费用不影响卖出可行性回放,无需重校验。"""
    with _LOCK:
        document = _read()
        target = next((item for item in document["trades"] if item.get("id") == trade_id), None)
        if target is None:
            raise PortfolioNotFoundError("交易记录不存在")
        estimated_fee: float | None = None
        estimated_tax: float | None = None
        if fee is None or tax is None:
            estimated_fee, estimated_tax = trade_fees.estimate_trade_cost(
                asset_type=str(target.get("asset_type") or "stock"),
                symbol=str(target.get("symbol") or ""),
                side=str(target.get("side") or "buy"),
                quantity=float(target.get("quantity") or 0),
                price=float(target.get("price") or 0),
            )
        target["fee"] = round(float(fee), 2) if fee is not None else estimated_fee
        target["tax"] = round(float(tax), 2) if tax is not None else estimated_tax
        target["cost_source"] = "manual" if fee is not None and tax is not None else "estimated"
        _write(document)
        return dict(target)


def reorder_trades(trade_ids: list[str]) -> None:
    """把同一交易日内列出的交易按给定顺序回填到原槽位,列表首为最早发生。

    未传入的交易保持原有 ``seq`` 槽位不变,因此跨账户账本只需要提供当前
    筛选范围内要调整的那部分交易的完整相对顺序。
    """
    ids = [str(value) for value in trade_ids]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("重排请求必须包含不重复的交易 ID")
    with _LOCK:
        document = _read()
        by_id = {str(item.get("id")): item for item in document["trades"]}
        if any(value not in by_id for value in ids):
            raise PortfolioNotFoundError("交易记录不存在")
        days = {str(by_id[value].get("trade_date") or "") for value in ids}
        if len(days) != 1:
            raise ValueError("只能调整同一交易日内的交易顺序")
        day = days.pop()
        day_sorted = sorted(
            (
                item
                for item in document["trades"]
                if str(item.get("trade_date") or "") == day
            ),
            key=_trade_sort_key,
        )
        slots = [item for item in day_sorted if str(item.get("id")) in set(ids)]
        if len(slots) < 2:
            raise ValueError("同一交易日需要至少两笔交易才能调整顺序")
        slot_seqs = [int(item.get("seq") or 0) for item in slots]
        seq_by_id = dict(zip(ids, slot_seqs, strict=True))
        candidate = [
            {**item, "seq": seq_by_id.get(str(item.get("id")), item.get("seq"))}
            for item in document["trades"]
        ]
        try:
            _validate_trades(candidate)
        except PortfolioConflictError as exc:
            raise PortfolioConflictError("调整后的交易顺序会导致后续卖出超过可用数量") from exc
        document["trades"] = candidate
        _write(document)


def _coerce_price_date(value: date | datetime | str) -> date:
    price_date = value
    if isinstance(price_date, datetime):
        price_date = price_date.date()
    elif isinstance(price_date, str):
        price_date = date.fromisoformat(price_date)
    return price_date


def _latest_prices(
    repo,
    positions: list[dict],
    as_of: date,
) -> dict[tuple[str, str], tuple[float, date]]:
    symbols_by_asset: dict[str, set[str]] = {}
    for item in positions:
        symbols_by_asset.setdefault(item["asset_type"], set()).add(item["symbol"])
    prices: dict[tuple[str, str], tuple[float, date]] = {}
    for asset_type, symbols in sorted(symbols_by_asset.items()):
        frame = repo.get_daily_close_batch(
            asset_type,
            sorted(symbols),
            as_of - timedelta(days=365),
            as_of,
        )
        required = {"symbol", "date", "close"}
        if frame.is_empty() or not required.issubset(frame.columns):
            continue
        latest = (
            frame.drop_nulls(["symbol", "date", "close"])
            .sort(["symbol", "date"])
            .group_by("symbol", maintain_order=True)
            .last()
        )
        for row in latest.iter_rows(named=True):
            price_date = _coerce_price_date(row["date"])
            if price_date <= as_of:
                prices[(asset_type, row["symbol"])] = (float(row["close"]), price_date)
    return prices


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def _position_snapshot(
    item: dict,
    as_of: date,
    latest_prices: dict[tuple[str, str], tuple[float, date]],
    *,
    exact_price_date: bool = False,
) -> dict:
    quantity = float(item["quantity"])
    average_cost = float(item["average_cost"])
    total_cost = quantity * average_cost
    current_price, price_date = latest_prices.get(
        (item["asset_type"], item["symbol"]),
        (None, None),
    )
    if exact_price_date and price_date != as_of:
        current_price, price_date = None, None
    market_value = quantity * current_price if current_price is not None else None
    unrealized_pnl = market_value - total_cost if market_value is not None else None
    ratio = unrealized_pnl / total_cost if unrealized_pnl is not None and total_cost else None
    return {
        **item,
        "total_cost": _round(total_cost),
        "current_price": _round(current_price),
        "price_date": price_date.isoformat() if price_date else None,
        "price_available": current_price is not None,
        "price_stale": bool(price_date and price_date < as_of),
        "market_value": _round(market_value),
        "unrealized_pnl": _round(unrealized_pnl),
        "unrealized_return_ratio": _round(ratio),
    }


def _aggregate(positions: list[dict], ledger: dict | None = None) -> dict:
    total_cost = sum(float(item["total_cost"]) for item in positions)
    priced = [item for item in positions if item["price_available"]]
    valuation_cost = sum(float(item["total_cost"]) for item in priced)
    market_value = sum(float(item["market_value"]) for item in priced)
    unrealized_pnl = market_value - valuation_cost
    ratio = unrealized_pnl / valuation_cost if valuation_cost else None
    return {
        "position_count": len(positions),
        "priced_position_count": len(priced),
        "missing_price_count": len(positions) - len(priced),
        "stale_price_count": sum(1 for item in priced if item["price_stale"]),
        "total_cost": _round(total_cost),
        "valuation_cost": _round(valuation_cost),
        "market_value": _round(market_value),
        "unrealized_pnl": _round(unrealized_pnl),
        "unrealized_return_ratio": _round(ratio),
        **(ledger or {}),
    }


def get_snapshot(
    repo,
    as_of: date,
    account_id: str | None = None,
    *,
    exact_price_date: bool = False,
) -> dict:
    with _LOCK:
        document = _read()
        accounts = [dict(item) for item in document["accounts"]]
        if account_id is not None:
            _account(document, account_id)
            accounts = [item for item in accounts if item.get("id") == account_id]
        trades = [
            dict(item)
            for item in document["trades"]
            if account_id is None or item.get("account_id") == account_id
        ]
    positions, ledger = _replay(trades, as_of)
    latest_prices = _latest_prices(repo, positions, as_of)
    snapshots = [
        _position_snapshot(
            item,
            as_of,
            latest_prices,
            exact_price_date=exact_price_date,
        )
        for item in positions
    ]
    account_snapshots = []
    for account in accounts:
        account_positions = [
            item for item in snapshots if item["account_id"] == account["id"]
        ]
        account_trades = [item for item in trades if item.get("account_id") == account["id"]]
        _, account_ledger = _replay(account_trades, as_of)
        account_snapshots.append(
            {
                **account,
                "positions": account_positions,
                **_aggregate(account_positions, account_ledger),
            }
        )
    return {
        "as_of": as_of.isoformat(),
        "cost_method": "fifo",
        "accounts": account_snapshots,
        **_aggregate(snapshots, ledger),
    }


def get_position_analysis_context(
    repo, account_id: str, symbol: str, as_of: date | None = None
) -> dict:
    snapshot = get_snapshot(repo, as_of or today(), account_id)
    account_snapshot = snapshot["accounts"][0]
    normalized = symbol.strip().upper()
    valued = next(
        (item for item in account_snapshot["positions"] if item["symbol"] == normalized),
        None,
    )
    if valued is None:
        raise PortfolioNotFoundError("持仓不存在")
    return {
        "source_ref": f"{account_id}:{normalized}",
        "account_id": account_id,
        "account_name": account_snapshot["name"],
        **{
            field: valued.get(field)
            for field in (
                "symbol",
                "quantity",
                "average_cost",
                "purchase_date",
                "total_cost",
                "current_price",
                "price_date",
                "market_value",
                "unrealized_pnl",
                "unrealized_return_ratio",
                "note",
            )
        },
    }


def today() -> date:
    return datetime.now(_TIMEZONE).date()


_COST_MATCH_TOLERANCE = 0.005


def _coerce_number(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _statement_match_key(
    account_id: str,
    symbol: object,
    trade_date: object,
    side: object,
    quantity: object,
    price: object,
) -> tuple:
    return (
        str(account_id),
        str(symbol).strip().upper(),
        str(trade_date),
        str(side),
        round(_coerce_number(quantity), 4),
        round(_coerce_number(price), 4),
    )


def preview_statement(repo, account_id: str, items: list[dict]) -> dict:
    """交割单候选与账本做消耗式自然键配对,不写库。

    每条候选标注 mode:insert(无匹配) / calibrate(匹配且费用不同) / skip(匹配且费用一致),
    供前端确认后走 apply_statement 落库。
    """
    del repo
    with _LOCK:
        document = _read()
        _account(document, account_id)
        existing = [
            item for item in document["trades"] if item.get("account_id") == account_id
        ]
    queues: dict[tuple, list[dict]] = {}
    for item in existing:
        key = _statement_match_key(
            account_id,
            item.get("symbol"),
            item.get("trade_date"),
            item.get("side"),
            item.get("quantity"),
            item.get("price"),
        )
        queues.setdefault(key, []).append(item)
    for queue in queues.values():
        queue.sort(key=_trade_sort_key)
    results = []
    for entry in items:
        key = _statement_match_key(
            account_id,
            entry.get("symbol"),
            entry.get("trade_date"),
            entry.get("side"),
            entry.get("quantity"),
            entry.get("price"),
        )
        queue = queues.get(key) or []
        matched = queue.pop(0) if queue else None
        row = dict(entry)
        if matched is None:
            row["mode"] = "insert"
            row["matched_trade_id"] = None
        else:
            current_fee = float(matched.get("fee") or 0)
            current_tax = float(matched.get("tax") or 0)
            same_costs = (
                abs(current_fee - float(entry.get("fee") or 0)) < _COST_MATCH_TOLERANCE
                and abs(current_tax - float(entry.get("tax") or 0)) < _COST_MATCH_TOLERANCE
            )
            row["mode"] = "skip" if same_costs else "calibrate"
            row["matched_trade_id"] = matched["id"]
            row["current_fee"] = current_fee
            row["current_tax"] = current_tax
            row["current_cost_source"] = matched.get("cost_source") or "manual"
        results.append(row)
    return {"items": results}


def apply_statement(repo, account_id: str, items: list[dict]) -> dict:
    """批量应用确认后的交割单:插入新交易并按交割单校准已有交易费用,原子落盘。"""
    inserts = [
        (index, entry) for index, entry in enumerate(items) if entry.get("mode") == "insert"
    ]
    inserts.sort(
        key=lambda pair: (
            str(pair[1]["trade_date"]),
            0 if pair[1].get("side") == "buy" else 1,
            pair[0],
        )
    )
    with _LOCK:
        document = _read()
        _account(document, account_id)
        by_id = {item["id"]: item for item in document["trades"]}
        inserted = 0
        calibrated = 0
        skipped = 0
        for entry in items:
            if entry.get("mode") != "calibrate":
                continue
            trade_id = str(entry.get("matched_trade_id") or "")
            target = by_id.get(trade_id)
            if target is None or target.get("account_id") != account_id:
                raise PortfolioNotFoundError("待校准的交易记录不存在")
            fee_value = round(float(entry["fee"]), 2)
            tax_value = round(float(entry["tax"]), 2)
            if (
                abs(float(target.get("fee") or 0) - fee_value) < _COST_MATCH_TOLERANCE
                and abs(float(target.get("tax") or 0) - tax_value) < _COST_MATCH_TOLERANCE
            ):
                skipped += 1
                continue
            target["fee"] = fee_value
            target["tax"] = tax_value
            target["cost_source"] = "calibrated"
            calibrated += 1
        for _index, entry in inserts:
            trade_date = date.fromisoformat(str(entry["trade_date"]))
            if trade_date > today():
                raise ValueError("交易日期不能晚于今天")
            side = str(entry.get("side") or "")
            if side not in {"buy", "sell"}:
                raise ValueError("交易方向必须是 buy 或 sell")
            normalized, name, asset_type = _resolve_instrument(repo, str(entry["symbol"]))
            same_day = [
                trade
                for trade in document["trades"]
                if str(trade.get("trade_date") or "") == trade_date.isoformat()
            ]
            item = {
                "id": uuid.uuid4().hex,
                "account_id": account_id,
                "symbol": normalized,
                "name": name,
                "asset_type": asset_type,
                "trade_date": trade_date.isoformat(),
                "side": side,
                "quantity": float(entry["quantity"]),
                "price": float(entry["price"]),
                "fee": round(float(entry["fee"]), 2),
                "tax": round(float(entry["tax"]), 2),
                "cost_source": "imported",
                "note": str(entry.get("note") or "").strip(),
                "created_at": _now_iso(),
                "seq": max([int(trade.get("seq") or 0) for trade in same_day] or [0]) + 1,
            }
            document["trades"].append(item)
            inserted += 1
        _validate_trades(document["trades"])
        _remove_held_watch_items(document)
        _write(document)
        return {"inserted": inserted, "calibrated": calibrated, "skipped": skipped}
