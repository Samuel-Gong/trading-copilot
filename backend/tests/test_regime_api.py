"""市场环境 API 的时间窗口契约回归测试。"""
from __future__ import annotations

import threading
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import regime
from app.services import atomic_parquet, market_mainline, preferences
from app.services.market_environment_lock import market_environment_snapshot


def _request(tmp_path):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
            ),
        ),
    )


def _mainline_snapshot(version: str, *, min_members: int = 4):
    config = {
        "min_members": min_members,
        "max_members": 600,
        "blacklist": [],
        "exclude_st": True,
    }
    return market_mainline.MainlineSourceSnapshot(
        coverage=pl.DataFrame({"version": ["coverage"]}),
        filter_config=config,
        filter_version=version,
    )


def _mainline_range_snapshot(
    target: date,
    version: str,
    *,
    min_members: int = 4,
):
    config = {
        "min_members": min_members,
        "max_members": 600,
        "blacklist": [],
        "exclude_st": True,
    }
    return market_mainline.MainlineSourceSnapshot(
        coverage=pl.DataFrame({
            "date": [target, target],
            "kind": ["concept", "industry"],
            "source_mtime_ns": [1, 1],
            "membership_version": [version, version],
            "filter_version": [version, version],
        }),
        filter_config=config,
        filter_version=version,
    )


def test_recompute_defaults_to_cn_today_and_replaces_complete_ranges(
    tmp_path,
    monkeypatch,
):
    """两个重算入口使用北京时间，并以请求范围完整替换旧结果。"""
    start = date(2099, 1, 1)
    business_today = date(2099, 1, 2)
    regime_ranges: list[tuple[date, date, bool]] = []
    mainline_ranges: list[tuple[str, date, date]] = []

    monkeypatch.setattr(
        regime,
        "cn_today",
        lambda: business_today,
        raising=False,
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "earliest_enriched_date",
        lambda repo: start,
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "run_regime_batch",
        lambda repo, start, end: pl.DataFrame(),
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "build_regime_history_full_snapshot",
        lambda data_dir, rows, repo, *, start, end, source_snapshot=None: (
            regime_ranges.append((start, end, rows.is_empty())) or []
        ),
        raising=False,
    )
    monkeypatch.setattr(regime.regime_builder, "refresh_phase_labels", lambda data_dir: 0)
    monkeypatch.setattr(
        market_mainline,
        "compute_mainline_range",
        lambda repo, data_dir, start, end, *, kind, filter_cfg: (
            mainline_ranges.append((kind, start, end)) or pl.DataFrame()
        ),
    )
    monkeypatch.setattr(
        market_mainline,
        "build_mainline_history_full_snapshot",
        lambda data_dir, rows_by_kind, repo, *, start, end, source_snapshot=None: (
            [],
            sum(frame.height for frame in rows_by_kind.values()),
        ),
    )
    monkeypatch.setattr(
        regime,
        "replace_parquet_set",
        lambda entries, **kwargs: None,
    )

    regime.regime_recompute(_request(tmp_path))
    regime.mainline_recompute(_request(tmp_path))

    assert regime_ranges == [(start, business_today, True)]
    assert mainline_ranges == [
        ("concept", start, business_today),
        ("industry", start, business_today),
        ("concept", start, business_today),
        ("industry", start, business_today),
    ]


def test_full_recompute_clears_derived_history_when_enriched_is_empty(tmp_path):
    """全部来源分区已删除时，全量重算必须清除 regime/mainline 结果与水位。"""
    regime_part = regime.regime_builder.regime_path(tmp_path)
    regime_coverage = regime.regime_builder.regime_coverage_path(tmp_path)
    mainline_part = market_mainline.mainline_path(tmp_path)
    mainline_coverage = market_mainline.mainline_coverage_path(tmp_path)
    for path in (regime_part, regime_coverage, mainline_part, mainline_coverage):
        path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"date": [date(2026, 1, 2)]}).write_parquet(path)

    result = regime.regime_recompute(_request(tmp_path))

    assert result == {"ok": True, "computed": 0, "phase_days": 0, "mainline_rows": 0}
    assert all(
        path.exists() and pl.read_parquet(path).is_empty()
        for path in (regime_part, regime_coverage, mainline_part, mainline_coverage)
    )


