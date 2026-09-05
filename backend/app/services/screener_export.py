"""已完成的选股结果投影与文件编码, 不计算策略或修改共享缓存。"""
from __future__ import annotations

import csv
import io
import math
from datetime import date
from typing import Any

CSV_FIELDS = (
    "as_of", "strategy_id", "strategy_name", "symbol", "name",
    "close", "change_pct", "turnover_rate", "score",
)


class ExportError(ValueError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def _safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


def build_export(
    cached: dict,
    strategy_names: dict[str, str],
    strategy_ids: list[str] | None = None,
    as_of: date | None = None,
    *,
    realtime_results: dict | None = None,
) -> dict:
    """整批验证后返回快照; 缺失或跨日期结果不能伪装成完整股票清单。"""
    results = dict(cached.get("results") or {})
    for sid, live in (realtime_results or {}).items():
        # 只接受完整范围的监控结果; 同日按每个策略的完成时间比较,
        # 避免另一策略写入文件的时间掩盖本策略实时结果。
        if live.get("scope") != "all" or (as_of and live.get("as_of") != str(as_of)):
            continue
        stored = results.get(sid) or {}
        if not isinstance(stored, dict):
            stored = {}
        live_key = (live.get("as_of") or "", live.get("computed_at_ns") or 0)
        stored_key = (stored.get("as_of") or "", stored.get("computed_at_ns") or 0)
        if not stored or (as_of and stored.get("as_of") != str(as_of)) or live_key > stored_key:
            results[sid] = live
    ids = list(dict.fromkeys(strategy_ids)) if strategy_ids else [
        sid for sid in results if sid in strategy_names
    ]
    if not ids:
        raise ExportError("暂无可导出的股票策略结果, 请先运行策略", 404)
    if any(sid not in strategy_names for sid in ids):
        raise ExportError("所选策略不存在或不支持股票日线", 404)

    exported = {}
    dates = set()
    symbols: dict[str, None] = {}
    for sid in ids:
        result = results.get(sid)
        if not isinstance(result, dict):
            raise ExportError(f"策略 {sid} 尚无结果, 请先运行策略")
        # 旧缓存没有资产信息, 曾可能被 ETF 单跑覆盖; 要求重跑以确认口径。
        if result.get("asset_type") != "stock" or result.get("timeframe") != "1d":
            raise ExportError(f"策略 {sid} 的缓存缺少股票日线标记, 请重新运行策略")
        if result.get("scope") != "all":
            raise ExportError(f"策略 {sid} 的结果不是完整范围, 请不限定股票池重新运行策略")
        try:
            result_date = date.fromisoformat(str(result.get("as_of")))
        except ValueError as exc:
            raise ExportError(f"策略 {sid} 的结果日期无效, 请重新运行策略") from exc
        dates.add(result_date)
        if as_of and result_date != as_of:
            raise ExportError(f"策略 {sid} 的结果日期为 {result_date}, 与请求日期 {as_of} 不一致")
        raw_rows = result.get("rows")
        if not isinstance(raw_rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("symbol"), str)
            or not row["symbol"].strip() or any(c in row["symbol"] for c in "\r\n\t")
            for row in raw_rows
        ):
            raise ExportError(f"策略 {sid} 的结果不完整, 请重新运行策略")
        rows = _safe(raw_rows)
        for row in rows:
            symbols[row["symbol"]] = None
        exported[sid] = {
            "name": strategy_names[sid], "as_of": str(result_date),
            "total": len(rows), "rows": rows,
        }
    if len(dates) != 1:
        raise ExportError("所选策略的结果日期不一致, 请先运行同一日期的策略")
    return {
        "as_of": str(next(iter(dates))), "asset_type": "stock", "timeframe": "1d",
        "total": len(symbols), "symbols": list(symbols), "results": exported,
    }


def _csv_cell(value: Any) -> Any:
    # 外部数据可能以公式开头; 保留数值类型的负数, 只转义文本公式。
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_csv(payload: dict) -> bytes:
    """每个策略命中一行, 保留策略内顺序、原始单位及空值。"""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for sid, result in payload["results"].items():
        for row in result["rows"]:
            values = {key: _csv_cell(row.get(key)) for key in CSV_FIELDS}
            values.update(as_of=payload["as_of"], strategy_id=_csv_cell(sid),
                          strategy_name=_csv_cell(result["name"]))
            writer.writerow(values)
    return output.getvalue().encode("utf-8-sig")
