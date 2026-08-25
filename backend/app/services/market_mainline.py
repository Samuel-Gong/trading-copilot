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

import hashlib
import json
import logging
import time
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl

from app.market_time import cn_today
from app.services.atomic_parquet import replace_parquet_set, write_parquet_atomic
from app.services.ext_data import ExtConfigStore
from app.services.market_environment_lock import (
    market_environment_journal_path,
    market_environment_snapshot,
    serialized_market_environment_update,
)
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

_MAINLINE_COVERAGE_SCHEMA = {
    "date": pl.Date,
    "kind": pl.Utf8,
    "source_mtime_ns": pl.Int64,
    "membership_version": pl.Utf8,
    "filter_version": pl.Utf8,
}


class MainlineSourceChangedError(RuntimeError):
    """全量主线计算期间行情、point-in-time 成员或过滤配置发生变化。"""


@dataclass(frozen=True)
class MainlineSourceSnapshot:
    """主线全量计算固定使用的来源水位与筛选配置。"""

    coverage: pl.DataFrame
    filter_config: dict
    filter_version: str
    kinds: tuple[str, ...] = ("concept", "industry")


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


def load_mainline_filter_config() -> dict:
    """一次性读取并规范化本轮主线计算使用的过滤配置。"""
    try:
        from app.services import preferences

        raw = preferences.get_mainline_filter_config()
        resolved = _resolve_filter_config(raw)
        exclude_st = bool(raw.get("exclude_st", True))
    except Exception:
        resolved = {"min_members": 4, "max_members": 600, "blacklist": set()}
        exclude_st = True
    return {
        "min_members": resolved["min_members"],
        "max_members": resolved["max_members"],
        "blacklist": sorted(resolved["blacklist"]),
        "exclude_st": exclude_st,
    }