def test_full_recompute_empty_source_publish_failure_restores_all_files(
    tmp_path,
    monkeypatch,
):
    """无来源清理的任一文件发布失败时，四文件必须完整回滚。"""
    paths = [
        regime.regime_builder.regime_path(tmp_path),
        regime.regime_builder.regime_coverage_path(tmp_path),
        market_mainline.mainline_path(tmp_path),
        market_mainline.mainline_coverage_path(tmp_path),
    ]
    for index, path in enumerate(paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"value": [index]}).write_parquet(path)
    original_replace = atomic_parquet.os.replace

    def fail_mainline_publish(source, target):
        if Path(target) == paths[2] and Path(source).suffix == ".tmp":
            raise OSError("mainline busy")
        original_replace(source, target)

    monkeypatch.setattr(atomic_parquet.os, "replace", fail_mainline_publish)

    with pytest.raises(OSError, match="mainline busy"):
        regime.regime_recompute(_request(tmp_path))

    for index, path in enumerate(paths):
        assert pl.read_parquet(path)["value"].to_list() == [index]
    assert not regime.market_environment_journal_path(tmp_path).exists()


def test_mainline_recompute_clears_empty_source_in_one_transaction(tmp_path):
    """独立主线无来源清理必须同时发布空 history 与 coverage。"""
    paths = [
        market_mainline.mainline_path(tmp_path),
        market_mainline.mainline_coverage_path(tmp_path),
    ]
    for index, path in enumerate(paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"value": [index]}).write_parquet(path)

    result = regime.mainline_recompute(_request(tmp_path))

    assert result == {"ok": True, "rows": 0}
    assert all(path.exists() and pl.read_parquet(path).is_empty() for path in paths)


def test_mainline_recompute_empty_source_failure_restores_both_files(
    tmp_path,
    monkeypatch,
):
    """独立主线空源事务失败时不得留下 history/coverage 半提交。"""
    paths = [
        market_mainline.mainline_path(tmp_path),
        market_mainline.mainline_coverage_path(tmp_path),
    ]
    for index, path in enumerate(paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"value": [index]}).write_parquet(path)
    original_replace = atomic_parquet.os.replace

    def fail_coverage_publish(source, target):
        if Path(target) == paths[1] and Path(source).suffix == ".tmp":
            raise OSError("coverage busy")
        original_replace(source, target)

    monkeypatch.setattr(atomic_parquet.os, "replace", fail_coverage_publish)

    with pytest.raises(OSError, match="coverage busy"):
        regime.mainline_recompute(_request(tmp_path))

    for index, path in enumerate(paths):
        assert pl.read_parquet(path)["value"].to_list() == [index]
    assert not regime.market_environment_journal_path(tmp_path).exists()


@pytest.mark.parametrize("entrypoint", ["regime", "mainline"])
def test_empty_source_recompute_rejects_new_enriched_partition_before_publish(
    tmp_path,
    monkeypatch,
    entrypoint,
):
    """首次空源扫描后出现新分区时，组合与独立入口都必须 fail-closed。"""
    target = date(2026, 8, 25)
    published: list[object] = []
    monkeypatch.setattr(
        regime.regime_builder,
        "earliest_enriched_date",
        lambda _repo: None,
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "enriched_date_set",
        lambda _repo: {target},
    )
    monkeypatch.setattr(
        regime,
        "replace_parquet_set",
        lambda *args, **kwargs: published.append(args),
    )

    with pytest.raises(regime.regime_builder.RegimeSourceChangedError):
        if entrypoint == "regime":
            regime.regime_recompute(_request(tmp_path))
        else:
            regime.mainline_recompute(_request(tmp_path))

    assert published == []


