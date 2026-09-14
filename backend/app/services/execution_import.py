"""同花顺 v1 成交接收:来源身份、批次幂等和全账本原子校验。"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.services import portfolio, trade_fees


class ExecutionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_record_id: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    identity_kind: Literal["row_fingerprint"]
    executed_at: AwareDatetime
    trade_date: date
    stock_code: str = Field(pattern=r"^\d{6}$")
    stock_name: str = Field(max_length=100)
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0, allow_inf_nan=False)
    price: float = Field(gt=0, allow_inf_nan=False)
    amount: float = Field(gt=0, allow_inf_nan=False)
    contract_number: str | None = Field(default=None, max_length=200)
    order_reference: str | None = Field(default=None, max_length=200)
    fee: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tax: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("executed_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class ExecutionImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    batch_id: str = Field(min_length=1, max_length=80)
    source: Literal["tonghuashun"]
    source_account_id: str = Field(min_length=1, max_length=200)
    account_id: str = Field(min_length=1, max_length=200)
    mode: Literal["preview", "commit"]
    items: list[ExecutionItem] = Field(min_length=1, max_length=2000)

    @field_validator("batch_id")
    @classmethod
    def validate_batch_id(cls, value: str) -> str:
        uuid.UUID(value)
        return value

    @field_validator("source_account_id", "account_id")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("账户标识不能为空")
        return value


class ExecutionImportConflict(portfolio.PortfolioConflictError):
    def __init__(self, result: dict):
        super().__init__("成交导入存在冲突,请在 Trading Copilot 核对流水")
        self.result = result


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def _source_key(body: ExecutionImportRequest, entry: ExecutionItem) -> str:
    return portfolio._execution_source_key(body.source, body.source_account_id, entry.source_record_id)


def _canonical_entry(entry: ExecutionItem) -> dict:
    return {**entry.model_dump(mode="json"), "source_record_id": entry.source_record_id.lower()}


def _group(trade: dict) -> tuple:
    return trade["account_id"], trade["symbol"], trade["trade_date"]


def _natural(trade: dict) -> tuple:
    return portfolio._statement_match_key(
        trade["account_id"], trade["symbol"], trade["trade_date"],
        trade["side"], trade["quantity"], trade["price"],
    )


def _row(entry: ExecutionItem, status: str, message: str = "", trade_id=None) -> dict:
    return {"source_record_id": entry.source_record_id, "status": status,
            "trade_id": trade_id, "message": message}


def _prepare(repo, body: ExecutionImportRequest) -> list[tuple[dict | None, str]]:
    # 名称、市场与资产类别来自证券目录,不根据六位代码前缀推断。
    codes: dict[str, list[str]] = defaultdict(list)
    for symbol in repo.get_name_map():
        if portfolio._SYMBOL_RE.fullmatch(symbol) and repo.resolve_asset_type(symbol) in {"stock", "etf"}:
            codes[symbol[:6]].append(symbol)
    prepared = []
    now = datetime.now(UTC)
    for entry in body.items:
        symbols = codes.get(entry.stock_code, [])
        if len(symbols) != 1:
            prepared.append((None, "证券目录缺失或市场不唯一,请更新目录并核对证券"))
            continue
        if (entry.executed_at.astimezone(portfolio._TIMEZONE).date() != entry.trade_date
                or entry.executed_at > now or entry.trade_date > portfolio.today()):
            prepared.append((None, "成交时间与上海交易日期不一致,或成交时间晚于当前时间"))
            continue
        quantity, price, amount = map(Decimal, map(str, (entry.quantity, entry.price, entry.amount)))
        # 桌面均价最多按三位小数展示,金额以分为单位;只接受舍入能解释的差异。
        if abs(quantity * price - amount) > quantity * Decimal("0.0005") + Decimal("0.005"):
            prepared.append((None, "成交金额与数量及均价不符,可能是累计成交,请核对原始逐笔流水"))
            continue
        symbol, name, asset_type = portfolio._resolve_instrument(repo, symbols[0])
        fee, tax = trade_fees.estimate_trade_cost(
            asset_type=asset_type, symbol=symbol, side=entry.side,
            quantity=entry.quantity, price=entry.amount / entry.quantity,
        )
        prepared.append(({
            "id": uuid.uuid4().hex, "account_id": body.account_id,
            "symbol": symbol, "name": name, "asset_type": asset_type,
            "trade_date": entry.trade_date.isoformat(), "executed_at": entry.executed_at.isoformat(),
            "side": entry.side, "quantity": entry.quantity, "price": entry.price,
            "amount": entry.amount,
            "fee": fee if entry.fee is None else entry.fee,
            "tax": tax if entry.tax is None else entry.tax,
            "cost_source": "estimated" if entry.fee is None or entry.tax is None else "imported",
            "source": body.source, "source_account_id": body.source_account_id,
            "source_record_id": entry.source_record_id, "identity_kind": entry.identity_kind,
            "contract_number": entry.contract_number, "order_reference": entry.order_reference,
            "note": "", "created_at": portfolio._now_iso(),
        }, ""))
    return prepared


def receive(repo, body: ExecutionImportRequest) -> dict:
    prepared = _prepare(repo, body)
    payload = body.model_dump(mode="json", exclude={"mode", "batch_id"})
    payload["items"] = [_canonical_entry(entry) for entry in body.items]
    batch_hash = _digest(payload)
    result = {"schema_version": 1, "batch_id": body.batch_id, "mode": body.mode, "items": []}
    with portfolio.mutation_guard():
        document = portfolio._read(strict=True)
        portfolio._account(document, body.account_id)
        state = document["execution_imports"]
        prior_batch = state["batches"].get(body.batch_id)
        if prior_batch is not None and prior_batch != batch_hash:
            result["items"] = [_row(e, "conflict", "同一批次内容或目标已改变,请核对持久化批次") for e in body.items]
            raise ExecutionImportConflict(result)
        existing = document["trades"]
        by_id = {t["id"]: t for t in existing}
        keys = [_source_key(body, e) for e in body.items]
        counts = Counter(keys)
        candidates = []
        for entry, key, (trade, message) in zip(body.items, keys, prepared, strict=True):
            binding = state["bindings"].get(key)
            entry_hash = _digest(_canonical_entry(entry))
            if counts[key] > 1:
                row = _row(entry, "conflict", "批次含重复行指纹,无法区分相同字段的两笔成交")
            elif binding is not None:
                target = by_id.get(binding["trade_id"])
                if binding["account_id"] != body.account_id or binding["content_hash"] != entry_hash:
                    row = _row(entry, "conflict", "来源已绑定其他目标或成交内容已改变,请在 Trading Copilot 核对")
                elif target is None:
                    row = _row(entry, "conflict", "已导入交易已删除,来源绑定仍保留;请在 Trading Copilot 核对")
                else:
                    row = _row(entry, "duplicate", trade_id=target["id"])
            elif trade is None:
                row = _row(entry, "unsupported", message)
            else:
                row = _row(entry, "ready")
                candidates.append((trade, row, key, entry_hash))
            result["items"].append(row)

        groups: dict[tuple, list[dict]] = defaultdict(list)
        natural: dict[tuple, list[dict]] = defaultdict(list)
        contracts: dict[tuple, int] = Counter()
        seconds: dict[tuple, int] = Counter()
        for trade in [*existing, *(item[0] for item in candidates)]:
            group = _group(trade)
            groups[group].append(trade)
            natural[_natural(trade)].append(trade)
            if trade.get("executed_at"):
                seconds[(*group, trade["executed_at"][:19])] += 1
            if trade.get("source"):
                for field in ("contract_number", "order_reference"):
                    if trade.get(field):
                        contracts[(*group, trade["source"], trade["source_account_id"], field, trade[field])] += 1
        for trade, row, _key, _entry_hash in candidates:
            group = _group(trade)
            if any(not t.get("executed_at") for t in groups[group]):
                message = "同日已有缺少成交时间的手工或交割单流水,请在 Trading Copilot 核对历史与顺序;v1 不自动合并"
            elif any((t.get("source"), t.get("source_account_id")) !=
                     (trade["source"], trade["source_account_id"])
                     for t in natural[_natural(trade)]):
                message = "存在相同自然键候选,行指纹不足以证明是新成交,请核对原始逐笔流水"
            elif seconds[(*group, trade["executed_at"][:19])] > 1:
                message = "同秒成交顺序或身份不明确,请核对带独立成交编号的逐笔流水"
            elif any(trade.get(field) and contracts[(
                *group, trade["source"], trade["source_account_id"], field, trade[field],
            )] > 1 for field in ("contract_number", "order_reference")):
                message = "同合同或委托存在多行,无法排除累计成交更新;请核对逐笔成交编号"
            else:
                continue
            row.update(status="conflict", message=message)

        candidate_trades = [dict(t) for t in existing]
        next_seq = max((int(t.get("seq") or 0) for t in existing), default=0)
        for offset, (trade, _row_value, _key, _entry_hash) in enumerate(candidates, 1):
            candidate_trades.append({**trade, "seq": next_seq + offset})
        # 只重排受影响账户/证券/日期的槽位,旧手工账本的相对顺序保持不变。
        affected = {_group(t) for t, _, _, _ in candidates}
        for group in affected:
            members = [t for t in candidate_trades if _group(t) == group]
            if all(t.get("executed_at") for t in members):
                slots = sorted(t["seq"] for t in members)
                for trade, seq in zip(sorted(members, key=lambda t: t["executed_at"]), slots, strict=True):
                    trade["seq"] = seq
        if all(row["status"] in {"ready", "duplicate"} for row in result["items"]):
            try:
                portfolio._validate_trades(candidate_trades)
            except portfolio.PortfolioConflictError:
                for row in result["items"]:
                    if row["status"] == "ready":
                        row.update(status="conflict", message="全账本 FIFO 校验失败,请补齐更早买入历史并核对成交顺序;整批未写入")
        blocked = any(row["status"] not in {"ready", "duplicate"} for row in result["items"])
        if body.mode == "commit" and blocked:
            raise ExecutionImportConflict(result)
        if body.mode == "commit":
            document["trades"] = candidate_trades
            for trade, row, key, entry_hash in candidates:
                state["bindings"][key] = {
                    "trade_id": trade["id"], "account_id": body.account_id, "content_hash": entry_hash,
                    "trade_hash": portfolio._execution_trade_hash(trade),
                }
                row.update(status="inserted", trade_id=trade["id"])
            portfolio._remove_held_watch_items(document)
        # preview 仅保留批次摘要,约束后续 commit/重试;不预留交易身份。
        state["batches"][body.batch_id] = batch_hash
        portfolio._write(document)
    return result
