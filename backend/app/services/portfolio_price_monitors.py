"""把持仓页的止损/加仓价映射为统一价格监控规则。"""
from __future__ import annotations

import re
from pathlib import Path

from app.strategy import monitor_rules

_SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_STOP_PREFIX = "pf_stop_"
_ADD_PREFIX = "pf_add_"
_DEFAULT_COOLDOWN_SECONDS = 1200


def _rule_id(kind: str, symbol: str) -> str:
    prefix = _STOP_PREFIX if kind == "stop_loss" else _ADD_PREFIX
    return f"{prefix}{symbol.lower().replace('.', '_')}"


def _price_condition(rule: dict) -> dict | None:
    conditions = rule.get("conditions")
    if rule.get("type") != "price" or not isinstance(conditions, list) or len(conditions) != 1:
        return None
    condition = conditions[0]
    if (
        not isinstance(condition, dict)
        or condition.get("field") not in {"last_price", "close"}
        or condition.get("op") != "<="
        or not isinstance(condition.get("value"), (int, float))
    ):
        return None
    return condition


def _kind(rule: dict) -> str | None:
    rule_id = str(rule.get("id") or "")
    if rule_id.startswith(_STOP_PREFIX):
        return "stop_loss"
    if rule_id.startswith(_ADD_PREFIX):
        return "add_position"
    return None


def migrate_legacy_rules(data_dir: Path) -> int:
    """把旧持仓规则的运行时 close 字段改成显式的分时最新价字段。"""
    migrated = 0
    for rule in monitor_rules.load_all(data_dir):
        kind = _kind(rule)
        condition = _price_condition(rule)
        if kind is None or condition is None or condition.get("field") != "close":
            continue
        symbols = rule.get("symbols")
        if not isinstance(symbols, list) or len(symbols) != 1:
            continue
        symbol = str(symbols[0]).upper()
        name = str(rule.get("name") or "")
        display_name = name.split(" · ", 1)[1] if " · " in name else symbol
        price = float(condition["value"])
        rule["conditions"] = [{**condition, "field": "last_price"}]
        if kind == "stop_loss":
            rule["message"] = f"{display_name} 分时最新价已跌至止损价 {price:g}"
        else:
            rule["message"] = f"{display_name} 分时最新价已跌至加仓观察价 {price:g}"
        monitor_rules.validate(rule)
        monitor_rules.save_one(data_dir, rule)
        migrated += 1
    return migrated


def list_monitors(data_dir: Path) -> list[dict]:
    """返回持仓页拥有的价格监控。停用的止损规则仍返回并标记为待处理。"""
    by_symbol: dict[str, dict] = {}
    for rule in monitor_rules.load_all(data_dir):
        kind = _kind(rule)
        condition = _price_condition(rule)
        symbols = rule.get("symbols")
        if kind is None or condition is None or not isinstance(symbols, list) or len(symbols) != 1:
            continue
        symbol = str(symbols[0]).upper()
        item = by_symbol.setdefault(
            symbol,
            {
                "symbol": symbol,
                "name": symbol,
                "asset_type": str(rule.get("asset_type") or "stock"),
                "stop_loss_price": None,
                "stop_loss_enabled": False,
                "add_position_price": None,
                "add_position_enabled": False,
                "webhook_channels": [],
            },
        )
        name = str(rule.get("name") or "")
        if " · " in name:
            item["name"] = name.split(" · ", 1)[1]
        item["asset_type"] = str(rule.get("asset_type") or item["asset_type"])
        item[f"{kind}_price"] = float(condition["value"])
        item[f"{kind}_enabled"] = bool(rule.get("enabled", True))
        channels = rule.get("webhook_channels")
        if isinstance(channels, list):
            item["webhook_channels"] = list(channels)
    return sorted(by_symbol.values(), key=lambda item: item["symbol"])


def save_monitor(
    data_dir: Path,
    *,
    symbol: str,
    name: str,
    asset_type: str,
    stop_loss_price: float,
    add_position_price: float | None,
    webhook_channels: list[str],
) -> dict:
    normalized_symbol = symbol.strip().upper()
    if not _SYMBOL_RE.fullmatch(normalized_symbol):
        raise ValueError("证券代码必须使用标准格式: 600519.SH")
    if asset_type not in {"stock", "etf"}:
        raise ValueError("持仓价格监控只支持 A 股和场内 ETF")
    if add_position_price is not None and add_position_price <= stop_loss_price:
        raise ValueError("加仓价必须高于止损价")

    display_name = name.strip() or normalized_symbol
    base = {
        "enabled": True,
        "type": "price",
        "asset_type": asset_type,
        "scope": "symbols",
        "symbols": [normalized_symbol],
        "sector": None,
        "strategy_id": None,
        "logic": "and",
        "cooldown_seconds": _DEFAULT_COOLDOWN_SECONDS,
        "webhook_channels": list(dict.fromkeys(webhook_channels)),
    }
    stop_rule = monitor_rules.normalize(
        {
            **base,
            "id": _rule_id("stop_loss", normalized_symbol),
            "name": f"持仓止损 · {display_name}",
            "direction": "exit",
            "conditions": [
                {"field": "last_price", "op": "<=", "value": float(stop_loss_price)}
            ],
            "severity": "critical",
            "message": f"{display_name} 分时最新价已跌至止损价 {stop_loss_price:g}",
        }
    )
    add_rule = None
    if add_position_price is not None:
        add_rule = monitor_rules.normalize(
            {
                **base,
                "id": _rule_id("add_position", normalized_symbol),
                "name": f"持仓加仓 · {display_name}",
                "direction": "entry",
                "conditions": [
                    {
                        "field": "last_price",
                        "op": "<=",
                        "value": float(add_position_price),
                    }
                ],
                "severity": "warn",
                "message": (
                    f"{display_name} 分时最新价已跌至加仓观察价 {add_position_price:g}"
                ),
            }
        )

    monitor_rules.validate(stop_rule)
    if add_rule is not None:
        monitor_rules.validate(add_rule)

    for rule in (stop_rule, add_rule):
        if rule is None:
            continue
        existing = monitor_rules.load_one(data_dir, rule["id"])
        if existing and existing.get("created_at"):
            rule["created_at"] = existing["created_at"]
        monitor_rules.save_one(data_dir, rule)
    if add_rule is None:
        monitor_rules.delete_one(data_dir, _rule_id("add_position", normalized_symbol))

    return next(
        item for item in list_monitors(data_dir) if item["symbol"] == normalized_symbol
    )