def test_full_recompute_drops_history_before_new_earliest_source(tmp_path, monkeypatch):
    """删除最早来源分区后，全量重算不得保留新起点之前的旧派生日期。"""
    old_date = date(2026, 1, 1)
    new_earliest = date(2026, 1, 2)
    source = tmp_path / "kline_daily_enriched" / f"date={new_earliest}" / "part.parquet"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    regime.regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [old_date],
        "state": ["range"],
        "score": [50],
    }))
    pl.DataFrame({
        "date": [old_date],
        "source_mtime_ns": [1],
    }).write_parquet(regime.regime_builder.regime_coverage_path(tmp_path))
    market_mainline.mainline_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "date": [old_date],
        "kind": ["concept"],
        "rank": [1],
    }).write_parquet(market_mainline.mainline_path(tmp_path))
    pl.DataFrame({
        "date": [old_date],
        "kind": ["concept"],
    }).write_parquet(market_mainline.mainline_coverage_path(tmp_path))
    monkeypatch.setattr(
        regime.regime_builder,
        "earliest_enriched_date",
        lambda _repo: new_earliest,
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "run_regime_batch",
        lambda _repo, start, end: pl.DataFrame({
            "date": [new_earliest],
            "state": ["strong"],
            "score": [80],
        }),
    )
    monkeypatch.setattr(regime.regime_builder, "refresh_phase_labels", lambda _data_dir: 1)
    monkeypatch.setattr(
        market_mainline,
        "compute_mainline_range",
        lambda _repo, _data_dir, start, end, *, kind, filter_cfg: pl.DataFrame({
            "date": [new_earliest],
            "kind": [kind],
            "rank": [1],
        }),
    )

    regime.regime_recompute(_request(tmp_path), end=new_earliest)

    stored_regime = regime.regime_builder.load_regime_history(tmp_path)
    stored_mainline = market_mainline.load_mainline_history(tmp_path)
    assert set(stored_regime["date"].to_list()) == {new_earliest}
    assert set(stored_mainline["date"].to_list()) == {new_earliest}
    stored_regime_coverage = pl.read_parquet(
        regime.regime_builder.regime_coverage_path(tmp_path),
    )
    assert set(stored_regime_coverage["date"].to_list()) == {new_earliest}
    stored_mainline_coverage = pl.read_parquet(
        market_mainline.mainline_coverage_path(tmp_path),
    )
    assert set(stored_mainline_coverage["date"].to_list()) == {new_earliest}
    assert set(stored_mainline_coverage["kind"].to_list()) == {
        "concept",
        "industry",
    }


def test_mainline_full_recompute_rejects_source_change_before_publish(
    tmp_path,
    monkeypatch,
):
    """计算期间来源版本变化时 fail-closed，不能发布旧结果与新水位。"""
    target = date(2026, 8, 25)
    path = market_mainline.mainline_path(tmp_path)
    path.parent.mkdir(parents=True)
    pl.DataFrame({
        "date": [target],
        "kind": ["concept"],
        "rank": [1],
    }).write_parquet(path)
    before = _mainline_snapshot("before", min_members=4)
    after = _mainline_snapshot("after", min_members=5)
    snapshots = iter([before, after])
    used_configs: list[dict] = []
    monkeypatch.setattr(regime, "cn_today", lambda: target)
    monkeypatch.setattr(
        regime.regime_builder,
        "earliest_enriched_date",
        lambda repo: target,
    )
    monkeypatch.setattr(
        market_mainline,
        "capture_mainline_source_snapshot",
        lambda *args, **kwargs: next(snapshots),
    )
    monkeypatch.setattr(
        market_mainline,
        "compute_mainline_range",
        lambda *args, **kwargs: (
            used_configs.append(kwargs["filter_cfg"]) or pl.DataFrame()
        ),
    )

    with pytest.raises(
        market_mainline.MainlineSourceChangedError,
        match="来源已更新",
    ):
        regime.mainline_recompute(_request(tmp_path))

    stored = pl.read_parquet(path)
    assert stored["rank"].to_list() == [1]
    assert used_configs == [before.filter_config, before.filter_config]


def test_regime_full_recompute_rejects_source_change_before_publish(
    tmp_path,
    monkeypatch,
):
    """组合全量重算也必须在四文件发布前复验来源版本。"""
    target = date(2026, 8, 25)
    snapshots = iter([
        _mainline_snapshot("before"),
        _mainline_snapshot("after"),
    ])
    published: list[object] = []
    monkeypatch.setattr(
        regime.regime_builder,
        "earliest_enriched_date",
        lambda repo: target,
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "run_regime_batch",
        lambda *args, **kwargs: pl.DataFrame(),
    )
    monkeypatch.setattr(
        market_mainline,
        "capture_mainline_source_snapshot",
        lambda *args, **kwargs: next(snapshots),
    )
    monkeypatch.setattr(
        market_mainline,
        "compute_mainline_range",
        lambda *args, **kwargs: pl.DataFrame(),
    )
    monkeypatch.setattr(
        regime,
        "replace_parquet_set",
        lambda *args, **kwargs: published.append(args),
    )

    with pytest.raises(market_mainline.MainlineSourceChangedError):
        regime.regime_recompute(_request(tmp_path), end=target)

    assert published == []


