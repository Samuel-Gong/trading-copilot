"""历史股本解析；只有公告后交易日才能使用对应记录。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl


def load_share_history(data_dir: Path) -> pl.DataFrame:
    """读取本地财务股本表；未同步或损坏时返回空表。"""
    try:
        from app.services.financial_sync import get_financial_df

        shares = get_financial_df(data_dir, "shares")
        if not {"symbol", "period_end", "float_shares"} <= set(shares.columns):
            return pl.DataFrame()
        return shares
    except Exception:
        return pl.DataFrame()


def apply_historical_float_shares(
    rows: pl.DataFrame,
    shares: pl.DataFrame | None,
    *,
    today: date,
) -> pl.DataFrame:
    """为行情行解析有效流通股本。

    当日保留 rows.float_shares；历史日期只使用公告日严格早于交易日的最新股本。
    缺少公告日或尚未公告时返回空值，禁止回退到当前 instruments 造成未来泄漏。
    """
    required = {"symbol", "date", "float_shares"}
    if rows.is_empty() or not required <= set(rows.columns):
        return rows

    def without_historical_fallback() -> pl.DataFrame:
        return rows.with_columns(
            pl.when(pl.col("date").cast(pl.Date, strict=False) == pl.lit(today))
            .then(pl.col("float_shares"))
            .otherwise(None)
            .cast(pl.Float64)
            .alias("float_shares")
        )

    if (
        shares is None
        or shares.is_empty()
        or not {"symbol", "period_end", "float_shares"} <= set(shares.columns)
    ):
        return without_historical_fallback()

    def as_date_expr(column: str) -> pl.Expr:
        dtype = shares.schema[column]
        if dtype == pl.Utf8:
            return pl.col(column).str.to_date(strict=False)
        return pl.col(column).cast(pl.Date, strict=False)

    if "announce_date" not in shares.columns:
        return without_historical_fallback()
    # 公告日当天可能在收盘后才发布；从下一自然日开始才允许进入历史计算。
    available_date = as_date_expr("announce_date") + pl.duration(days=1)

    history = (
        shares
        .select(
            pl.col("symbol").cast(pl.Utf8),
            available_date.alias("_share_available_date"),
            pl.col("period_end").cast(pl.Utf8).alias("_share_period_end"),
            pl.col("float_shares").cast(pl.Float64, strict=False).alias("_historical_float_shares"),
        )
        .filter(
            pl.col("symbol").is_not_null()
            & pl.col("_share_available_date").is_not_null()
            & (pl.col("_historical_float_shares") > 0)
        )
        .sort(["symbol", "_share_available_date", "_share_period_end"])
        .unique(subset=["symbol", "_share_available_date"], keep="last")
        .sort(["symbol", "_share_available_date"])
    )
    if history.is_empty():
        return without_historical_fallback()

    resolved = (
        rows
        .with_row_index("_share_row_order")
        .with_columns(
            pl.col("symbol").cast(pl.Utf8),
            pl.col("date").cast(pl.Date, strict=False).alias("_share_trade_date"),
        )
        .sort(["symbol", "_share_trade_date"])
        .join_asof(
            history,
            left_on="_share_trade_date",
            right_on="_share_available_date",
            by="symbol",
            strategy="backward",
            check_sortedness=False,
        )
        .with_columns(
            pl.when(pl.col("_share_trade_date") == pl.lit(today))
            .then(pl.col("float_shares"))
            .otherwise(pl.col("_historical_float_shares"))
            .alias("float_shares")
        )
        .sort("_share_row_order")
    )
    return resolved.drop(
        "_share_row_order",
        "_share_trade_date",
        "_share_available_date",
        "_share_period_end",
        "_historical_float_shares",
    )
