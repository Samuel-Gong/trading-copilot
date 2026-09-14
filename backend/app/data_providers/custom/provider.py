"""Generic HTTP provider for custom market data sources."""
from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl

from app.config import settings
from app.data_providers.base import AssetType
from app.data_providers.custom.config import CustomSourceConfig, DatasetConfig
from app.data_providers.custom.mapper import (
    apply_transforms,
    datetime_payload,
    extract_rows,
    map_rows,
)
from app.data_providers.normalizer import normalize_adj_factors, normalize_daily
from app.market_time import cn_now
from app.tickflow.rate_limits import chunked, sleep_between_batches

logger = logging.getLogger(__name__)

_REQUIRED = {
    "daily": {"symbol", "date", "open", "high", "low", "close", "volume", "amount"},
    "adj_factor": {"symbol", "trade_date", "ex_factor"},
    "realtime": {
        "symbol",
        "last_price",
        "prev_close",
        "open",
        "high",
        "low",
        "volume",
        "timestamp",
    },
    "minute": {"symbol", "datetime", "open", "high", "low", "close", "volume", "amount"},
    # full_minute (全量分钟) 与 minute 同形: 当日窗口批量拉取, 字段映射一致
    "full_minute": {"symbol", "datetime", "open", "high", "low", "close", "volume", "amount"},
    # 财务历史必须带报告期与公告日，才能满足 point-in-time 契约。
    "financial": {"symbol", "period_end", "announce_date"},
    "instruments": {"symbol"},
}

_PCT_COLUMNS = ("change_pct", "amplitude", "turnover_rate")
_FINANCIAL_PCT_COLUMNS = (
    "roe",
    "gross_margin",
    "net_margin",
    "revenue_yoy",
    "net_income_yoy",
    "debt_to_asset_ratio",
)
_AUTH_TYPES = {"none", "bearer", "header", "query"}
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _normalize_pct_units(
    df: pl.DataFrame,
    pct_unit: str | None = None,
) -> pl.DataFrame:
    """比例字段单位归一为契约小数制 (change_pct/amplitude/turnover_rate,
    0.0366 = 3.66%, CONTRIBUTING §3.1)。单位只认显式声明, 不靠数值猜:

      - pct_unit="percent"  → 三列无条件 /100 (声明即契约, 即使数值看着像小数制);
      - pct_unit="decimal"  → 原样透传 (即使数值看着像百分制也不动);
      - 未声明 → 所有比例列置 None。低波动百分数与小数制在数值上重叠,
        禁止根据批次分布猜测单位。
    """
    dropped_undeclared = False
    for col in _PCT_COLUMNS:
        if col not in df.columns:
            continue
        df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False).alias(col))
        if pct_unit == "percent":
            df = df.with_columns((pl.col(col) / 100).alias(col))
        elif pct_unit == "decimal":
            continue
        else:
            df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(col))
            dropped_undeclared = True
    if dropped_undeclared:
        logger.warning(
            "自定义源 realtime 未声明 pct_unit: 比例字段单位无法安全判定,"
            "已全部置 None;"
            "请在 realtime 数据集配置中显式声明 pct_unit: percent 或 decimal"
        )
    return df


def _normalize_volume_units(
    df: pl.DataFrame,
    volume_unit: str | None,
    dataset: str,
) -> pl.DataFrame:
    """把 Provider 成交量统一为手；未声明单位时 fail-closed。"""
    if df.is_empty() or "volume" not in df.columns:
        return df
    if volume_unit not in {"lots", "shares"}:
        logger.error("custom %s volume_unit is required", dataset)
        return pl.DataFrame()
    volume = pl.col("volume").cast(pl.Float64, strict=False)
    if volume_unit == "shares":
        volume /= 100.0
    return df.with_columns(volume.alias("volume"))