def test_regime_full_recompute_rejects_index_source_change_before_publish(
    tmp_path,
    monkeypatch,
):
    """指数 enriched 在计算期间变化时，不得发布旧 regime 与新水位。"""
    target = date(2026, 8, 25)
    enriched = tmp_path / "kline_daily_enriched" / f"date={target}" / "part.parquet"
    index = tmp_path / "kline_index_enriched" / f"date={target}" / "part.parquet"
    enriched.parent.mkdir(parents=True)
    index.parent.mkdir(parents=True)
    enriched.write_bytes(b"stock-source")
    index.write_bytes(b"index-v1")
    published: list[object] = []
    monkeypatch.setattr(
        regime.regime_builder,
        "earliest_enriched_date",
        lambda repo: target,
    )

    def change_index_during_compute(*args, **kwargs):
        index.write_bytes(b"index-version-two")
        return pl.DataFrame()

    monkeypatch.setattr(
        regime.regime_builder,
        "run_regime_batch",
        change_index_during_compute,
    )
    monkeypatch.setattr(
        market_mainline,
        "compute_mainline_range",
        lambda *args, **kwargs: pl.DataFrame(),
    )
    monkeypatch.setattr(
        regime,
        "replace_parquet_set",
        lambda *args, **kwargs: published.append(args),
    )

    with pytest.raises(regime.regime_builder.RegimeSourceChangedError):
        regime.regime_recompute(_request(tmp_path), end=target)

    assert published == []


def test_regime_range_recompute_rejects_source_change_before_publish(
    tmp_path,
    monkeypatch,
):
    """带 start 的区间重算也必须复验并沿用计算前来源快照。"""
    target = date(2026, 8, 25)
    enriched = tmp_path / "kline_daily_enriched" / f"date={target}" / "part.parquet"
    index = tmp_path / "kline_index_enriched" / f"date={target}" / "part.parquet"
    enriched.parent.mkdir(parents=True)
    index.parent.mkdir(parents=True)
    enriched.write_bytes(b"stock-source")
    index.write_bytes(b"index-v1")
    published: list[object] = []

    def change_index_during_compute(*args, **kwargs):
        index.write_bytes(b"index-version-two")
        return pl.DataFrame()

    monkeypatch.setattr(
        regime.regime_builder,
        "run_regime_batch",
        change_index_during_compute,
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "replace_regime_history_range",
        lambda *args, **kwargs: published.append(args),
    )

    with pytest.raises(regime.regime_builder.RegimeSourceChangedError):
        regime.regime_recompute(
            _request(tmp_path),
            start=target,
            end=target,
        )

    assert published == []


def test_regime_range_recompute_rejects_mainline_source_change_before_publish(
    tmp_path,
    monkeypatch,
):
    """区间重算期间主线来源变化时，四个派生文件都不得发布。"""
    target = date(2026, 8, 25)
    before = _mainline_range_snapshot(target, "before")
    after = _mainline_range_snapshot(target, "after", min_members=5)
    snapshots = iter([before, after])
    published: list[object] = []
    monkeypatch.setattr(
        regime.regime_builder,
        "capture_regime_source_snapshot",
        lambda *args, **kwargs: pl.DataFrame(),
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "assert_regime_source_unchanged",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "run_regime_batch",
        lambda *args, **kwargs: pl.DataFrame(),
    )
    monkeypatch.setattr(
        market_mainline,
        "capture_mainline_source_snapshot",
        lambda *args, **kwargs: next(snapshots),
    )
    monkeypatch.setattr(
        market_mainline,
        "compute_mainline_range",
        lambda *args, **kwargs: pl.DataFrame(),
    )
    monkeypatch.setattr(
        regime,
        "replace_parquet_set",
        lambda *args, **kwargs: published.append(args),
    )

    with pytest.raises(market_mainline.MainlineSourceChangedError):
        regime.regime_recompute(
            _request(tmp_path),
            start=target,
            end=target,
        )

    assert published == []


