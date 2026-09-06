"""市场主线(market_mainline)与过滤配置单元测试。"""
from __future__ import annotations

import os
from datetime import date

import polars as pl
import pytest

from app.services import market_mainline, preferences
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField


def _write_enriched(root, rows: list[dict]) -> None:
    enriched = root / "kline_daily_enriched"
    by_date: dict[date, list[dict]] = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)
    for d, day_rows in by_date.items():
        part = enriched / f"date={d.isoformat()}" / "part.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(day_rows).write_parquet(part)


def _fake_repo(tmp_path):
    import types

    return types.SimpleNamespace(store=types.SimpleNamespace(data_dir=tmp_path))


def _patch_map(
    monkeypatch,
    mapping: dict[str, list[str]],
    kind: str = "concept",
    effective_date: date = date(2024, 1, 2),
) -> None:
    map_df = pl.DataFrame(
        {
            "_source_id": ["test_source" for _, ms in mapping.items() for _ in ms],
            "_effective_date": [effective_date for _, ms in mapping.items() for _ in ms],
            "_sym_up": [s for s, ms in mapping.items() for _ in ms],
            kind: [m for _, ms in mapping.items() for m in ms],
        },
        schema={
            "_source_id": pl.Utf8,
            "_effective_date": pl.Date,
            "_sym_up": pl.Utf8,
            kind: pl.Utf8,
        },
    ).unique()

    def fake_load(repo, k="concept"):
        return map_df if k == kind else pl.DataFrame()

    monkeypatch.setattr(market_mainline, "_load_point_in_time_map_df", fake_load)


def _mk_rows(d: date, spec: list[tuple[str, int, float]]) -> list[dict]:
    return [
        {"symbol": sym, "date": d, "consecutive_limit_ups": consec, "amount": amt}
        for sym, consec, amt in spec
    ]


