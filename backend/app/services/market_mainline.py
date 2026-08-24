"""市场主线(板块/概念)识别 — 基于涨停梯队的历史聚合。

用户判据的量化: 主升阶段的主线 = 同一概念内涨停家数多、最高连板高、
梯队档位填得满(2 板到最高板之间不断层)。对每个交易日按概念聚合涨停梯队,
截面 rank 归一后加权成主线分, 持久化为日频时序, 供市场环境页展示
"什么阶段走什么主升"。

口径限制(重要): 历史主线只使用带日期的 timeseries 概念/行业成分快照，按交易日
选择当时最新可用版本。没有 point-in-time 版本的 snapshot 数据不参与历史计算，
避免把当前成分回填到过去。MEMBERSHIP_NOTE 随 API 返回给前端展示。

性能: 全量回填只窄扫 enriched 的 4 列并先过滤连板 >=1(全历史 ~10 万行),
join 概念映射后 group_by, 峰值内存 <100MB。
"""
from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path

import polars as pl

from app.services.ext_data import ExtConfigStore
from app.services.market_overview_builder import (
    _dimension_field,
    _dimension_values,
    _ext_files,
    _symbol_keys,
)

logger = logging.getLogger(__name__)

MEMBERSHIP_NOTE = (
    "历史主线仅使用带日期的成分快照，并按交易日选择当时最新可用版本；"
    "早于首个版本的日期不计算"
)

MAINLINE_DIR = "mainline_history"
_TOP_PER_DAY = 30          # 每日持久化的主线数(按分数截断)
_INDUSTRY_LEVEL = 2        # 行业主线取前两级(如 计算机-软件开发)
_MIN_LIMIT_UP = 3          # 单概念当日最少涨停家数(低于此不参与排名)

# 主线分权重: 概念内涨停家数 / 最高连板 / 梯队档位数 / 二板以上家数
_SCORE_WEIGHTS = {
    "limit_up_count": 0.35,
    "max_boards": 0.25,
    "rungs_filled": 0.25,
    "ge2_count": 0.15,
}


def _resolve_filter_config(filter_cfg: dict | None) -> dict:
    """解析过滤配置; None 时读用户偏好(宽基/风格标签过滤, 见 preferences 文档)。"""
    if filter_cfg is not None:
        return {
            "min_members": int(filter_cfg.get("min_members", 4)),
            "max_members": int(filter_cfg.get("max_members", 600)),
            "blacklist": {str(x) for x in filter_cfg.get("blacklist") or []},
        }
    try:
        from app.services import preferences

        cfg = preferences.get_mainline_filter_config()
        return {
            "min_members": int(cfg["min_members"]),
            "max_members": int(cfg["max_members"]),
            "blacklist": set(cfg["blacklist"]),
        }
    except Exception:
        return {"min_members": 4, "max_members": 600, "blacklist": set()}


def mainline_path(data_dir: Path) -> Path:
    return data_dir / MAINLINE_DIR / "part.parquet"


_ST_SYMBOLS_CACHE: tuple[float, frozenset[str]] | None = None


def load_risk_warning_symbols(data_dir: Path) -> frozenset[str]:
    """当前维表快照中名称含 ST 标记的 symbol 集合(大写), 供主线/情绪统计剔除。

    判定与 indicators 涨跌停口径共用同一权威实现(price_limits.polars_is_risk_warning_name,
    即名称含 "ST", 覆盖 ST/*ST/S*ST)。维表是快照无历史版本, 与概念成分同样的
    回看限制。600s 进程内缓存(维表 snapshot 进程内不变)。
    """
    global _ST_SYMBOLS_CACHE
    now = time.time()
    if _ST_SYMBOLS_CACHE is not None and now - _ST_SYMBOLS_CACHE[0] < 600:
        return _ST_SYMBOLS_CACHE[1]
    from app.price_limits import polars_is_risk_warning_name

    syms: frozenset[str] = frozenset()
    inst_dir = data_dir / "instruments"
    if inst_dir.exists():
        try:
            df = pl.read_parquet(inst_dir / "**" / "*.parquet").select(["symbol", "name"])
            st = df.filter(polars_is_risk_warning_name(pl.col("name")))
            syms = frozenset(s.upper() for s in st["symbol"].to_list())
        except Exception as e:
            logger.warning("load risk-warning symbols failed: %s", e)
    _ST_SYMBOLS_CACHE = (now, syms)
    return syms


def load_mainline_history(data_dir: Path, kind: str = "concept") -> pl.DataFrame:
    """读取主线时序(全部 kind), 不存在返回空 DataFrame。"""
    p = mainline_path(data_dir)
    if not p.exists():
        return pl.DataFrame()
    try:
        df = pl.read_parquet(p)
    except Exception as e:
        logger.warning("load_mainline_history failed: %s", e)
        return pl.DataFrame()
    if df.is_empty() or "kind" not in df.columns:
        return df
    return df.filter(pl.col("kind") == kind)


def _industry_member(member: str, kind: str) -> str:
    """行业维度取前 _INDUSTRY_LEVEL 级; 概念原样返回。"""
    if kind != "industry":
        return member
    return "-".join(member.split("-")[:_INDUSTRY_LEVEL])