def test_regime_range_recompute_publishes_mainline_coverage_in_one_transaction(
    tmp_path,
    monkeypatch,
):
    """区间重算必须把两类主线结果、水位和 regime 文件一次提交。"""
    target = date(2026, 8, 25)
    snapshot = _mainline_range_snapshot(target, "fixed")
    published: list[list[tuple[object, pl.DataFrame]]] = []
    monkeypatch.setattr(
        regime.regime_builder,
        "capture_regime_source_snapshot",
        lambda *args, **kwargs: pl.DataFrame(),
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "assert_regime_source_unchanged",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "run_regime_batch",
        lambda *args, **kwargs: pl.DataFrame(),
    )
    monkeypatch.setattr(
        market_mainline,
        "capture_mainline_source_snapshot",
        lambda *args, **kwargs: snapshot,
    )
    monkeypatch.setattr(
        market_mainline,
        "assert_mainline_source_unchanged",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        market_mainline,
        "compute_mainline_range",
        lambda _repo, _data_dir, _start, _end, *, kind, filter_cfg: pl.DataFrame({
            "date": [target],
            "kind": [kind],
            "rank": [1],
        }),
    )
    monkeypatch.setattr(
        regime,
        "replace_parquet_set",
        lambda entries, **kwargs: published.append(entries),
    )

    result = regime.regime_recompute(
        _request(tmp_path),
        start=target,
        end=target,
    )

    assert result["mainline_rows"] == 2
    assert len(published) == 1
    entries = dict(published[0])
    assert set(entries) == {
        regime.regime_builder.regime_path(tmp_path),
        regime.regime_builder.regime_coverage_path(tmp_path),
        market_mainline.mainline_path(tmp_path),
        market_mainline.mainline_coverage_path(tmp_path),
    }
    assert set(entries[market_mainline.mainline_path(tmp_path)]["kind"].to_list()) == {
        "concept",
        "industry",
    }
    assert set(
        entries[market_mainline.mainline_coverage_path(tmp_path)]["kind"].to_list(),
    ) == {"concept", "industry"}


def test_manual_mainline_recompute_waits_for_pipeline_regime_update(
    tmp_path,
    monkeypatch,
):
    """后台 regime 计算与手动主线重算必须共享完整计算区间锁。"""
    target = date(2026, 8, 25)
    regime_entered = threading.Event()
    release_regime = threading.Event()
    mainline_entered = threading.Event()
    errors: list[BaseException] = []
    request = _request(tmp_path)

    monkeypatch.setattr(
        regime.regime_builder,
        "enriched_date_set",
        lambda repo: {target},
    )

    def run_regime_batch(repo, start, end):
        regime_entered.set()
        assert release_regime.wait(2)
        return pl.DataFrame()

    monkeypatch.setattr(
        regime.regime_builder,
        "run_regime_batch",
        run_regime_batch,
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "replace_regime_history_range",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "refresh_phase_labels",
        lambda data_dir: 0,
    )
    monkeypatch.setattr(
        regime.regime_builder,
        "earliest_enriched_date",
        lambda repo: target,
    )
    monkeypatch.setattr(
        market_mainline,
        "compute_mainline_range",
        lambda *args, **kwargs: (
            mainline_entered.set() or pl.DataFrame()
        ),
    )
    monkeypatch.setattr(
        market_mainline,
        "replace_mainline_history_range",
        lambda *args, **kwargs: None,
    )

    def invoke(fn) -> None:
        try:
            fn()
        except BaseException as exc:  # pragma: no cover - 断言线程异常
            errors.append(exc)

    pipeline_thread = threading.Thread(
        target=lambda: invoke(
            lambda: regime.regime_builder.compute_regime_incremental(
                request.app.state.repo,
                tmp_path,
                today=target,
            )
        ),
    )
    manual_thread = threading.Thread(
        target=lambda: invoke(lambda: regime.mainline_recompute(request)),
    )

    pipeline_thread.start()
    assert regime_entered.wait(1)
    manual_thread.start()
    blocked = not mainline_entered.wait(0.2)
    release_regime.set()
    pipeline_thread.join(2)
    manual_thread.join(2)

    assert blocked
    assert mainline_entered.is_set()
    assert not pipeline_thread.is_alive()
    assert not manual_thread.is_alive()
    assert errors == []