class TestComputeMainline:
    def _setup(self, tmp_path, monkeypatch):
        d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
        # 概念 X: d1 三个涨停(2,1,1), d2 三个涨停(3,2,1); 概念 Y: 单股 2 板
        # S5 无概念映射; 大概念 BIG 成员 700 家但只有 5 家涨停(数据里只写 5 行)
        rows = _mk_rows(d1, [("S1.SH", 2, 5e8), ("S2.SH", 1, 1e8), ("S3.SH", 1, 2e8),
                             ("S4.SH", 2, 3e8), ("S5.SH", 1, 1e8),
                             ("B1.SH", 1, 1e8), ("B2.SH", 1, 1e8)])
        rows += _mk_rows(d2, [("S1.SH", 3, 6e8), ("S2.SH", 2, 2e8), ("S3.SH", 0, 1e8),
                              ("S4.SH", 3, 4e8), ("S5.SH", 1, 1e8),
                              ("B1.SH", 2, 1e8), ("B2.SH", 0, 1e8)])
        _write_enriched(tmp_path, rows)
        mapping = {
            "S1.SH": ["X"], "S2.SH": ["X"], "S3.SH": ["X"],
            "S4.SH": ["X", "Y"], "S5.SH": [],
            "B1.SH": ["BIG"], "B2.SH": ["BIG"],
            **{f"F{i}.SH": ["BIG"] for i in range(700)},  # BIG 成员 702 → 超 600 上限
        }
        _patch_map(monkeypatch, mapping)
        return _fake_repo(tmp_path), d1, d2

    def test_aggregation_and_big_concept_filter(self, tmp_path, monkeypatch):
        repo, d1, d2 = self._setup(tmp_path, monkeypatch)
        out = market_mainline.compute_mainline_range(
            repo, tmp_path, d1, d2, kind="concept",
            filter_cfg={"min_members": 4, "max_members": 600, "blacklist": []},
        )
        members = set(out["member"].to_list())
        assert "BIG" not in members  # 成员数超上限被过滤
        assert "X" in members
        x_d2 = out.filter((pl.col("date") == d2) & (pl.col("member") == "X")).to_dicts()[0]
        assert x_d2["limit_up_count"] == 3        # S1,S2,S4
        assert x_d2["ge2_count"] == 3
        assert x_d2["max_boards"] == 3
        assert x_d2["rungs_filled"] == 2          # 档位 {2,3}
        assert x_d2["leader_symbol"] == "S1.SH"   # 最高板且成交额大
        assert x_d2["rank"] == 1

    def test_blacklist_and_min_limit_up(self, tmp_path, monkeypatch):
        repo, d1, d2 = self._setup(tmp_path, monkeypatch)
        out = market_mainline.compute_mainline_range(
            repo, tmp_path, d1, d2, kind="concept",
            filter_cfg={"min_members": 1, "max_members": 5000, "blacklist": ["X"]},
        )
        # X 被黑名单; BIG 只有 2-3 家涨停 < _MIN_LIMIT_UP=3 也不参与 → 只剩空/无 X
        assert "X" not in set(out["member"].to_list())

    def test_upsert_replaces_same_day_kind(self, tmp_path, monkeypatch):
        repo, d1, d2 = self._setup(tmp_path, monkeypatch)
        cfg = {"min_members": 4, "max_members": 600, "blacklist": []}
        first = market_mainline.compute_mainline_range(repo, tmp_path, d1, d1, kind="concept", filter_cfg=cfg)
        market_mainline.upsert_mainline_history(tmp_path, first)
        both = market_mainline.compute_mainline_range(repo, tmp_path, d1, d2, kind="concept", filter_cfg=cfg)
        market_mainline.upsert_mainline_history(tmp_path, both)
        stored = pl.read_parquet(market_mainline.mainline_path(tmp_path))
        assert set(stored["date"].to_list()) == {d1, d2}
        # 同日重算不产生重复行
        assert stored.filter(pl.col("date") == d1).height == first.height

    def test_range_replace_clears_stale_rows_when_recompute_is_empty(
        self,
        tmp_path,
        monkeypatch,
    ):
        """指定范围重算为空时，也必须删除该维度的历史旧行。"""
        repo, d1, d2 = self._setup(tmp_path, monkeypatch)
        rows = market_mainline.compute_mainline_range(
            repo,
            tmp_path,
            d1,
            d2,
            kind="concept",
            filter_cfg={"min_members": 1, "max_members": 5000, "blacklist": []},
        )
        industry = rows.with_columns(pl.lit("industry").alias("kind"))
        market_mainline.upsert_mainline_history(tmp_path, rows)
        market_mainline.upsert_mainline_history(tmp_path, industry)

        market_mainline.replace_mainline_history_range(
            tmp_path,
            pl.DataFrame(),
            start=d1,
            end=d2,
            kind="concept",
        )

        stored = pl.read_parquet(market_mainline.mainline_path(tmp_path))
        assert stored.filter(pl.col("kind") == "concept").is_empty()
        assert not stored.filter(pl.col("kind") == "industry").is_empty()

    def test_incremental_fills_missing_days(self, tmp_path, monkeypatch):
        repo, d1, d2 = self._setup(tmp_path, monkeypatch)
        cfg = {"min_members": 4, "max_members": 600, "blacklist": []}
        first = market_mainline.compute_mainline_range(repo, tmp_path, d1, d1, kind="concept", filter_cfg=cfg)
        market_mainline.upsert_mainline_history(tmp_path, first)
        filter_config = market_mainline.load_mainline_filter_config()
        market_mainline._mark_mainline_dates_processed(
            tmp_path,
            repo,
            "concept",
            {d1},
            filter_version=market_mainline._mainline_filter_version(filter_config),
        )
        new = market_mainline.compute_mainline_incremental(repo, tmp_path, kind="concept")
        assert not new.is_empty()
        assert set(new["date"].to_list()) == {d2}

    def test_incremental_records_dates_with_no_mainline_results(self, tmp_path, monkeypatch):
        """无结果日期也应记录为已处理，后续增量不得反复扫描同一区间。"""
        d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
        repo = _fake_repo(tmp_path)
        calls: list[tuple[date, date, str]] = []
        monkeypatch.setattr(
            "app.services.regime_builder.enriched_date_set",
            lambda current_repo: {d1, d2},
        )

        def compute(current_repo, data_dir, start, end, kind="concept", **kwargs):
            calls.append((start, end, kind))
            return pl.DataFrame()

        monkeypatch.setattr(market_mainline, "compute_mainline_range", compute)

        assert market_mainline.compute_mainline_incremental(
            repo, tmp_path, today=d2, kind="concept",
        ).is_empty()
        assert market_mainline.compute_mainline_incremental(
            repo, tmp_path, today=d2, kind="concept",
        ).is_empty()

        assert calls == [(d1, d2, "concept")]

    def test_incremental_recomputes_after_filter_config_change(
        self,
        tmp_path,
        monkeypatch,
    ):
        """过滤配置版本变化后，所有已处理日期都必须重新计算。"""
        d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
        repo = _fake_repo(tmp_path)
        calls: list[tuple[date, date, str]] = []
        current_config = {
            "min_members": 4,
            "max_members": 600,
            "blacklist": [],
            "exclude_st": True,
        }
        monkeypatch.setattr(
            "app.services.regime_builder.enriched_date_set",
            lambda current_repo: {d1, d2},
        )
        monkeypatch.setattr(
            market_mainline,
            "load_mainline_filter_config",
            lambda: current_config.copy(),
        )

        def compute(current_repo, data_dir, start, end, kind="concept", **kwargs):
            calls.append((start, end, kind))
            return pl.DataFrame()

        monkeypatch.setattr(market_mainline, "compute_mainline_range", compute)
        market_mainline.compute_mainline_incremental(
            repo,
            tmp_path,
            today=d2,
            kind="concept",
        )
        market_mainline.compute_mainline_incremental(
            repo,
            tmp_path,
            today=d2,
            kind="concept",
        )
        current_config["min_members"] = 5
        market_mainline.compute_mainline_incremental(
            repo,
            tmp_path,
            today=d2,
            kind="concept",
        )

        assert calls == [
            (d1, d2, "concept"),
            (d1, d2, "concept"),
        ]

    def test_full_snapshot_records_all_no_result_dates(self, tmp_path, monkeypatch):
        """全量发布也要为两个维度记录空结果日期的完成水位。"""
        d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
        _write_enriched(
            tmp_path,
            _mk_rows(d1, [("S1.SH", 0, 1e8)])
            + _mk_rows(d2, [("S1.SH", 0, 1e8)]),
        )
        repo = _fake_repo(tmp_path)
        results = {"concept": pl.DataFrame(), "industry": pl.DataFrame()}

        market_mainline.replace_mainline_history_full(
            tmp_path,
            results,
            repo,
            start=d1,
            end=d2,
        )

        coverage = pl.read_parquet(market_mainline.mainline_coverage_path(tmp_path))
        assert set(zip(coverage["date"], coverage["kind"], strict=True)) == {
            (d1, "concept"),
            (d1, "industry"),
            (d2, "concept"),
            (d2, "industry"),
        }
        calls: list[tuple[date, date, str]] = []

        def compute(current_repo, data_dir, start, end, kind="concept", **kwargs):
            calls.append((start, end, kind))
            return pl.DataFrame()

        monkeypatch.setattr(market_mainline, "compute_mainline_range", compute)
        market_mainline.compute_mainline_incremental(
            repo,
            tmp_path,
            today=d2,
            kind="concept",
        )
        market_mainline.compute_mainline_incremental(
            repo,
            tmp_path,
            today=d2,
            kind="industry",
        )
        assert calls == []

    def test_incremental_defaults_to_cn_business_date(self, tmp_path, monkeypatch):
        """未显式传 today 时应使用北京时间业务日。"""
        target = date(2099, 1, 2)
        repo = _fake_repo(tmp_path)
        calls: list[tuple[date, date, str]] = []
        monkeypatch.setattr(
            market_mainline,
            "cn_today",
            lambda: target,
            raising=False,
        )
        monkeypatch.setattr(
            "app.services.regime_builder.enriched_date_set",
            lambda current_repo: {target},
        )

        def compute(current_repo, data_dir, start, end, kind="concept", **kwargs):
            calls.append((start, end, kind))
            return pl.DataFrame()

        monkeypatch.setattr(market_mainline, "compute_mainline_range", compute)

        market_mainline.compute_mainline_incremental(
            repo,
            tmp_path,
            kind="concept",
        )

        assert calls == [(target, target, "concept")]

    def test_incremental_recomputes_overwritten_enriched_partition(
        self,
        tmp_path,
        monkeypatch,
    ):
        """已处理 enriched 分区被覆写后，仍应重新计算对应交易日。"""
        repo, d1, d2 = self._setup(tmp_path, monkeypatch)
        calls: list[tuple[date, date, str]] = []

        def compute(current_repo, data_dir, start, end, kind="concept", **kwargs):
            calls.append((start, end, kind))
            return pl.DataFrame()

        monkeypatch.setattr(market_mainline, "compute_mainline_range", compute)

        market_mainline.compute_mainline_incremental(
            repo, tmp_path, today=d2, kind="concept",
        )
        market_mainline.compute_mainline_incremental(
            repo, tmp_path, today=d2, kind="concept",
        )

        overwritten = (
            tmp_path / "kline_daily_enriched" / f"date={d2.isoformat()}" / "part.parquet"
        )
        future = overwritten.stat().st_mtime + 10
        os.utime(overwritten, (future, future))

        market_mainline.compute_mainline_incremental(
            repo, tmp_path, today=d2, kind="concept",
        )

        assert calls == [
            (d1, d2, "concept"),
            (d2, d2, "concept"),
        ]

    def test_incremental_rejects_source_change_before_publish(
        self,
        tmp_path,
        monkeypatch,
    ):
        """增量主线计算期间来源变化时不得发布旧结果或新水位。"""
        repo, d1, d2 = self._setup(tmp_path, monkeypatch)
        overwritten = (
            tmp_path
            / "kline_daily_enriched"
            / f"date={d2.isoformat()}"
            / "part.parquet"
        )
        published: list[object] = []

        def change_source_during_compute(*args, **kwargs):
            overwritten.write_bytes(b"new-source-version")
            return pl.DataFrame()

        monkeypatch.setattr(
            market_mainline,
            "compute_mainline_range",
            change_source_during_compute,
        )
        monkeypatch.setattr(
            market_mainline,
            "replace_mainline_history_range",
            lambda *args, **kwargs: published.append(args),
        )

        with pytest.raises(market_mainline.MainlineSourceChangedError):
            market_mainline.compute_mainline_incremental(
                repo,
                tmp_path,
                today=d2,
                kind="concept",
            )

        assert published == []
        assert not market_mainline.mainline_coverage_path(tmp_path).exists()

    def test_incremental_recomputes_dates_affected_by_new_membership_snapshot(
        self,
        tmp_path,
        monkeypatch,
    ):
        """成分快照新增版本后，只重算其生效日起的已处理交易日。"""
        d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
        repo = _fake_repo(tmp_path)
        config = ExtConfig(
            id="versioned_concepts",
            label="历史概念",
            mode="timeseries",
            fields=[
                ExtField("symbol", "string", "标的代码"),
                ExtField("concept", "string", "概念"),
            ],
        )
        ExtConfigStore(tmp_path).upsert(config)
        first_part = (
            tmp_path
            / "ext_data"
            / config.id
            / "timeseries"
            / f"date={d1}"
            / "part.parquet"
        )
        first_part.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"symbol": ["S1.SH"], "concept": ["旧概念"]}).write_parquet(
            first_part,
        )
        calls: list[tuple[date, date, str]] = []
        monkeypatch.setattr(
            "app.services.regime_builder.enriched_date_set",
            lambda current_repo: {d1, d2},
        )

        def compute(current_repo, data_dir, start, end, kind="concept", **kwargs):
            calls.append((start, end, kind))
            return pl.DataFrame()

        monkeypatch.setattr(market_mainline, "compute_mainline_range", compute)

        market_mainline.compute_mainline_incremental(
            repo, tmp_path, today=d2, kind="concept",
        )
        market_mainline.compute_mainline_incremental(
            repo, tmp_path, today=d2, kind="concept",
        )

        second_part = (
            tmp_path
            / "ext_data"
            / config.id
            / "timeseries"
            / f"date={d2}"
            / "part.parquet"
        )
        second_part.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"symbol": ["S1.SH"], "concept": ["新概念"]}).write_parquet(
            second_part,
        )
        market_mainline.compute_mainline_incremental(
            repo, tmp_path, today=d2, kind="concept",
        )

        assert calls == [
            (d1, d2, "concept"),
            (d2, d2, "concept"),
        ]

    def test_incremental_clears_stale_rows_when_recompute_is_empty(
        self,
        tmp_path,
        monkeypatch,
    ):
        """被覆写日期不再产出主线时，旧结果必须随重算一起清除。"""
        repo, d1, d2 = self._setup(tmp_path, monkeypatch)
        initial = market_mainline.compute_mainline_incremental(
            repo,
            tmp_path,
            today=d2,
            kind="concept",
        )
        assert set(initial["date"].to_list()) == {d1, d2}

        _write_enriched(
            tmp_path,
            _mk_rows(d2, [
                ("S1.SH", 0, 6e8),
                ("S2.SH", 0, 2e8),
                ("S3.SH", 0, 1e8),
                ("S4.SH", 0, 4e8),
            ]),
        )
        overwritten = (
            tmp_path / "kline_daily_enriched" / f"date={d2.isoformat()}" / "part.parquet"
        )
        future = overwritten.stat().st_mtime + 10
        os.utime(overwritten, (future, future))

        refreshed = market_mainline.compute_mainline_incremental(
            repo,
            tmp_path,
            today=d2,
            kind="concept",
        )

        assert refreshed.is_empty()
        stored = market_mainline.load_mainline_history(tmp_path, "concept")
        assert set(stored["date"].to_list()) == {d1}

    def test_incremental_clears_rows_and_coverage_for_deleted_enriched_date(
        self,
        tmp_path,
        monkeypatch,
    ):
        """来源分区被删除后，旧主线结果与完成水位都必须清除。"""
        repo, d1, d2 = self._setup(tmp_path, monkeypatch)
        initial = market_mainline.compute_mainline_incremental(
            repo,
            tmp_path,
            today=d2,
            kind="concept",
        )
        assert set(initial["date"].to_list()) == {d1, d2}

        deleted = (
            tmp_path / "kline_daily_enriched" / f"date={d2.isoformat()}" / "part.parquet"
        )
        deleted.unlink()

        refreshed = market_mainline.compute_mainline_incremental(
            repo,
            tmp_path,
            today=d2,
            kind="concept",
        )

        assert refreshed.is_empty()
        stored = market_mainline.load_mainline_history(tmp_path, "concept")
        assert set(stored["date"].to_list()) == {d1}
        coverage = pl.read_parquet(market_mainline.mainline_coverage_path(tmp_path))
        concept_dates = set(
            coverage.filter(pl.col("kind") == "concept")["date"].to_list()
        )
        assert concept_dates == {d1}

    def test_industry_level_truncation(self, tmp_path, monkeypatch):
        d1 = date(2024, 1, 2)
        rows = _mk_rows(d1, [("S1.SH", 2, 5e8), ("S2.SH", 1, 1e8),
                             ("S3.SH", 1, 2e8), ("S4.SH", 3, 4e8)])
        _write_enriched(tmp_path, rows)
        _patch_map(
            monkeypatch,
            {"S1.SH": ["计算机-软件开发-垂直应用软件"],
             "S2.SH": ["计算机-软件开发-垂直应用软件"],
             "S3.SH": ["计算机-IT服务-IT服务Ⅲ"],
             "S4.SH": ["计算机-软件开发-垂直应用软件"]},
            kind="industry",
        )
        out = market_mainline.compute_mainline_range(
            _fake_repo(tmp_path), tmp_path, d1, d1, kind="industry",
            filter_cfg={"min_members": 1, "max_members": 5000, "blacklist": []},
        )
        members = set(out["member"].to_list())
        assert "计算机-软件开发" in members
        assert all(m.count("-") <= 1 for m in members)
        sw = out.filter(pl.col("member") == "计算机-软件开发").to_dicts()[0]
        assert sw["limit_up_count"] == 3
        assert sw["max_boards"] == 3

    def test_snapshot_membership_is_not_backfilled_into_history(self, tmp_path):
        """没有生效日期的当前概念快照不能用于历史主线重算。"""
        historical_date = date(2024, 1, 2)
        symbols = ["S1.SH", "S2.SH", "S3.SH"]
        _write_enriched(
            tmp_path,
            _mk_rows(historical_date, [(symbol, 1, 1e8) for symbol in symbols]),
        )
        config = ExtConfig(
            id="snapshot_concepts",
            label="当前概念",
            mode="snapshot",
            fields=[
                ExtField("symbol", "string", "标的代码"),
                ExtField("concept", "string", "概念"),
            ],
        )
        ExtConfigStore(tmp_path).upsert(config)
        snapshot = tmp_path / "ext_data" / config.id / "part.parquet"
        pl.DataFrame({
            "symbol": symbols,
            "concept": ["后来新增"] * len(symbols),
        }).write_parquet(snapshot)

        out = market_mainline.compute_mainline_range(
            _fake_repo(tmp_path),
            tmp_path,
            historical_date,
            historical_date,
            kind="concept",
            filter_cfg={"min_members": 1, "max_members": 5000, "blacklist": []},
            exclude_st=False,
        )

        assert out.is_empty()

    def test_timeseries_membership_uses_version_effective_on_each_date(self, tmp_path):
        """带日期的成分快照按生效日切换，不把新版本回填到旧交易日。"""
        first_date = date(2024, 1, 2)
        second_date = date(2024, 1, 3)
        symbols = ["S1.SH", "S2.SH", "S3.SH"]
        _write_enriched(
            tmp_path,
            _mk_rows(first_date, [(symbol, 1, 1e8) for symbol in symbols])
            + _mk_rows(second_date, [(symbol, 1, 1e8) for symbol in symbols]),
        )
        config = ExtConfig(
            id="versioned_concepts",
            label="历史概念",
            mode="timeseries",
            fields=[
                ExtField("symbol", "string", "标的代码"),
                ExtField("concept", "string", "概念"),
            ],
        )
        ExtConfigStore(tmp_path).upsert(config)
        for effective, member in ((first_date, "旧概念"), (second_date, "新概念")):
            part = (
                tmp_path
                / "ext_data"
                / config.id
                / "timeseries"
                / f"date={effective}"
                / "part.parquet"
            )
            part.parent.mkdir(parents=True, exist_ok=True)
            pl.DataFrame({
                "symbol": symbols,
                "concept": [member] * len(symbols),
            }).write_parquet(part)

        out = market_mainline.compute_mainline_range(
            _fake_repo(tmp_path),
            tmp_path,
            first_date,
            second_date,
            kind="concept",
            filter_cfg={"min_members": 1, "max_members": 5000, "blacklist": []},
            exclude_st=False,
        )

        assert out.select(["date", "member"]).to_dicts() == [
            {"date": first_date, "member": "旧概念"},
            {"date": second_date, "member": "新概念"},
        ]

    def test_timeseries_sources_advance_on_independent_schedules(self, tmp_path):
        """一个来源更新时，其他来源的上一版成分仍应继续生效。"""
        first_date = date(2024, 1, 2)
        second_date = date(2024, 1, 3)
        first_symbols = ["S1.SH", "S2.SH", "S3.SH"]
        second_symbols = ["T1.SH", "T2.SH", "T3.SH"]
        _write_enriched(
            tmp_path,
            _mk_rows(first_date, [(symbol, 1, 1e8) for symbol in first_symbols])
            + _mk_rows(
                second_date,
                [(symbol, 1, 1e8) for symbol in first_symbols + second_symbols],
            ),
        )

        for config_id, effective, symbols, member in (
            ("source_a", first_date, first_symbols, "来源甲概念"),
            ("source_b", second_date, second_symbols, "来源乙概念"),
        ):
            config = ExtConfig(
                id=config_id,
                label=config_id,
                mode="timeseries",
                fields=[
                    ExtField("symbol", "string", "标的代码"),
                    ExtField("concept", "string", "概念"),
                ],
            )
            ExtConfigStore(tmp_path).upsert(config)
            part = (
                tmp_path
                / "ext_data"
                / config.id
                / "timeseries"
                / f"date={effective}"
                / "part.parquet"
            )
            part.parent.mkdir(parents=True, exist_ok=True)
            pl.DataFrame({
                "symbol": symbols,
                "concept": [member] * len(symbols),
            }).write_parquet(part)

        out = market_mainline.compute_mainline_range(
            _fake_repo(tmp_path),
            tmp_path,
            first_date,
            second_date,
            kind="concept",
            filter_cfg={"min_members": 1, "max_members": 5000, "blacklist": []},
            exclude_st=False,
        )

        assert set(out.select(["date", "member"]).iter_rows()) == {
            (first_date, "来源甲概念"),
            (second_date, "来源甲概念"),
            (second_date, "来源乙概念"),
        }