def _load_point_in_time_map_df(repo, kind: str) -> pl.DataFrame:
    """加载带生效日期的概念/行业成分版本；snapshot 数据一律 fail-closed。"""
    data_dir = repo.store.data_dir
    pairs: list[tuple[str, date, str, str]] = []
    for config in ExtConfigStore(data_dir).load_all():
        if config.mode != "timeseries":
            continue
        field = _dimension_field(config, kind)
        if not field:
            continue
        files = _ext_files(data_dir, config)
        if not files:
            continue
        try:
            frame = pl.read_parquet(files, hive_partitioning=True)
        except TypeError:
            frame = pl.read_parquet(files)
        except Exception as exc:  # noqa: BLE001
            logger.warning("load point-in-time %s membership failed: %s", kind, exc)
            continue
        if frame.is_empty() or field not in frame.columns or "date" not in frame.columns:
            continue
        for row in frame.to_dicts():
            effective = row.get("date")
            if not isinstance(effective, date):
                try:
                    effective = date.fromisoformat(str(effective))
                except (TypeError, ValueError):
                    continue
            members = _dimension_values(row.get(field))
            if not members:
                continue
            for symbol in _symbol_keys(row, config):
                for member in members:
                    pairs.append((config.id, effective, symbol, member))
    if not pairs:
        return pl.DataFrame(schema={
            "_source_id": pl.Utf8,
            "_effective_date": pl.Date,
            "_sym_up": pl.Utf8,
            kind: pl.Utf8,
        })
    return pl.DataFrame({
        "_source_id": [item[0] for item in pairs],
        "_effective_date": [item[1] for item in pairs],
        "_sym_up": [item[2] for item in pairs],
        kind: [item[3] for item in pairs],
    }).unique()


def _join_point_in_time_membership(
    limit_rows: pl.DataFrame,
    map_df: pl.DataFrame,
    kind: str,
) -> pl.DataFrame:
    """各来源独立按交易日选择最近成分快照，再合并去重。"""
    parts: list[pl.DataFrame] = []
    for source in map_df["_source_id"].unique().to_list():
        source_map = map_df.filter(pl.col("_source_id") == source)
        versions = sorted(source_map["_effective_date"].unique().to_list())
        for index, effective in enumerate(versions):
            next_effective = versions[index + 1] if index + 1 < len(versions) else None
            rows = limit_rows.filter(pl.col("date") >= effective)
            if next_effective is not None:
                rows = rows.filter(pl.col("date") < next_effective)
            if rows.is_empty():
                continue
            membership = source_map.filter(
                pl.col("_effective_date") == effective
            ).drop(["_source_id", "_effective_date"])
            joined = rows.join(membership, on="_sym_up", how="inner")
            if not joined.is_empty():
                parts.append(joined)
    if not parts:
        return pl.DataFrame()
    return pl.concat(parts, how="vertical_relaxed").unique(
        subset=["date", "_sym_up", kind],
        maintain_order=True,
    )