def test_regime_phases_waits_for_market_environment_snapshot(tmp_path, monkeypatch):
    """阶段与主线组合读取不得穿过整组发布边界。"""
    target = date(2026, 8, 25)
    history = pl.DataFrame({
        "date": [target],
        "phase": ["rally"],
        "max_consecutive": [3],
        "first_board": [8],
        "ge2_count": [2],
        "promo_rate": [0.2],
        "seal_rate": [0.6],
    })
    publisher_entered = threading.Event()
    release_publisher = threading.Event()
    reader_entered = threading.Event()
    result: list[dict] = []

    def load_regime(data_dir):
        reader_entered.set()
        return history

    monkeypatch.setattr(regime.regime_builder, "load_regime_history", load_regime)
    monkeypatch.setattr(
        market_mainline,
        "load_mainline_history",
        lambda data_dir, kind: pl.DataFrame(),
    )

    def hold_publish_boundary() -> None:
        with market_environment_snapshot(tmp_path):
            publisher_entered.set()
            assert release_publisher.wait(2)

    publisher = threading.Thread(target=hold_publish_boundary)
    reader = threading.Thread(
        target=lambda: result.append(regime.regime_phases(_request(tmp_path))),
    )
    publisher.start()
    assert publisher_entered.wait(1)
    reader.start()
    blocked = not reader_entered.wait(0.2)
    release_publisher.set()
    publisher.join(2)
    reader.join(2)

    assert blocked
    assert not publisher.is_alive()
    assert not reader.is_alive()
    assert result[0]["segments"][0]["phase"] == "rally"


def test_regime_phases_limit_uses_latest_trading_days(tmp_path, monkeypatch):
    days = [date(2026, 8, 20), date(2026, 8, 21), date(2026, 8, 24)]
    history = pl.DataFrame({
        "date": days,
        "phase": ["ice", "rally", "rally"],
        "max_consecutive": [1, 3, 4],
        "first_board": [2, 8, 10],
        "ge2_count": [0, 2, 3],
        "promo_rate": [None, 0.2, 0.3],
        "seal_rate": [0.4, 0.6, 0.7],
    })
    monkeypatch.setattr(regime.regime_builder, "load_regime_history", lambda data_dir: history)
    monkeypatch.setattr(market_mainline, "load_mainline_history", lambda data_dir, kind: pl.DataFrame())

    result = regime.regime_phases(_request(tmp_path), limit=2)

    assert result["segments"] == [{
        "phase": "rally",
        "label": "主升",
        "start": "2026-08-21",
        "end": "2026-08-24",
        "days": 2,
        "avg_height": 3.5,
        "avg_first_board": 9.0,
        "avg_ge2": 2.5,
        "avg_promo": 0.25,
        "avg_seal_rate": 0.65,
        "top_mainlines": [],
    }]


def test_regime_mainline_limit_uses_latest_distinct_trading_days(tmp_path, monkeypatch):
    history = pl.DataFrame({
        "date": [
            date(2026, 8, 20), date(2026, 8, 20),
            date(2026, 8, 21), date(2026, 8, 21),
            date(2026, 8, 24), date(2026, 8, 24),
        ],
        "kind": ["concept"] * 6,
        "member": ["旧主线", "次线", "新主线", "次线", "新主线", "次线"],
        "score": [99.0, 60.0, 80.0, 50.0, 90.0, 40.0],
        "limit_up_count": [5, 3, 5, 3, 6, 3],
        "ge2_count": [2, 1, 2, 1, 3, 1],
        "max_boards": [3, 2, 3, 2, 4, 2],
        "rungs_filled": [2, 1, 2, 1, 3, 1],
        "leader_symbol": ["A.SH", "B.SH", "C.SH", "B.SH", "C.SH", "B.SH"],
        "rank": [1, 2, 1, 2, 1, 2],
    })
    monkeypatch.setattr(market_mainline, "load_mainline_history", lambda data_dir, kind: history)
    monkeypatch.setattr(
        preferences,
        "get_mainline_filter_config",
        lambda: {"min_members": 4, "max_members": 600, "blacklist": []},
    )

    result = regime.regime_mainline(_request(tmp_path), limit=2)

    assert {row["date"] for row in result["rows"]} == {"2026-08-21", "2026-08-24"}
    assert result["leaders"][0]["member"] == "新主线"
    assert all(row["member"] != "旧主线" for row in result["rows"])