class GenericHTTPProvider:
    """HTTP-backed custom source. It only handles fetching and schema mapping."""

    def __init__(self, config: CustomSourceConfig) -> None:
        self.config = config
        self.name = config.name
        self._client = httpx.Client(timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def validate(self) -> list[str]:
        errors: list[str] = []
        auth = self.config.auth
        if auth.type not in _AUTH_TYPES:
            errors.append(f"auth: unsupported type: {auth.type}")
        elif auth.type != "none":
            if not auth.token_env:
                errors.append(f"auth: token_env is required for {auth.type}")
            elif not _ENV_NAME_RE.fullmatch(auth.token_env):
                errors.append("auth: token_env must be a valid environment variable name")
        for dataset, cfg in self.config.datasets.items():
            if not cfg.url:
                errors.append(f"{dataset}: url is required")
            required = _REQUIRED.get(dataset)
            if required:
                mapped = set(cfg.field_map.values())
                missing = sorted(required - mapped)
                if missing:
                    errors.append(f"{dataset}: missing mapped fields: {', '.join(missing)}")
            if cfg.pct_unit is not None:
                if dataset not in {"realtime", "financial"}:
                    errors.append(f"{dataset}: pct_unit 仅用于 realtime/financial 数据集")
                elif cfg.pct_unit not in ("percent", "decimal"):
                    errors.append(f"{dataset}: pct_unit 必须是 percent 或 decimal")
            if (
                dataset == "realtime"
                and set(cfg.field_map.values()).intersection(_PCT_COLUMNS)
                and cfg.pct_unit is None
            ):
                errors.append(
                    "realtime: 映射比例字段时必须声明 pct_unit: percent 或 decimal"
                )
            if (
                dataset == "financial"
                and set(cfg.field_map.values()).intersection(_FINANCIAL_PCT_COLUMNS)
                and cfg.pct_unit is None
            ):
                errors.append(
                    "financial: 映射比例字段时必须声明 pct_unit: percent 或 decimal"
                )
            if "volume" in set(cfg.field_map.values()):
                if cfg.volume_unit not in {"lots", "shares"}:
                    errors.append(
                        f"{dataset}: 映射 volume 时必须声明 volume_unit: lots 或 shares"
                    )
            elif cfg.volume_unit is not None:
                errors.append(f"{dataset}: volume_unit 仅在映射 volume 时使用")
            if dataset == "adj_factor":
                if cfg.adj_factor_kind not in {"event_ratio", "cumulative"}:
                    errors.append(
                        "adj_factor: 必须声明 adj_factor_kind: event_ratio 或 cumulative"
                    )
            elif cfg.adj_factor_kind is not None:
                errors.append(f"{dataset}: adj_factor_kind 仅用于 adj_factor 数据集")
            if dataset not in {"realtime", "instruments"}:
                request_params = [cfg.symbols_param, cfg.start_param, cfg.end_param]
                if dataset in {"minute", "full_minute"}:
                    request_params.extend(
                        name for name in (cfg.asset_type_param, cfg.freq_param) if name
                    )
                duplicates = sorted({
                    name for name in request_params if request_params.count(name) > 1
                })
                if duplicates:
                    errors.append(
                        f"{dataset}: duplicate request parameter names: "
                        f"{', '.join(duplicates)}"
                    )
        return errors

    def _request_rows_retry(
        self, cfg, symbols: list[str], *, start_time=None, end_time=None, retries: int = 1
    ) -> list[dict]:
        """单批请求 + 短退避重试。仍失败抛出, 由调用方决定隔离粒度 (#226)。"""
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return self._request_rows(
                    cfg, symbols=symbols, start_time=start_time, end_time=end_time
                )
            except Exception as e:
                last = e
                if attempt < retries:
                    time.sleep(1.0 * (attempt + 1))
        assert last is not None
        raise last

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",  # noqa: ARG002
        on_chunk_done=None,
        failed_out: list[str] | None = None,
    ) -> pl.DataFrame:
        cfg = self._dataset("daily")
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, cfg.batch)
        failed: list[str] = []
        for i, chunk in enumerate(chunks):
            sleep_between_batches(i, cfg.rpm)
            try:
                rows = self._request_rows_retry(
                    cfg, chunk, start_time=start_time, end_time=end_time
                )
            except Exception as e:
                # 单批失败只隔离该批 (#226): 之前任一批 502 会让整个 stage
                # 抛异常, 已成功批次的结果留在内存里全部丢弃
                failed.extend(chunk)
                logger.warning(
                    "custom daily: batch %d/%d failed (%d symbols), skipped: %s",
                    i + 1, len(chunks), len(chunk), e,
                )
                if on_chunk_done:
                    on_chunk_done(i + 1, len(chunks))
                continue
            df = self._mapped_frame(cfg, rows)
            df = _normalize_volume_units(df, cfg.volume_unit, "daily")
            df = normalize_daily(df, source=self.name)
            if df.is_empty():
                failed.extend(chunk)
            else:
                frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        if failed:
            logger.warning(
                "custom daily: %d/%d symbols missing due to batch failures: %s",
                len(failed), len(symbols), ", ".join(failed[:20]),
            )
            if failed_out is not None:
                failed_out.extend(failed)
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",  # noqa: ARG002
        on_chunk_done=None,
        failed_out: list[str] | None = None,
    ) -> pl.DataFrame:
        cfg = self._dataset("adj_factor")
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, cfg.batch)
        failed: list[str] = []
        for i, chunk in enumerate(chunks):
            sleep_between_batches(i, cfg.rpm)
            try:
                rows = self._request_rows_retry(
                    cfg,
                    chunk,
                    # 累计因子的区间首行需要前一条基线才能求事件比。
                    # 因此增量同步从源头拉全历史，换算后再截回请求区间。
                    start_time=None if cfg.adj_factor_kind == "cumulative" else start_time,
                    end_time=end_time,
                )
            except Exception as e:
                failed.extend(chunk)
                logger.warning(
                    "custom adj_factor: batch %d/%d failed (%d symbols), skipped: %s",
                    i + 1, len(chunks), len(chunk), e,
                )
                if on_chunk_done:
                    on_chunk_done(i + 1, len(chunks))
                continue
            df = self._mapped_frame(cfg, rows)
            df = normalize_adj_factors(df, source=self.name)
            if not df.is_empty() and cfg.adj_factor_kind == "cumulative":
                if start_time is not None:
                    start_day = start_time.date()
                    in_range_symbols = set(
                        df.filter(pl.col("trade_date") >= start_day)["symbol"].to_list()
                    )
                    baseline_symbols = set(
                        df.filter(pl.col("trade_date") < start_day)["symbol"].to_list()
                    )
                    missing_baseline = in_range_symbols - baseline_symbols
                    if missing_baseline:
                        logger.error(
                            "custom adj_factor cumulative baseline missing for %s",
                            sorted(missing_baseline)[:20],
                        )
                        failed.extend(sorted(missing_baseline))
                        df = df.filter(~pl.col("symbol").is_in(list(missing_baseline)))
                df = self._cumulative_to_event_ratios(df)
                if start_time is not None:
                    df = df.filter(pl.col("trade_date") >= start_time.date())
                if end_time is not None:
                    df = df.filter(pl.col("trade_date") <= end_time.date())
            elif not df.is_empty() and cfg.adj_factor_kind != "event_ratio":
                df = pl.DataFrame()
            if not df.is_empty():
                frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        if failed:
            failed = list(dict.fromkeys(failed))
            logger.warning(
                "custom adj_factor: %d/%d symbols missing due to batch failures: %s",
                len(failed), len(symbols), ", ".join(failed[:20]),
            )
            if failed_out is not None:
                failed_out.extend(failed)
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_realtime(self) -> list[dict]:
        cfg = self._dataset("realtime")
        rows = self._request_rows(cfg)
        df = self._mapped_frame(cfg, rows)
        df = _normalize_volume_units(df, cfg.volume_unit, "realtime")
        # 单位归一: 仅接受显式 pct_unit; 未声明时所有比例列 fail-closed 置 None。
        df = _normalize_pct_units(
            df,
            pct_unit=cfg.pct_unit,
        )
        if df.is_empty() or "timestamp" not in df.columns:
            logger.warning("custom realtime missing timestamp; snapshot discarded")
            return []
        input_rows = df.height
        df = df.with_columns(pl.col("timestamp").cast(pl.Int64, strict=False))
        valid_rows = df.filter(
            pl.col("timestamp").is_not_null() & (pl.col("timestamp") > 0)
        ).height
        if valid_rows != input_rows:
            logger.warning("custom realtime contains invalid timestamp; snapshot discarded")
            return []
        return df.to_dicts()

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """拉取分钟 K。

        asset_type / freq 默认不传上游 (minute dataset URL 应返回 1m 数据)。
        在 dataset 配置中设置 asset_type_param / freq_param 后, 这两个参数会以
        配置的参数名注入请求 (GET → params, POST → body), 用于上游需区分
        stock/ETF/index 或固定频率的场景。
        """
        return self._fetch_minute_dataset(
            "minute", symbols, start_time, end_time, asset_type, freq, on_chunk_done,
        )

    def get_intraday_batch(
        self,
        symbols: list[str],
        count: int = 300,
        asset_type: AssetType = "stock",
    ) -> pl.DataFrame:
        """全量分钟修复轮: 按当日窗口批量拉取 full_minute 数据集 (chunked + rpm 限速)。

        与 get_minute 同形 (字段映射/归一一致), 区别仅在数据集名与窗口由调用方
        传当日值。稳态增量 (get_intraday_latest) YAML 声明式源不提供 — 服务自动
        降级为仅修复轮模式并放慢节奏。
        """
        now = cn_now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self._fetch_minute_dataset(
            "full_minute", symbols, start, now, asset_type, "1m", None,
        )

    def _fetch_minute_dataset(
        self,
        ds_name: str,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        cfg = self._dataset(ds_name)
        override: dict[str, Any] = {}
        if cfg.asset_type_param:
            override[cfg.asset_type_param] = asset_type
        if cfg.freq_param:
            override[cfg.freq_param] = freq
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, cfg.batch)
        for i, chunk in enumerate(chunks):
            sleep_between_batches(i, cfg.rpm)
            rows = self._request_rows(
                cfg, symbols=chunk, start_time=start_time, end_time=end_time,
                override_params=override or None, override_body=override or None,
            )
            df = self._mapped_frame(cfg, rows)
            df = _normalize_volume_units(df, cfg.volume_unit, ds_name)
            df = self._normalize_minute(df)
            if not df.is_empty():
                frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    @staticmethod
    def _cumulative_to_event_ratios(df: pl.DataFrame) -> pl.DataFrame:
        """把按日期递增的累计因子转换为单次事件比值。"""
        ordered = df.sort(["symbol", "trade_date"])
        previous = pl.col("ex_factor").shift(1).over("symbol")
        return ordered.with_columns(
            pl.when(previous.is_null())
            .then(1.0)
            .when(previous > 0)
            .then(pl.col("ex_factor") / previous)
            .otherwise(None)
            .alias("ex_factor")
        ).drop_nulls(["symbol", "trade_date", "ex_factor"])

    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        """拉取财务数据。table 包含四张财务报表及 shares 股本表。

        custom 源用一个 'financial' dataset 配置覆盖全部财务表; 请求时把 table 作为参数传给上游,
        上游根据 table 返回对应数据。统一校验报告期、公告日和百分比单位，
        不满足 point-in-time 契约的数据 fail-closed。
        """
        cfg = self._dataset("financial")
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, cfg.batch)
        for i, chunk in enumerate(chunks):
            sleep_between_batches(i, cfg.rpm)
            # 把 table 注入到请求参数 (上游据此区分财务表)
            extra_params = {**cfg.params, "table": table}
            extra_body = {**cfg.body, "table": table}
            if table == "shares":
                extra_params["latest"] = latest_only
                extra_body["latest"] = latest_only
            rows = self._request_rows(
                cfg, symbols=chunk,
                override_params=extra_params, override_body=extra_body,
            )
            df = self._mapped_frame(cfg, rows)
            df = self._normalize_financial(df, table, cfg.pct_unit)
            if not df.is_empty():
                frames.append(df)
        if not frames:
            return pl.DataFrame()
        return pl.concat(frames, how="diagonal_relaxed")

    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        """拉取标的维表，供日 K 自定义源在无 TickFlow 时建立股票池。"""
        cfg = self._dataset("instruments")
        override: dict[str, Any] = {}
        if cfg.asset_type_param:
            override[cfg.asset_type_param] = asset_type
        rows = self._request_rows(
            cfg,
            override_params=override or None,
            override_body=override or None,
        )
        frame = self._mapped_frame(cfg, rows)
        if frame.is_empty() or "symbol" not in frame.columns:
            return []
        keep = [
            column
            for column in (
                "symbol", "name", "code", "exchange", "region", "type",
                "listing_date", "total_shares", "float_shares", "tick_size",
                "limit_up", "limit_down",
            )
            if column in frame.columns
        ]
        return frame.select(keep).drop_nulls(["symbol"]).to_dicts()

    @staticmethod
    def _normalize_financial(
        df: pl.DataFrame,
        table: str,
        pct_unit: str | None,
    ) -> pl.DataFrame:
        if df.is_empty():
            return df
        required = {"symbol", "period_end", "announce_date"}
        if not required.issubset(df.columns):
            logger.error(
                "custom financial %s missing PIT columns: %s",
                table,
                sorted(required - set(df.columns)),
            )
            return pl.DataFrame()
        frame = df.with_columns(
            pl.col("symbol").cast(pl.Utf8, strict=False),
            pl.col("period_end")
            .cast(pl.Utf8, strict=False)
            .str.slice(0, 10)
            .str.to_date(strict=False),
            pl.col("announce_date")
            .cast(pl.Utf8, strict=False)
            .str.slice(0, 10)
            .str.to_date(strict=False),
        ).drop_nulls(["symbol", "period_end", "announce_date"])
        pct_columns = [
            column for column in _FINANCIAL_PCT_COLUMNS if column in frame.columns
        ]
        if pct_columns and pct_unit is None:
            logger.error(
                "custom financial %s has percentage columns without pct_unit", table
            )
            return pl.DataFrame()
        expressions = []
        for column in pct_columns:
            value = pl.col(column).cast(pl.Float64, strict=False)
            if pct_unit == "decimal":
                value *= 100.0
            expressions.append(value.alias(column))
        for column in ("bps",):
            if column in frame.columns:
                expressions.append(
                    pl.col(column).cast(pl.Float64, strict=False).alias(column)
                )
        if expressions:
            frame = frame.with_columns(expressions)
        return frame

    @classmethod
    def _normalize_minute(cls, df: pl.DataFrame) -> pl.DataFrame:
        """把映射后的 df 规范成 minute canonical 列。"""
        if df.is_empty():
            return df
        if "datetime" in df.columns and df.schema["datetime"] != pl.Datetime("us"):
            if df.schema["datetime"] == pl.Utf8:
                # 字符串 datetime 直接 cast 会整体置 null (polars 不做字符串解析);
                # 先解析再对齐微秒精度 (#225, 参照
                # kline_sync._enforce_minute_beijing_wallclock 的处理)。
                # Series 级立即解析: 表达式错误要到 collect 才抛, 无法按格式回退
                df = df.with_columns(cls._parse_datetime_series(df["datetime"]))
            df = df.with_columns(pl.col("datetime").cast(pl.Datetime("us"), strict=False))
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in df.columns:
                df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
        keep = [c for c in ("symbol", "datetime", "open", "high", "low", "close", "volume", "amount") if c in df.columns]
        return df.select(keep) if keep else pl.DataFrame()

    _DATETIME_STR_FORMATS = (
        None,  # 自动推断
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    )

    @classmethod
    def _parse_datetime_series(cls, s: pl.Series) -> pl.Series:
        """逐格式尝试解析字符串 datetime; 均失败返回全 null (宽松语义)。"""
        for fmt in cls._DATETIME_STR_FORMATS:
            try:
                return (
                    s.str.to_datetime(strict=False, format=fmt)
                    if fmt else s.str.to_datetime(strict=False)
                )
            except Exception:
                continue
        return pl.Series("datetime", [None] * s.len(), dtype=pl.Datetime("us"))

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        cfg = self._dataset(dataset)
        test_symbols = symbols or ["000001.SZ"]
        end_time = cn_now()
        start_time = end_time - timedelta(days=7)
        if dataset == "realtime":
            rows = self._request_rows(cfg)
        elif dataset in {"minute", "full_minute", "instruments"}:
            override: dict[str, Any] = {}
            if cfg.asset_type_param:
                override[cfg.asset_type_param] = "stock"
            if dataset in {"minute", "full_minute"} and cfg.freq_param:
                override[cfg.freq_param] = "1m"
            rows = self._request_rows(
                cfg,
                symbols=test_symbols if dataset != "instruments" else None,
                start_time=start_time if dataset != "instruments" else None,
                end_time=end_time if dataset != "instruments" else None,
                override_params=override or None,
                override_body=override or None,
            )
        elif dataset in {"daily", "adj_factor"}:
            rows = self._request_rows(
                cfg,
                symbols=test_symbols,
                start_time=start_time,
                end_time=end_time,
            )
        else:
            rows = self._request_rows(cfg, symbols=test_symbols)
        df = self._mapped_frame(cfg, rows)
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": len(rows),
            "columns": df.columns,
            "preview": df.head(5).to_dicts() if not df.is_empty() else [],
        }

    def _dataset(self, name: str) -> DatasetConfig:
        cfg = self.config.datasets.get(name)
        if not cfg:
            raise ValueError(f"Custom data source '{self.name}' does not configure dataset '{name}'")
        return cfg

    def _mapped_frame(self, cfg: DatasetConfig, rows: list[dict]) -> pl.DataFrame:
        df = map_rows(rows, cfg.field_map)
        return apply_transforms(df, cfg.transforms)

    def _request_rows(
        self,
        cfg: DatasetConfig,
        *,
        symbols: list[str] | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        override_params: dict[str, Any] | None = None,
        override_body: dict[str, Any] | None = None,
    ) -> list[dict]:
        headers, auth_params = self._auth_parts()
        params = dict(cfg.params)
        params.update(auth_params)
        if override_params:
            params.update(override_params)
        body = dict(cfg.body)
        if override_body:
            body.update(override_body)
        if symbols:
            body[cfg.symbols_param] = symbols
            params.setdefault(cfg.symbols_param, ",".join(symbols))
        start_value = datetime_payload(start_time)
        end_value = datetime_payload(end_time)
        if start_value:
            body[cfg.start_param] = start_value
            params.setdefault(cfg.start_param, start_value)
        if end_value:
            body[cfg.end_param] = end_value
            params.setdefault(cfg.end_param, end_value)

        method = cfg.method.upper()
        request_kwargs: dict[str, Any] = {"headers": headers, "timeout": cfg.timeout}
        if method == "GET":
            request_kwargs["params"] = params
        else:
            request_kwargs["params"] = auth_params
            request_kwargs["json"] = body
        try:
            resp = self._client.request(method, cfg.url, **request_kwargs)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise RuntimeError(
                f"custom data source request failed: HTTP {status}"
            ) from None
        except httpx.HTTPError:
            # httpx 异常常带完整 request URL；query auth 会把 token 放在 URL 中，
            # 因此跨日志/API 边界只传固定脱敏说明。
            raise RuntimeError("custom data source request failed: network error") from None
        return extract_rows(resp.json(), cfg.response_path)

    def _auth_parts(self) -> tuple[dict[str, str], dict[str, str]]:
        auth = self.config.auth
        if auth.type == "none":
            return {}, {}
        if auth.type not in _AUTH_TYPES:
            raise RuntimeError(f"custom data source {self.name} has unsupported auth type")
        if not auth.token_env or not _ENV_NAME_RE.fullmatch(auth.token_env):
            raise RuntimeError(f"custom data source {self.name} auth token_env is invalid")
        token = _token_from_env(auth.token_env)
        if not token:
            raise RuntimeError(f"custom data source {self.name} auth token is not set")
        if auth.type == "bearer":
            return {auth.header: f"Bearer {token}"}, {}
        if auth.type == "header":
            return {auth.header: token}, {}
        if auth.type == "query":
            return {}, {auth.param: token}
        raise RuntimeError(f"custom data source {self.name} has unsupported auth type")


def _token_from_env(name: str | None) -> str | None:
    if not name:
        return None
    token = os.getenv(name)
    if token:
        return token
    candidates = [settings.data_dir.parent / ".env", Path.cwd() / ".env", Path.cwd().parent / ".env"]
    env_path = next((path for path in candidates if path.exists()), None)
    if env_path is None:
        return None
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001
        return None
    return None