def _mainline_filter_version(filter_config: dict) -> str:
    return json.dumps(
        filter_config,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def mainline_path(data_dir: Path) -> Path:
    return data_dir / MAINLINE_DIR / "part.parquet"


def mainline_coverage_path(data_dir: Path) -> Path:
    """增量计算完成水位；与有无主线结果解耦。"""
    return data_dir / MAINLINE_DIR / "coverage.parquet"


def clear_mainline_history(data_dir: Path) -> None:
    """删除失去 enriched 来源后的主线历史及逐日完成水位。"""
    for path in (mainline_path(data_dir), mainline_coverage_path(data_dir)):
        path.unlink(missing_ok=True)


def _processed_mainline_dates(data_dir: Path, kind: str) -> set[date]:
    path = mainline_coverage_path(data_dir)
    if not path.exists():
        return set()
    try:
        frame = pl.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("load mainline coverage failed: %s", exc)
        return set()
    if not {"date", "kind"}.issubset(frame.columns):
        return set()
    return set(
        frame.filter(pl.col("kind") == kind)["date"].cast(pl.Date).to_list()
    )


def _enriched_partition_mtime_ns(repo, target_date: date) -> int | None:
    part = (
        repo.store.data_dir
        / "kline_daily_enriched"
        / f"date={target_date.isoformat()}"
        / "part.parquet"
    )
    try:
        return part.stat().st_mtime_ns
    except OSError:
        return None


def _membership_effective_date(path: Path) -> date | None:
    """从 timeseries 分区路径提取生效日期。"""
    for parent in path.parents:
        if not parent.name.startswith("date="):
            continue
        try:
            return date.fromisoformat(parent.name.removeprefix("date="))
        except ValueError:
            return None
    return None


def _membership_versions_by_date(
    data_dir: Path,
    kind: str,
    dates: set[date],
) -> dict[date, tuple[str, int]]:
    """计算各交易日实际参与主线计算的成分快照版本。"""
    sources: list[
        tuple[str, int, tuple[date, ...], dict[date, list[tuple[str, int, int]]]]
    ] = []
    for config in ExtConfigStore(data_dir).load_all():
        if config.mode != "timeseries" or not _dimension_field(config, kind):
            continue
        semantic_config = {
            "id": config.id,
            "mode": config.mode,
            "fields": [field.to_dict() for field in config.fields],
            "symbol_map": config.symbol_map,
            "code_map": config.code_map,
        }
        config_version = json.dumps(
            semantic_config,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        config_path = data_dir / "ext_data" / config.id / "config.json"
        try:
            config_mtime_ns = config_path.stat().st_mtime_ns
        except OSError:
            config_mtime_ns = 0
        snapshots: dict[date, list[tuple[str, int, int]]] = {}
        for raw_path in _ext_files(data_dir, config):
            path = Path(raw_path)
            effective = _membership_effective_date(path)
            if effective is None:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            relative = str(path.relative_to(data_dir))
            snapshots.setdefault(effective, []).append(
                (relative, stat.st_mtime_ns, stat.st_size),
            )
        if snapshots:
            sources.append((
                config_version,
                config_mtime_ns,
                tuple(sorted(snapshots)),
                snapshots,
            ))

    versions: dict[date, tuple[str, int]] = {}
    for target_date in dates:
        digest = hashlib.sha256()
        newest_mtime_ns = 0
        for config_version, config_mtime_ns, effective_dates, snapshots in sources:
            effective_index = bisect_right(effective_dates, target_date) - 1
            if effective_index < 0:
                continue
            effective = effective_dates[effective_index]
            digest.update(config_version.encode("utf-8"))
            digest.update(b"\0")
            digest.update(effective.isoformat().encode("ascii"))
            digest.update(b"\0")
            newest_mtime_ns = max(newest_mtime_ns, config_mtime_ns)
            for relative, mtime_ns, size in sorted(snapshots[effective]):
                digest.update(relative.encode("utf-8"))
                digest.update(f"\0{mtime_ns}\0{size}".encode("ascii"))
                newest_mtime_ns = max(newest_mtime_ns, mtime_ns)
        versions[target_date] = (digest.hexdigest(), newest_mtime_ns)
    return versions


def _stale_mainline_dates(
    data_dir: Path,
    repo,
    kind: str,
    completed_dates: set[date],
    *,
    current_filter_version: str | None = None,
) -> set[date]:
    """返回行情、成分快照或过滤配置与完成水位不一致的已处理日期。"""
    path = mainline_coverage_path(data_dir)
    versions: dict[date, int | None] = {}
    membership_versions: dict[date, str | None] = {}
    filter_versions: dict[date, str | None] = {}
    if path.exists():
        try:
            frame = pl.read_parquet(path)
            if {"date", "kind", "source_mtime_ns"}.issubset(frame.columns):
                versions = {
                    row["date"]: row["source_mtime_ns"]
                    for row in frame.filter(pl.col("kind") == kind)
                    .select(["date", "source_mtime_ns"])
                    .iter_rows(named=True)
                }
            if {"date", "kind", "membership_version"}.issubset(frame.columns):
                membership_versions = {
                    row["date"]: row["membership_version"]
                    for row in frame.filter(pl.col("kind") == kind)
                    .select(["date", "membership_version"])
                    .iter_rows(named=True)
                }
            if {"date", "kind", "filter_version"}.issubset(frame.columns):
                filter_versions = {
                    row["date"]: row["filter_version"]
                    for row in frame.filter(pl.col("kind") == kind)
                    .select(["date", "filter_version"])
                    .iter_rows(named=True)
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("load mainline coverage versions failed: %s", exc)

    legacy_mtime_ns = 0
    for completed_path in (mainline_path(data_dir), path):
        try:
            legacy_mtime_ns = max(legacy_mtime_ns, completed_path.stat().st_mtime_ns)
        except OSError:
            continue

    current_membership_versions = _membership_versions_by_date(
        data_dir,
        kind,
        completed_dates,
    )
    if current_filter_version is None:
        current_filter_version = _mainline_filter_version(
            load_mainline_filter_config(),
        )
    stale: set[date] = set()
    for target_date in completed_dates:
        source_mtime_ns = _enriched_partition_mtime_ns(repo, target_date)
        recorded_source_mtime_ns = versions.get(target_date)
        if (
            source_mtime_ns is not None
            and target_date in versions
            and recorded_source_mtime_ns != source_mtime_ns
        ):
            stale.add(target_date)
        elif (
            source_mtime_ns is not None
            and target_date not in versions
            and source_mtime_ns > legacy_mtime_ns
        ):
            stale.add(target_date)
        current_version, membership_mtime_ns = current_membership_versions[target_date]
        recorded_membership_version = membership_versions.get(target_date)
        if (
            target_date in membership_versions
            and recorded_membership_version is not None
            and recorded_membership_version != current_version
        ):
            stale.add(target_date)
        elif (
            recorded_membership_version is None
            and membership_mtime_ns > legacy_mtime_ns
        ):
            stale.add(target_date)
        if filter_versions.get(target_date) != current_filter_version:
            stale.add(target_date)
    return stale


def _mark_mainline_dates_processed(
    data_dir: Path,
    repo,
    kind: str,
    dates: set[date],
    *,
    filter_version: str | None = None,
    source_snapshot: MainlineSourceSnapshot | None = None,
) -> None:
    if not dates:
        return
    path = mainline_coverage_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if source_snapshot is None:
        assert filter_version is not None
        incoming = _mainline_coverage_frame(
            data_dir,
            repo,
            dates,
            (kind,),
            filter_version=filter_version,
        )
    else:
        incoming = source_snapshot.coverage.filter(
            (pl.col("kind") == kind) & pl.col("date").is_in(sorted(dates)),
        )
    old = pl.read_parquet(path) if path.exists() else pl.DataFrame()
    if not old.is_empty() and "source_mtime_ns" not in old.columns:
        old = old.with_columns(pl.lit(None).cast(pl.Int64).alias("source_mtime_ns"))
    if not old.is_empty() and "membership_version" not in old.columns:
        old = old.with_columns(
            pl.lit(None).cast(pl.Utf8).alias("membership_version"),
        )
    if not old.is_empty() and "filter_version" not in old.columns:
        old = old.with_columns(
            pl.lit(None).cast(pl.Utf8).alias("filter_version"),
        )
    combined = incoming if old.is_empty() else pl.concat(
        [old.select(incoming.columns), incoming],
        how="vertical_relaxed",
    )
    write_parquet_atomic(
        combined.unique(subset=["date", "kind"], keep="last").sort(
            ["date", "kind"]
        ),
        path,
    )


def _mainline_coverage_frame(
    data_dir: Path,
    repo,
    dates: set[date],
    kinds: tuple[str, ...],
    *,
    filter_version: str,
) -> pl.DataFrame:
    """构建指定交易日及维度的完整来源/成员版本水位。"""
    if not dates or not kinds:
        return pl.DataFrame(schema=_MAINLINE_COVERAGE_SCHEMA)
    ordered_dates = sorted(dates)
    frames: list[pl.DataFrame] = []
    for kind in kinds:
        membership_versions = _membership_versions_by_date(data_dir, kind, dates)
        frames.append(pl.DataFrame({
            "date": pl.Series(ordered_dates, dtype=pl.Date),
            "kind": pl.Series([kind] * len(ordered_dates), dtype=pl.Utf8),
            "source_mtime_ns": pl.Series(
                [_enriched_partition_mtime_ns(repo, item) for item in ordered_dates],
                dtype=pl.Int64,
            ),
            "membership_version": pl.Series(
                [membership_versions[item][0] for item in ordered_dates],
                dtype=pl.Utf8,
            ),
            "filter_version": pl.Series(
                [filter_version] * len(ordered_dates),
                dtype=pl.Utf8,
            ),
        }))
    return pl.concat(frames).sort(["date", "kind"])


def capture_mainline_source_snapshot(
    data_dir: Path,
    repo,
    *,
    start: date,
    end: date,
    kinds: tuple[str, ...] = ("concept", "industry"),
    filter_config: dict | None = None,
) -> MainlineSourceSnapshot:
    """捕获主线结果对应的行情、成员版本与过滤配置快照。"""
    from app.services.regime_builder import enriched_date_set

    filter_config = (
        load_mainline_filter_config()
        if filter_config is None
        else {
            **filter_config,
            "blacklist": list(filter_config.get("blacklist") or []),
        }
    )
    filter_version = _mainline_filter_version(filter_config)
    covered_dates = {
        target_date
        for target_date in enriched_date_set(repo)
        if start <= target_date <= end
    }
    return MainlineSourceSnapshot(
        coverage=_mainline_coverage_frame(
            data_dir,
            repo,
            covered_dates,
            kinds,
            filter_version=filter_version,
        ),
        filter_config=filter_config,
        filter_version=filter_version,
        kinds=kinds,
    )


def assert_mainline_source_unchanged(
    expected: MainlineSourceSnapshot,
    data_dir: Path,
    repo,
    *,
    start: date,
    end: date,
) -> None:
    """发布前复验完整来源版本，避免把旧结果标记成新 coverage。"""
    current = capture_mainline_source_snapshot(
        data_dir,
        repo,
        start=start,
        end=end,
        kinds=expected.kinds,
    )
    if (
        expected.coverage.to_dicts() != current.coverage.to_dicts()
        or expected.filter_version != current.filter_version
    ):
        raise MainlineSourceChangedError("主线计算期间来源已更新，请重试")


def _remove_mainline_dates_processed(
    data_dir: Path,
    kind: str,
    dates: set[date],
) -> None:
    """删除已失去 enriched 来源的主线完成水位。"""
    if not dates:
        return
    path = mainline_coverage_path(data_dir)
    if not path.exists():
        return
    frame = pl.read_parquet(path)
    if frame.is_empty() or not {"date", "kind"}.issubset(frame.columns):
        return
    kept = frame.filter(
        ~(
            (pl.col("kind") == kind)
            & pl.col("date").is_in(sorted(dates))
        )
    )
    write_parquet_atomic(kept, path)


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
    with market_environment_snapshot(data_dir):
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


@serialized_market_environment_update
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
    write_parquet_atomic(combined, p)


def build_mainline_history_full_snapshot(
    data_dir: Path,
    rows_by_kind: dict[str, pl.DataFrame],
    repo,
    *,
    start: date,
    end: date,
    source_snapshot: MainlineSourceSnapshot | None = None,
) -> tuple[list[tuple[Path, pl.DataFrame]], int]:
    """构建合并概念/行业结果与逐日完整完成水位的全量文件集合。"""
    frames = [frame for frame in rows_by_kind.values() if not frame.is_empty()]
    if frames:
        combined = pl.concat(frames, how="vertical_relaxed").sort(
            ["date", "kind", "rank"]
        )
    else:
        combined = pl.DataFrame()
    snapshot = source_snapshot
    if snapshot is None:
        snapshot = capture_mainline_source_snapshot(
            data_dir,
            repo,
            start=start,
            end=end,
            kinds=tuple(rows_by_kind),
        )
    entries = [
        (mainline_path(data_dir), combined),
        (mainline_coverage_path(data_dir), snapshot.coverage),
    ]
    return entries, sum(frame.height for frame in rows_by_kind.values())


@serialized_market_environment_update
def replace_mainline_history_full(
    data_dir: Path,
    rows_by_kind: dict[str, pl.DataFrame],
    repo,
    *,
    start: date,
    end: date,
    source_snapshot: MainlineSourceSnapshot | None = None,
) -> int:
    """原子发布合并后的概念/行业全量结果及逐日完成水位。"""
    if source_snapshot is not None:
        assert_mainline_source_unchanged(
            source_snapshot,
            data_dir,
            repo,
            start=start,
            end=end,
        )
    entries, rows = build_mainline_history_full_snapshot(
        data_dir,
        rows_by_kind,
        repo,
        start=start,
        end=end,
        source_snapshot=source_snapshot,
    )
    replace_parquet_set(
        entries,
        journal_path=market_environment_journal_path(data_dir),
    )
    return rows


@serialized_market_environment_update
def replace_mainline_history_range(
    data_dir: Path,
    new_rows: pl.DataFrame,
    *,
    start: date,
    end: date,
    kind: str,
) -> None:
    """以重算结果完整替换指定日期与维度范围，允许结果为空。"""
    if start > end:
        return
    p = mainline_path(data_dir)
    old = pl.read_parquet(p) if p.exists() else pl.DataFrame()
    if old.is_empty() and new_rows.is_empty():
        return

    incoming = new_rows
    if not incoming.is_empty():
        incoming = incoming.filter(
            (pl.col("date") >= start)
            & (pl.col("date") <= end)
            & (pl.col("kind") == kind)
        )

    if old.is_empty():
        combined = incoming
    else:
        kept = old.filter(
            ~(
                (pl.col("date") >= start)
                & (pl.col("date") <= end)
                & (pl.col("kind") == kind)
            )
        )
        if incoming.is_empty():
            combined = kept
        else:
            target_cols = incoming.columns
            kept = kept.select([
                pl.col(column)
                if column in kept.columns
                else pl.lit(None).alias(column)
                for column in target_cols
            ])
            combined = pl.concat(
                [kept, incoming.select(target_cols)],
                how="vertical_relaxed",
            )

    p.parent.mkdir(parents=True, exist_ok=True)
    write_parquet_atomic(combined.sort(["date", "kind", "rank"]), p)


def build_mainline_history_range_snapshot(
    data_dir: Path,
    rows_by_kind: dict[str, pl.DataFrame],
    *,
    start: date,
    end: date,
    source_snapshot: MainlineSourceSnapshot,
) -> tuple[list[tuple[Path, pl.DataFrame]], int]:
    """构建区间重算后的合并主线历史与来源水位，不提前写盘。"""
    kinds = tuple(rows_by_kind)
    old = (
        pl.read_parquet(mainline_path(data_dir))
        if mainline_path(data_dir).exists()
        else pl.DataFrame()
    )
    frames = [
        frame.filter(
            (pl.col("date") >= start)
            & (pl.col("date") <= end)
            & (pl.col("kind") == kind),
        )
        for kind, frame in rows_by_kind.items()
        if not frame.is_empty()
    ]
    incoming = (
        pl.concat(frames, how="vertical_relaxed")
        if frames
        else pl.DataFrame()
    )
    if old.is_empty():
        combined = incoming
    else:
        kept = old.filter(
            ~(
                (pl.col("date") >= start)
                & (pl.col("date") <= end)
                & pl.col("kind").is_in(kinds)
            ),
        )
        if incoming.is_empty():
            combined = kept
        else:
            target_cols = incoming.columns
            kept = kept.select([
                pl.col(column)
                if column in kept.columns
                else pl.lit(None).alias(column)
                for column in target_cols
            ])
            combined = pl.concat(
                [kept, incoming.select(target_cols)],
                how="vertical_relaxed",
            )
    if not combined.is_empty():
        combined = combined.sort(["date", "kind", "rank"])

    coverage_path = mainline_coverage_path(data_dir)
    old_coverage = (
        pl.read_parquet(coverage_path)
        if coverage_path.exists()
        else pl.DataFrame()
    )
    incoming_coverage = source_snapshot.coverage.filter(
        (pl.col("date") >= start)
        & (pl.col("date") <= end)
        & pl.col("kind").is_in(kinds),
    )
    if old_coverage.is_empty():
        combined_coverage = incoming_coverage
    else:
        for column, dtype in _MAINLINE_COVERAGE_SCHEMA.items():
            if column not in old_coverage.columns:
                old_coverage = old_coverage.with_columns(
                    pl.lit(None).cast(dtype).alias(column),
                )
        kept_coverage = old_coverage.select(incoming_coverage.columns).filter(
            ~(
                (pl.col("date") >= start)
                & (pl.col("date") <= end)
                & pl.col("kind").is_in(kinds)
            ),
        )
        combined_coverage = pl.concat(
            [kept_coverage, incoming_coverage],
            how="vertical_relaxed",
        )
    if not combined_coverage.is_empty():
        combined_coverage = combined_coverage.unique(
            subset=["date", "kind"],
            keep="last",
        ).sort(["date", "kind"])

    return [
        (mainline_path(data_dir), combined),
        (coverage_path, combined_coverage),
    ], sum(frame.height for frame in rows_by_kind.values())


@serialized_market_environment_update
def compute_mainline_incremental(repo, data_dir: Path, *, today: date | None = None,
                                 kind: str = "concept") -> pl.DataFrame:
    """增量补算主线：补缺失日，并重算被覆写的 enriched 分区。"""
    today = today or cn_today()
    from app.services.regime_builder import enriched_date_set

    enriched_dates = enriched_date_set(repo)
    existing = load_mainline_history(data_dir, kind)
    existing_dates = set(existing["date"].to_list()) if not existing.is_empty() else set()
    processed_dates = _processed_mainline_dates(data_dir, kind)
    completed_dates = existing_dates | processed_dates
    filter_config = load_mainline_filter_config()
    filter_version = _mainline_filter_version(filter_config)
    missing = sorted(d for d in enriched_dates if d not in completed_dates and d <= today)
    stale = sorted(
        d for d in _stale_mainline_dates(
            data_dir,
            repo,
            kind,
            completed_dates,
            current_filter_version=filter_version,
        )
        if d in enriched_dates and d <= today
    )
    removed = sorted(
        d for d in completed_dates if d not in enriched_dates and d <= today
    )
    to_compute = sorted(set(missing) | set(stale) | set(removed))
    if not to_compute:
        return pl.DataFrame()
    logger.info(
        "mainline incremental(%s): compute %d days (missing=%d, stale=%d, removed=%d)",
        kind,
        len(to_compute),
        len(missing),
        len(stale),
        len(removed),
    )
    source_snapshot = capture_mainline_source_snapshot(
        data_dir,
        repo,
        start=to_compute[0],
        end=to_compute[-1],
        kinds=(kind,),
        filter_config=filter_config,
    )
    new_rows = compute_mainline_range(
        repo,
        data_dir,
        to_compute[0],
        to_compute[-1],
        kind=kind,
        filter_cfg=source_snapshot.filter_config,
    )
    assert_mainline_source_unchanged(
        source_snapshot,
        data_dir,
        repo,
        start=to_compute[0],
        end=to_compute[-1],
    )
    replace_mainline_history_range(
        data_dir,
        new_rows,
        start=to_compute[0],
        end=to_compute[-1],
        kind=kind,
    )
    _remove_mainline_dates_processed(data_dir, kind, set(removed))
    _mark_mainline_dates_processed(
        data_dir,
        repo,
        kind,
        set(source_snapshot.coverage["date"].to_list()),
        source_snapshot=source_snapshot,
    )
    return new_rows