class TestMainlineFilterPreferences:
    def test_filter_config_uses_one_preferences_snapshot(self, monkeypatch):
        """汇总读取不得拼接多次 load 的不同版本。"""
        calls = 0

        def load_once():
            nonlocal calls
            calls += 1
            return {
                "mainline_min_members": 7,
                "mainline_max_members": 800,
                "mainline_blacklist": "标签甲,标签乙",
                "sentiment_exclude_st": False,
            }

        monkeypatch.setattr(preferences, "load", load_once)

        assert preferences.get_mainline_filter_config() == {
            "min_members": 7,
            "max_members": 800,
            "blacklist": ["标签甲", "标签乙"],
            "exclude_st": False,
        }
        assert calls == 1

    def test_blacklist_string_parsing_and_clamp(self, tmp_path, monkeypatch):
        path = tmp_path / "preferences.json"
        monkeypatch.setattr(preferences, "_path", lambda: path)
        got = preferences.set_mainline_filter_config({
            "max_members": 99999,          # 超上限被夹到 5000
            "min_members": 0,              # 低于下限被夹到 1
            "blacklist": "融资融券, 沪股通；深股通",  # noqa: RUF001
        })
        assert got["max_members"] == 5000
        assert got["min_members"] == 1
        assert set(got["blacklist"]) == {"融资融券", "沪股通", "深股通"}
        # 部分更新: 只改黑名单, 其他保持
        got2 = preferences.set_mainline_filter_config({"blacklist": ["ST板块"]})
        assert got2["blacklist"] == ["ST板块"]
        assert got2["max_members"] == 5000

    def test_defaults(self, tmp_path, monkeypatch):
        path = tmp_path / "preferences.json"
        monkeypatch.setattr(preferences, "_path", lambda: path)
        cfg = preferences.get_mainline_filter_config()
        assert cfg == {"min_members": 4, "max_members": 600, "blacklist": [], "exclude_st": True}

    def test_sentiment_exclude_st_roundtrip(self, tmp_path, monkeypatch):
        path = tmp_path / "preferences.json"
        monkeypatch.setattr(preferences, "_path", lambda: path)
        assert preferences.get_sentiment_exclude_st() is True  # 默认剔除
        assert preferences.set_sentiment_exclude_st(False) is False
        assert preferences.get_sentiment_exclude_st() is False
        # 经主线过滤配置部分更新同样生效
        got = preferences.set_mainline_filter_config({"exclude_st": True})
        assert got["exclude_st"] is True


