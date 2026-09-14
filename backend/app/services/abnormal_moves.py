"""异动边缘统计 — 按交易所异动规则口径实时计算个股接近度。

规则 (滚动估算口径, 与交易所《交易规则》的异常波动/严重异常波动披露阈值对齐;
主板/科创板条款号指上交所《交易规则(2026年修订)》, 2026-07-06 施行):
- 主板:     连续3日收盘价涨跌幅偏离值累计 ±20% (5.4.2)
- 创业板/科创板: 3日 ±30% (科创板 6.10)
- 北交所:   3日 ±40%; 10日 +150%(-60%); 30日 +300%(-75%)
- 沪深严重异常波动 (5.4.3/6.11): 10日累计偏离 +100%(-50%),
  30日 +200%(-70%)。北交所使用上述独立阈值。
  「10日内多次同向异常波动」情形需事件计数, 当前尚未实现;
  北交所现行规则为 10 日内 3 次同向。
- 风险警示 (ST/*ST): 2026-07-06 起主板风险警示股票涨跌幅限制调整为 10%,
  异常波动特别规定 (原 3日±15% / 10日+50% / 30日+100%) 同步废止,
  与主板普通股票适用同一套标准 (见 price_limits.MAIN_BOARD_ST_LIMIT_CHANGE_DATE)。

偏离值 = 个股 N 日累计涨跌幅 - 对应指数同期涨跌幅 (enriched 运行时列 deviate_Nd)。
「接近度」= |实时偏离| / 该方向阈值: ≥1 滚动估算达线, ≥0.7 边缘, ≥0.5 观察。
本模块没有异常公告后的重置事件、无涨跌幅限制期等完整监管事件数据，因此
任何数值都不表示交易所已经认定或必须披露；达线结果统一标为 estimate。
盘中实时叠加: 历史偏离 (已完成交易日) + 今日实时涨跌 - 基准指数今日涨跌。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import polars as pl

from app.indicators.pipeline import _BENCHMARK_PREFERENCE, DEVIATION_WINDOWS
from app.market_time import cn_today

# ── 规则表 ────────────────────────────────────────────────

@dataclass(frozen=True)
class AbnormalRule:
    board: str
    st: bool
    # 各窗口阈值 (小数): {窗口: (正向, 负向)} — 严重异动负向阈值更严 (见模块 docstring)
    thresholds: dict[int, tuple[float, float]]


# 3 日异常波动阈值各板块对称; 10/30 日严重异动按交易所独立口径。
_MAIN = {3: (0.20, 0.20), 10: (1.00, 0.50), 30: (2.00, 0.70)}
_GEM_STAR = {3: (0.30, 0.30), 10: (1.00, 0.50), 30: (2.00, 0.70)}
_BSE = {3: (0.40, 0.40), 10: (1.50, 0.60), 30: (3.00, 0.75)}

RULES_META: list[dict[str, Any]] = [
    {"board": "主板", "st": False, "thresholds": {f"{k}d": {"up": u, "down": d} for k, (u, d) in _MAIN.items()},
     "note": "3日±20% 异常波动; 严重异常波动 10日+100%(-50%) / 30日+200%(-70%), "
             "负向更严; 2026-07-06 起风险警示(ST)股票同口径 (原±15%特别规定已废止)"},
    {"board": "创业板/科创板", "st": False, "thresholds": {f"{k}d": {"up": u, "down": d} for k, (u, d) in _GEM_STAR.items()},
     "note": "20%涨跌幅板块, 3日±30%"},
    {"board": "北交所", "st": False, "thresholds": {f"{k}d": {"up": u, "down": d} for k, (u, d) in _BSE.items()},
     "note": "30%涨跌幅板块, 3日±40%; 10日+150%(-60%) / 30日+300%(-75%); "
             "10日内3次同向异常波动的事件计数口径尚未实现"},
]

def board_of(symbol: str) -> str:
    """按代码前缀判定板块。"""
    code = symbol.split(".")[0]
    if symbol.endswith(".BJ") or code[:2] in {"43", "83", "87", "92"}:
        return "北交所"
    if code.startswith("68"):
        return "科创板"
    if code.startswith(("30", "301")):
        return "创业板"
    return "主板"


def is_st_name(name: str | None) -> bool:
    return bool(name) and "ST" in str(name).upper()


def rule_for(symbol: str, name: str | None) -> AbnormalRule:
    board = board_of(symbol)
    st = is_st_name(name)
    # 主板风险警示股票 2026-07-06 起与普通股票同标准 (涨跌幅 10%,
    # 异常波动特别规定废止); st 仅为展示标记。创业板/科创板/北交所本就不区分。
    if board == "北交所":
        return AbnormalRule(board, st, _BSE)
    if board in ("创业板", "科创板"):
        return AbnormalRule(board, st, _GEM_STAR)
    return AbnormalRule(board, st, _MAIN)


# ── 快照计算 ──────────────────────────────────────────────

_hist_cache_lock = threading.Lock()
_hist_cache: dict[str, Any] = {}
_hist_cache_generation = 0
_HIST_CACHE_TTL = 60.0

_STATUS_ESTIMATE = "estimate"
_STATUS_EDGE = "edge"
_STATUS_WATCH = "watch"


def invalidate_abnormal_moves_cache() -> None:
    """清除异动历史快照缓存。"""
    global _hist_cache_generation
    with _hist_cache_lock:
        _hist_cache_generation += 1
        _hist_cache.clear()


def _status_of(closeness: float) -> str:
    if closeness >= 1.0:
        return _STATUS_ESTIMATE
    if closeness >= 0.7:
        return _STATUS_EDGE
    return _STATUS_WATCH


def _hist_snapshot(repo: Any) -> dict[str, Any]:
    """enriched 最新日偏离快照；仅缓存已经完成的历史交易日。"""
    while True:
        now = time.monotonic()
        today_iso = cn_today().isoformat()
        with _hist_cache_lock:
            generation = _hist_cache_generation
            cached = _hist_cache.get("data")
            if (
                cached is not None
                and cached.get("cache_date") is not None
                and cached["cache_date"] < today_iso
                and now - cached["_ts"] < _HIST_CACHE_TTL
            ):
                return cached

        df, cache_date = repo.get_enriched_latest()
        rows: dict[str, dict[str, Any]] = {}
        if not df.is_empty() and "symbol" in df.columns:
            symbols = [str(symbol) for symbol in df["symbol"].to_list()]
            try:
                name_map = repo.get_name_map(symbols)
            except Exception:
                name_map = {}
            cols = ["symbol", *[c for c in ("name", "close", "change_pct",
                                            "deviate_3d", "deviate_10d", "deviate_30d") if c in df.columns]]
            df = df.select(cols)
            for r in df.iter_rows(named=True):
                symbol = str(r["symbol"])
                rows[symbol] = {
                    "name": r.get("name") or name_map.get(symbol),
                    "close": r.get("close"),
                    "rt_pct": r.get("change_pct"),
                    "deviate_3d": r.get("deviate_3d"),
                    "deviate_10d": r.get("deviate_10d"),
                    "deviate_30d": r.get("deviate_30d"),
                }
        cache_date_iso = cache_date.isoformat() if cache_date else None
        payload = {"_ts": now, "rows": rows, "cache_date": cache_date_iso}
        with _hist_cache_lock:
            if generation != _hist_cache_generation:
                continue
            if cache_date_iso is not None and cache_date_iso < today_iso:
                _hist_cache["data"] = payload
            else:
                _hist_cache.pop("data", None)
            return payload


def _row_change_pct(row: dict[str, Any], *, percent_value: bool) -> float | None:
    """从标准化行情行读取涨跌幅, 优先用价格比值消除单位歧义。"""
    close = row.get("close")
    prev_close = row.get("prev_close")
    if close is not None and prev_close not in (None, 0):
        return float(close / prev_close - 1)
    for col in ("change_pct", "pct", "pct_change"):
        value = row.get(col)
        if value is not None:
            pct = float(value)
            return pct / 100.0 if percent_value else pct
    return None


def _bench_rt_pcts(quote_service: Any) -> dict[str, float | None]:
    """按交易所读取今日基准指数涨跌幅, 缺数据时保留不可用状态。"""
    try:
        df = quote_service.get_index_quotes()
    except Exception:
        return {exchange: None for exchange in _BENCHMARK_PREFERENCE}
    if df is None or df.is_empty():
        return {exchange: None for exchange in _BENCHMARK_PREFERENCE}

    by_symbol = {str(row["symbol"]): row for row in df.iter_rows(named=True)}
    result: dict[str, float | None] = {}
    for exchange, candidates in _BENCHMARK_PREFERENCE.items():
        value = None
        for symbol in candidates:
            row = by_symbol.get(symbol)
            if row is None:
                continue
            # QuoteService 的指数缓存 change_pct 为百分数值, 股票 enriched 则为小数制。
            value = _row_change_pct(row, percent_value=True)
            if value is not None:
                break
        result[exchange] = value
    return result


def _bench_rt_pct(quote_service: Any) -> float | None:
    """兼容旧调用方的单基准摘要；逐标的计算仍使用交易所映射。"""
    for value in _bench_rt_pcts(quote_service).values():
        if value is not None:
            return value
    return None


def _today_stock_rows(quote_service: Any) -> dict[str, dict[str, Any]]:
    """读取当日标准化个股行情与滚动偏离值。"""
    try:
        frame, quote_date = quote_service.get_enriched_today()
    except Exception:
        return {}
    if quote_date != cn_today() or frame is None or frame.is_empty() or "symbol" not in frame.columns:
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for row in frame.iter_rows(named=True):
        rows[str(row["symbol"])] = {
            "close": row.get("close"),
            "rt_pct": _row_change_pct(row, percent_value=False),
            **{
                f"deviate_{window}d": row.get(f"deviate_{window}d")
                for window in DEVIATION_WINDOWS
            },
        }
    return rows


def build_overview(
    repo: Any,
    quote_service: Any = None,
    *,
    min_closeness: float = 0.5,
    limit: int = 200,
) -> dict[str, Any]:
    """返回异动边缘总览: 规则表 + 按接近度排序的个股列表。"""
    hist = _hist_snapshot(repo)
    cache_date = hist.get("cache_date")
    hist_rows: dict[str, dict[str, Any]] = hist["rows"]

    # enriched 已含今日收盘 (盘后已同步) 时, 今日涨跌已计入历史偏离, 不再叠加
    includes_today = cache_date is not None and cache_date >= cn_today().isoformat()
    bench_rt_by_exchange = (
        _bench_rt_pcts(quote_service)
        if quote_service is not None
        else {exchange: None for exchange in _BENCHMARK_PREFERENCE}
    )
    today_stock_rows = (
        _today_stock_rows(quote_service)
        if quote_service is not None and not includes_today
        else {}
    )

    out_rows: list[dict[str, Any]] = []
    for symbol, base in hist_rows.items():
        rule = rule_for(symbol, base.get("name"))
        if includes_today:
            rt_pct = base.get("rt_pct")
            current = base
            close = base.get("close")
        else:
            current = today_stock_rows.get(symbol)
            if current is None:
                # 历史分区的指标属于旧交易日, 不能冒充当日滚动窗口。
                continue
            rt_pct = current.get("rt_pct")
            close = current.get("close") or base.get("close")

        windows: dict[str, dict[str, Any]] = {}
        max_closeness = 0.0
        for n in DEVIATION_WINDOWS:
            live = current.get(f"deviate_{n}d")
            if live is None:
                continue
            up_t, down_t = rule.thresholds[n]
            threshold = up_t if live >= 0 else down_t
            closeness = abs(live) / threshold if threshold > 0 else 0.0
            windows[f"{n}d"] = {
                "value": round(live, 4),
                "threshold": threshold,
                "closeness": round(closeness, 4),
            }
            max_closeness = max(max_closeness, closeness)
        if not windows or max_closeness < min_closeness:
            continue
        out_rows.append({
            "symbol": symbol,
            "name": base.get("name"),
            "board": rule.board,
            "st": rule.st,
            "close": close,
            "rt_pct": rt_pct,
            "windows": windows,
            "max_closeness": round(max_closeness, 4),
            "status": _status_of(max_closeness),
            "determination": "rolling_estimate",
        })

    out_rows.sort(key=lambda r: r["max_closeness"], reverse=True)
    counts = {
        _STATUS_ESTIMATE: sum(1 for r in out_rows if r["status"] == _STATUS_ESTIMATE),
        _STATUS_EDGE: sum(1 for r in out_rows if r["status"] == _STATUS_EDGE),
        _STATUS_WATCH: sum(1 for r in out_rows if r["status"] == _STATUS_WATCH),
    }
    available_bench_rt_pcts = [
        value for value in bench_rt_by_exchange.values() if value is not None
    ]
    return {
        "asof": time.time(),
        "cache_date": cache_date,
        # 保留既有标量响应契约, 仅用于页面摘要; 逐标的计算使用上面的交易所映射。
        "bench_rt_pct": (
            round(sum(available_bench_rt_pcts) / len(available_bench_rt_pcts), 4)
            if available_bench_rt_pcts
            else None
        ),
        "includes_today": includes_today,
        "calculation_scope": "rolling_estimate",
        "rules": RULES_META,
        "counts": counts,
        "rows": out_rows[:limit],
    }


# ================================================================
# 盘中异动 (量价信号聚合, 异动监控「盘中」tab)
#
# 数据源: enriched 最新快照的当日消息号列 (零新增采集):
# 涨停/跌停/跌停翘板/炸板/放量(量比≥2)/创60日新高/新低。
# 行序 = 信号优先级 (涨停 > 炸板 > 翘板 > 跌停 > 新高 > 新低 > 放量),
# 同级按 |今日涨跌| 降序; counts 供前端筛选 chips 展示各类型数量。
# ================================================================

_INTRADAY_SIGNALS: tuple[tuple[str, str], ...] = (
    ("signal_limit_up", "limit_up"),
    ("signal_broken_limit_up", "broken"),
    ("signal_limit_down_recovery", "recovery"),
    ("signal_limit_down", "limit_down"),
    ("signal_n_day_high", "new_high"),
    ("signal_n_day_low", "new_low"),
    ("signal_volume_surge", "volume_surge"),
)
_INTRADAY_PRIORITY = {key: i for i, (_, key) in enumerate(_INTRADAY_SIGNALS)}
_INTRADAY_COLS = ("symbol", "name", "close", "change_pct", "amplitude",
                  "vol_ratio_5d", "turnover_rate", "consecutive_limit_ups")


def build_intraday(repo: Any, limit: int = 500) -> dict[str, Any]:
    """enriched 最新快照 → 当日异动信号命中行 (含各类型计数)。"""
    df, cache_date = repo.get_enriched_latest()
    empty = {"cache_date": cache_date.isoformat() if cache_date else None,
             "counts": {}, "rows": []}
    if df.is_empty() or "symbol" not in df.columns:
        return empty
    present = [(c, k) for c, k in _INTRADAY_SIGNALS if c in df.columns]
    if not present:
        return empty

    hits = df.filter(pl.any_horizontal([pl.col(c).fill_null(False) for c, _ in present]))
    if hits.is_empty():
        return empty
    counts = {k: int(hits[c].fill_null(False).sum()) for c, k in present}

    sig_cols = {k: hits[c].fill_null(False).to_list() for c, k in present}
    base_cols = [c for c in _INTRADAY_COLS if c in hits.columns]
    base = hits.select(base_cols).to_dicts()
    rows: list[dict[str, Any]] = []
    for i, r in enumerate(base):
        signals = [k for k, flags in sig_cols.items() if flags[i]]
        rows.append({
            **{c: r.get(c) for c in base_cols},
            "signals": signals,
            "_prio": min((_INTRADAY_PRIORITY[s] for s in signals), default=99),
        })
    rows.sort(key=lambda r: (r["_prio"], -abs(r.get("change_pct") or 0.0)))
    for r in rows:
        r.pop("_prio", None)
    return {"cache_date": cache_date.isoformat() if cache_date else None,
            "counts": counts, "rows": rows[:limit]}