def compute_mainline_range(repo, data_dir: Path, start: date, end: date,
                           kind: str = "concept",
                           filter_cfg: dict | None = None,
                           exclude_st: bool | None = None) -> pl.DataFrame:
    """计算 [start, end] 每日主线排行(按 _SCORE_WEIGHTS 加权截面分)。

    filter_cfg: {"min_members", "max_members", "blacklist"}; None 时读用户偏好。
    宽基/风格标签(融资融券/沪深股通等数千成分)按成员数上限过滤,
    用户黑名单按名称过滤(不论大小)。修改配置后重算主线生效。
    exclude_st: 为兼容既有调用保留。历史 ST 状态没有 point-in-time 版本，故历史
    主线不执行当前名称快照过滤；待引入带日期的风险警示状态后再启用。

    返回列: date, kind, member, limit_up_count, ge2_count, max_boards,
    boards_sum, rungs_filled, leader_symbol, score, rank。空数据返回空表。
    """
    if start > end:
        return pl.DataFrame()
    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    if not enriched_dir.exists():
        return pl.DataFrame()

    map_df = _load_point_in_time_map_df(repo, kind)
    if map_df.is_empty():
        return pl.DataFrame()

    cfg = _resolve_filter_config(filter_cfg)
    if cfg["min_members"] > 1 or cfg["max_members"] < 5000 or cfg["blacklist"]:
        member_counts = (
            map_df.group_by(["_source_id", "_effective_date", kind])
            .len()
            .rename({"len": "_members"})
        )
        member_counts = member_counts.filter(
            pl.col("_members").ge(cfg["min_members"])
            & pl.col("_members").le(cfg["max_members"])
            & ~pl.col(kind).is_in(sorted(cfg["blacklist"]))
        )
        allowed = member_counts.select(["_source_id", "_effective_date", kind])
        map_df = map_df.join(
            allowed,
            on=["_source_id", "_effective_date", kind],
            how="semi",
        )
        if map_df.is_empty():
            return pl.DataFrame()

    limit_rows = (
        pl.scan_parquet(enriched_dir / "**" / "*.parquet")
        .select(["date", "symbol", "consecutive_limit_ups", "amount"])
        .filter(
            (pl.col("date") >= start) & (pl.col("date") <= end)
            & (pl.col("consecutive_limit_ups") >= 1)
        )
        .collect()
    )
    if limit_rows.is_empty():
        return pl.DataFrame()

    limit_rows = limit_rows.with_columns(pl.col("symbol").str.to_uppercase().alias("_sym_up"))

    # 当前 instruments 名称没有历史生效日期，不得据此回看过滤过去的 ST 状态。
    # exclude_st 参数保留兼容，历史口径在有 point-in-time 状态源前保持不滤。
    joined = _join_point_in_time_membership(limit_rows, map_df, kind)
    if joined.is_empty():
        return pl.DataFrame()
    joined = joined.with_columns(
        pl.col(kind).map_elements(
            lambda m: _industry_member(str(m), kind),
            return_dtype=pl.Utf8,
        ).alias("member")
    )

    agg = (
        joined.group_by(["date", "member"])
        .agg(
            pl.len().alias("limit_up_count"),
            (pl.col("consecutive_limit_ups") >= 2).sum().alias("ge2_count"),
            pl.col("consecutive_limit_ups").max().alias("max_boards"),
            pl.col("consecutive_limit_ups").sum().alias("boards_sum"),
            pl.col("consecutive_limit_ups")
              .filter(pl.col("consecutive_limit_ups") >= 2)
              .n_unique()
              .alias("rungs_filled"),
            pl.col("symbol")
              .sort_by(
                  pl.col("consecutive_limit_ups"), pl.col("amount"),
                  descending=[True, True],
              )
              .first()
              .alias("leader_symbol"),
        )
    )

    # 截面 rank 归一(0-1) → 加权主线分(0-100)。分母 max(n-1,1) 保证单概念日不除零。
    agg = agg.filter(pl.col("limit_up_count") >= _MIN_LIMIT_UP)
    norm_exprs = []
    for col in _SCORE_WEIGHTS:
        norm_exprs.append(
            ((pl.col(col).rank(method="average") - 1.0)
             / pl.max_horizontal(pl.len().over("date") - 1, 1)).over("date").alias(f"_{col}_r")
        )
    agg = agg.with_columns(norm_exprs)
    agg = agg.with_columns(
        (
            100.0 * sum(
                _SCORE_WEIGHTS[col] * pl.col(f"_{col}_r") for col in _SCORE_WEIGHTS
            )
        ).alias("score")
    )
    agg = agg.with_columns(
        pl.col("score").rank(method="ordinal", descending=True).over("date").alias("rank")
    )
    result = (
        agg.filter(pl.col("rank") <= _TOP_PER_DAY)
        .drop([f"_{col}_r" for col in _SCORE_WEIGHTS])
        .with_columns(pl.lit(kind).alias("kind"))
        .select([
            "date", "kind", "member", "limit_up_count", "ge2_count",
            "max_boards", "boards_sum", "rungs_filled", "leader_symbol",
            "score", "rank",
        ])
        .sort(["date", "rank"])
    )
    return result


def upsert_mainline_history(data_dir: Path, new_rows: pl.DataFrame) -> None:
    """按 (date, kind) 整日覆盖 upsert; schema 以 new_rows 为权威(同 regime 模式)。"""
    if new_rows.is_empty() or "date" not in new_rows.columns:
        return
    p = mainline_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    old = pl.read_parquet(p) if p.exists() else pl.DataFrame()
    if old.is_empty():
        combined = new_rows
    else:
        # 按 (date, kind) 整日覆盖: anti-join 掉本次重算的 (日, 维度) 组合
        kept = old.join(
            new_rows.select(["date", "kind"]).unique(),
            on=["date", "kind"],
            how="anti",
        )
        target_cols = new_rows.columns
        keep_exprs = [
            pl.col(c) if c in kept.columns else pl.lit(None).alias(c)
            for c in target_cols
        ]
        kept = kept.select(keep_exprs)
        combined = pl.concat([kept, new_rows.select(target_cols)], how="vertical_relaxed")
    combined = combined.sort(["date", "kind", "rank"])
    combined.write_parquet(p)


def compute_mainline_incremental(repo, data_dir: Path, *, today: date | None = None,
                                 kind: str = "concept") -> pl.DataFrame:
    """增量补算主线(供 daily_pipeline / 手动触发): 补 enriched 已有而主线缺失的日。"""
    today = today or date.today()
    from app.services.regime_builder import enriched_date_set

    enriched_dates = enriched_date_set(repo)
    existing = load_mainline_history(data_dir, kind)
    existing_dates = set(existing["date"].to_list()) if not existing.is_empty() else set()
    missing = sorted(d for d in enriched_dates if d not in existing_dates and d <= today)
    if not missing:
        return pl.DataFrame()
    logger.info("mainline incremental(%s): compute %d days", kind, len(missing))
    new_rows = compute_mainline_range(repo, data_dir, missing[0], missing[-1], kind=kind)
    if not new_rows.is_empty():
        upsert_mainline_history(data_dir, new_rows)
    return new_rows
