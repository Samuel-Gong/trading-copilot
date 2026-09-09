"""标的维表同步服务。

盘前 9:10 调用 tf.exchanges.get_instruments("SH"/"SZ"/"BJ", type="stock")
获取全量标的元数据，flatten ext 字段，写入 instruments.parquet。

Starter+ 盘后可用 quotes.get(universes) 顺便补充 name。
"""
from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

from app.market_time import cn_today
from app.tickflow.client import get_client

logger = logging.getLogger(__name__)

_EXCHANGES = ["SH", "SZ", "BJ"]


def _flatten_instruments(items: list[dict]) -> list[dict]:
    """把 SDK 返回的 Instrument 列表 flatten 成扁平行。"""
    rows = []
    for item in items:
        row = {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "code": item.get("code"),
            "exchange": item.get("exchange"),
            "region": item.get("region"),
            "type": item.get("type"),
        }
        ext = item.get("ext") or {}
        row["listing_date"] = ext.get("listing_date")
        row["total_shares"] = ext.get("total_shares")
        row["float_shares"] = ext.get("float_shares")
        row["tick_size"] = ext.get("tick_size")
        row["limit_up"] = ext.get("limit_up")
        row["limit_down"] = ext.get("limit_down")
        rows.append(row)
    return rows


def _fetch_instruments_via_provider(provider_name: str, provider: object) -> list[dict]:
    """在调用方持有的 Provider 租约内拉取并展开标的维表。"""
    items = provider.get_instruments("stock") or []
    rows = _flatten_instruments(items)
    logger.info("instruments via %s: %d stocks", provider_name, len(rows))
    return rows


def sync_instruments(data_dir: Path) -> int:
    """全量同步标的维表 → data/instruments/instruments.parquet。

    返回写入的行数。
    """
    from app.services import preferences

    provider_name = preferences.get_daily_data_provider()

    def _publish(all_rows: list[dict], generation: int | None) -> int:
        if not all_rows:
            return 0
        from app.services.provider_routes import provider_route_commit_guard

        with provider_route_commit_guard(
            provider_name,
            generation,
            preferences.get_daily_data_provider,
            "instruments",
        ):
            df = pl.DataFrame(all_rows)
            df = df.with_columns(pl.lit(cn_today()).alias("as_of"))
            out = data_dir / "instruments" / "instruments.parquet"
            out.parent.mkdir(parents=True, exist_ok=True)
            df.write_parquet(out)
        logger.info("instruments synced: %d rows → %s", df.height, out)
        return df.height

    if provider_name == "tickflow":
        tf = get_client()
        tickflow_rows: list[dict] = []
        for ex in _EXCHANGES:
            try:
                items = tf.exchanges.get_instruments(ex, instrument_type="stock")
                if items:
                    tickflow_rows.extend(_flatten_instruments(items))
                    logger.info("instruments %s: %d stocks", ex, len(items))
            except Exception as e:
                logger.warning("get_instruments(%s) failed: %s", ex, e)
        return _publish(tickflow_rows, None)

    from app.data_providers import custom as custom_sources

    if not custom_sources.provider_has_dataset(provider_name, "instruments"):
        logger.error(
            "selected daily provider %s does not provide instruments; refusing TickFlow fallback",
            provider_name,
        )
        return 0
    try:
        with custom_sources.lease_provider(provider_name) as (provider, generation):
            rows = _fetch_instruments_via_provider(provider_name, provider)
            return _publish(rows, generation)
    except Exception as exc:  # noqa: BLE001
        logger.warning("provider %s get_instruments 失败: %s", provider_name, exc)
        return 0


def enrich_names_from_quotes(
    data_dir: Path,
    quotes_data: list[dict],
) -> int:
    """从 quotes 响应中提取 name，更新 instruments 维表（兜底补充）。

    盘后 quotes.get(universes) 返回的数据中包含 ext.name，
    用来补充 instruments 中可能缺失的 name。
    """
    if not quotes_data:
        return 0

    # 构建 symbol → name 映射
    name_map: dict[str, str] = {}
    for q in quotes_data:
        symbol = q.get("symbol", "")
        ext = q.get("ext") or {}
        name = ext.get("name") or q.get("name", "")
        if symbol and name:
            name_map[symbol] = name

    if not name_map:
        return 0

    inst_path = data_dir / "instruments" / "instruments.parquet"
    if not inst_path.exists():
        return 0

    df = pl.read_parquet(inst_path)

    # 只更新空 name 的行
    updates = pl.DataFrame({
        "symbol": list(name_map.keys()),
        "_new_name": list(name_map.values()),
    })
    df = df.join(updates, on="symbol", how="left")
    df = df.with_columns(
        pl.when(pl.col("name").is_null() | (pl.col("name") == ""))
        .then(pl.col("_new_name"))
        .otherwise(pl.col("name"))
        .alias("name"),
    ).drop("_new_name")

    df.write_parquet(inst_path)
    logger.info("instruments name enriched from quotes: %d names", len(name_map))
    return len(name_map)
