"""券商交割单(CSV/Excel)解析为规范化成交记录。

主流券商导出的列名差异较大,这里按关键词做角色映射:
费用(fee) = 佣金/手续费 + 过户费 + 其他收费,税费(tax) = 印花税,与实盘记账口径一致。
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import polars as pl

from app.config import settings
from app.services.ext_data import ensure_utf8_csv
from app.services.watchlist_ocr.pipeline import build_instrument_lookups

_COLUMN_ROLES: dict[str, tuple[str, ...]] = {
    "trade_date": ("成交日期", "交易日期", "日期"),
    "code": ("证券代码", "代码", "股票代码"),
    "name": ("证券名称", "名称", "股票名称"),
    "side": ("买卖方向", "委托方向", "业务名称", "操作", "摘要", "方向"),
    "quantity": ("成交数量", "股份数量", "股票数量", "数量"),
    "price": ("成交均价", "成交价格", "成交价", "价格"),
    "commission": ("佣金及手续费", "佣金", "手续费"),
    "stamp_tax": ("印花税", "税金"),
    "transfer_fee": ("过户费",),
    "other_fee": ("其他费用", "其他费", "规费", "结算费", "经手费"),
}

_FEE_ROLES = ("commission", "transfer_fee", "other_fee")

_BUY_PATTERN = re.compile(r"买|buy", re.IGNORECASE)
_SELL_PATTERN = re.compile(r"卖|sell", re.IGNORECASE)
_DATE_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


class StatementParseError(ValueError):
    """交割单内容无法识别为成交记录。"""


def _resolve_columns(columns: list[str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    cleaned = {str(col).strip(): col for col in columns}
    for role, keywords in _COLUMN_ROLES.items():
        for keyword in keywords:
            if keyword in cleaned:
                resolved[role] = cleaned[keyword]
                break
        else:
            for text, original in cleaned.items():
                if any(keyword in text for keyword in keywords):
                    resolved[role] = original
                    break
    return resolved


def _parse_date(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return None
    text = text.split("T")[0].split(" ")[0].replace("/", "-").replace(".", "-")
    compact = _DATE_COMPACT.fullmatch(text)
    if compact:
        text = f"{compact.group(1)}-{compact.group(2)}-{compact.group(3)}"
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _parse_number(value: object) -> float | None:
    text = str(value or "").strip().replace(",", "").replace("，", "")  # noqa: RUF001 兼容中文全角千分位
    if not text or text.lower() in {"nan", "none", "--", "-"}:
        return None
    try:
        return abs(float(text))
    except ValueError:
        return None


def _parse_side(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if _BUY_PATTERN.search(text):
        return "buy"
    if _SELL_PATTERN.search(text):
        return "sell"
    return None


def parse_statement_file(file_path: Path) -> dict:
    """解析交割单文件,返回规范化成交列表与识别缺口。

    返回 {"items": [...], "skipped_rows": [{"row": 行号, "reason": 原因}]}。
    items 中 symbol 可能为 None(主数据缺失),由上层决定拒绝或跳过。
    """
    suffix = file_path.suffix.lower()
    if suffix == ".csv":
        frame = pl.read_csv(ensure_utf8_csv(file_path), infer_schema_length=10000)
    elif suffix in (".xlsx", ".xls"):
        frame = pl.read_excel(file_path)
    else:
        raise StatementParseError(f"不支持的交割单格式: {suffix or '(无后缀)'}")
    if frame.is_empty():
        raise StatementParseError("交割单为空")

    columns = _resolve_columns([str(col) for col in frame.columns])
    missing = [role for role in ("trade_date", "code", "side", "quantity", "price") if role not in columns]
    if missing:
        raise StatementParseError(
            "无法识别的交割单列结构,缺少: " + ", ".join(missing) + f"(文件列: {frame.columns})"
        )

    code_to_symbol, symbol_to_name = build_instrument_lookups(settings.data_dir)

    items: list[dict] = []
    skipped: list[dict] = []
    for index, row in enumerate(frame.iter_rows(named=True)):
        row_no = index + 2
        trade_date = _parse_date(row.get(columns["trade_date"]))
        code = str(row.get(columns["code"]) or "").strip()
        code = code.split(".")[0].strip()
        side = _parse_side(row.get(columns["side"]))
        quantity = _parse_number(row.get(columns["quantity"]))
        price = _parse_number(row.get(columns["price"]))
        if trade_date is None and code in {"", "nan", "None"}:
            continue
        if trade_date is None or len(code) != 6 or not code.isdigit() or side is None:
            skipped.append({"row": row_no, "reason": "日期/代码/方向无法识别"})
            continue
        if not quantity or quantity <= 0 or price is None or price < 0:
            skipped.append({"row": row_no, "reason": "数量或价格无效"})
            continue
        fee = 0.0
        for role in _FEE_ROLES:
            column = columns.get(role)
            if column:
                fee += _parse_number(row.get(column)) or 0.0
        tax_column = columns.get("stamp_tax")
        tax = _parse_number(row.get(tax_column)) if tax_column is not None else None
        symbol = code_to_symbol.get(code)
        items.append(
            {
                "symbol": symbol,
                "raw_code": code,
                "name": (symbol_to_name.get(symbol) if symbol else None)
                or str(row.get(columns.get("name", "")) or "").strip()
                or code,
                "trade_date": trade_date,
                "side": side,
                "quantity": quantity,
                "price": price,
                "fee": round(fee, 2),
                "tax": round(tax or 0.0, 2),
            }
        )
    if not items:
        raise StatementParseError("未解析到任何有效成交记录")
    return {"items": items, "skipped_rows": skipped}