class TestExcludeST:
    """风险警示股剔除: 维表名称含 ST → 主线聚合前过滤。"""

    @staticmethod
    def _write_instruments(tmp_path, names: dict[str, str]) -> None:
        part = tmp_path / "instruments" / "part.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({
            "symbol": list(names),
            "name": list(names.values()),
        }).write_parquet(part)

    def _reset_cache(self, monkeypatch):
        monkeypatch.setattr(market_mainline, "_ST_SYMBOLS_CACHE", None)

    def test_load_risk_warning_symbols(self, tmp_path, monkeypatch):
        self._reset_cache(monkeypatch)
        self._write_instruments(tmp_path, {
            "s1.SH": "*ST环保", "S2.SH": "ST万邦", "S3.SZ": "正常股",
            "s4.BJ": "S*ST京", "S5.SH": "斯太尔",  # 中文名含"斯"不含 ST 标记
        })
        got = market_mainline.load_risk_warning_symbols(tmp_path)
        assert got == frozenset({"S1.SH", "S2.SH", "S4.BJ"})  # 大写归一
        # 缓存命中: 再次读取不重扫磁盘
        self._write_instruments(tmp_path, {"S9.SH": "ST新增"})
        assert market_mainline.load_risk_warning_symbols(tmp_path) == got

    def test_load_risk_warning_symbols_empty_dir(self, tmp_path, monkeypatch):
        self._reset_cache(monkeypatch)
        assert market_mainline.load_risk_warning_symbols(tmp_path) == frozenset()

    def test_compute_mainline_does_not_backfill_current_st_status(
        self,
        tmp_path,
        monkeypatch,
    ):
        """当前 ST 名称无历史生效日期时，历史主线不执行该过滤。"""
        self._reset_cache(monkeypatch)
        self._write_instruments(tmp_path, {"S1.SH": "*ST一", "S2.SH": "正常一"})
        repo, d1, d2 = TestComputeMainline()._setup(tmp_path, monkeypatch)
        cfg = {"min_members": 4, "max_members": 600, "blacklist": []}

        out = market_mainline.compute_mainline_range(
            repo, tmp_path, d1, d2, kind="concept", filter_cfg=cfg, exclude_st=True,
        )
        x_d1 = out.filter((pl.col("date") == d1) & (pl.col("member") == "X")).to_dicts()[0]
        assert x_d1["limit_up_count"] == 4
        assert x_d1["ge2_count"] == 2
        assert x_d1["max_boards"] == 2
        assert x_d1["leader_symbol"] == "S1.SH"

        out_keep = market_mainline.compute_mainline_range(
            repo, tmp_path, d1, d2, kind="concept", filter_cfg=cfg, exclude_st=False,
        )
        x_d1_keep = out_keep.filter((pl.col("date") == d1) & (pl.col("member") == "X")).to_dicts()[0]
        assert x_d1_keep["limit_up_count"] == 4
        assert x_d1_keep["ge2_count"] == 2
        assert x_d1_keep["leader_symbol"] == "S1.SH"
