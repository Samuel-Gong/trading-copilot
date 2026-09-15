"""自定义 Provider 与 TickFlow 的日 K / 复权 / universe 边界。"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import date
from threading import Lock
from types import SimpleNamespace

import polars as pl
import pytest

from app.data_providers import custom as custom_sources
from app.jobs import daily_pipeline
from app.services import extend_history, index_sync, instrument_sync, kline_sync, preferences
from app.tickflow.capabilities import Cap, CapabilitySet


class _CustomDailyProvider:
    def __init__(self) -> None:
        self.asset_types: list[str] = []

    def get_daily(
        self,
        symbols,
        start_time,
        end_time,
        asset_type="stock",
        on_chunk_done=None,
    ):
        del start_time, end_time, on_chunk_done
        self.asset_types.append(asset_type)
        return pl.DataFrame({
            "symbol": symbols,
            "date": [date(2026, 8, 27)] * len(symbols),
            "close": [10.0] * len(symbols),
        })

    def get_adj_factors(self, symbols, start_time, end_time, asset_type="stock"):
        del start_time, end_time
        self.asset_types.append(f"adj:{asset_type}")
        return pl.DataFrame({
            "symbol": symbols,
            "trade_date": [date(2026, 8, 27)] * len(symbols),
            "ex_factor": [1.0] * len(symbols),
        })


def test_custom_daily_route_handles_stock_index_and_etf_without_tickflow(monkeypatch):
    provider = _CustomDailyProvider()
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "custom_only")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, ds: ds == "daily")
    monkeypatch.setattr(
        custom_sources,
        "lease_provider",
        lambda name: nullcontext((provider, 1)),
    )
    monkeypatch.setattr(
        kline_sync,
        "sync_daily_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("TickFlow 不应被调用")),
    )

    for symbol, asset_type in (
        ("600519.SH", "stock"),
        ("000001.SH", "index"),
        ("510300.SH", "etf"),
    ):
        result = kline_sync.fetch_daily_routed(
            [symbol], CapabilitySet(), asset_type=asset_type,
        )
        assert result["symbol"].to_list() == [symbol]

    assert provider.asset_types == ["stock", "index", "etf"]


def test_invalid_selected_custom_daily_and_adj_never_fall_back_to_tickflow(monkeypatch):
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "broken_custom")
    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "broken_custom")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda _name, _ds: False)
    monkeypatch.setattr(
        kline_sync,
        "sync_daily_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("TickFlow 不应被调用")),
    )
    monkeypatch.setattr(
        kline_sync,
        "fetch_adj_factor_single",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("TickFlow 不应被调用")),
    )

    assert kline_sync.fetch_daily_routed(["600519.SH"], CapabilitySet()).is_empty()
    assert kline_sync.fetch_adj_factor_single_routed(
        "600519.SH", CapabilitySet(),
    ).is_empty()


def test_empty_tickflow_daily_batch_is_reported_as_failure(monkeypatch):
    client = SimpleNamespace(
        klines=SimpleNamespace(batch=lambda *_args, **_kwargs: [])
    )
    monkeypatch.setattr(kline_sync, "get_client", lambda: client)
    failures: list[str] = []

    result = kline_sync.sync_daily_batch(
        ["600519.SH"], batch_size=100, rpm=0, failed_out=failures
    )

    assert result.is_empty()
    assert failures == ["600519.SH"]


def test_daily_persist_discards_partial_batch_results(monkeypatch):
    partial = pl.DataFrame({
        "symbol": ["600519.SH"],
        "date": [date(2026, 8, 27)],
        "close": [10.0],
    })

    def _partial_fetch(symbols, capset, **kwargs):
        del symbols, capset
        kwargs["failed_out"].append("000001.SZ")
        return partial

    monkeypatch.setattr(kline_sync, "fetch_daily_routed", _partial_fetch)
    appended: list[pl.DataFrame] = []
    repo = SimpleNamespace(append_daily=appended.append)
    failures: list[str] = []

    written = kline_sync.sync_and_persist_daily_batch(
        ["600519.SH", "000001.SZ"],
        repo,
        CapabilitySet(),
        failed_out=failures,
    )

    assert written == 0
    assert failures == ["000001.SZ"]
    assert appended == []


def test_custom_daily_capability_does_not_authorize_tickflow_universe(monkeypatch):
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "custom_only")
    monkeypatch.setattr(
        daily_pipeline,
        "get_pool",
        lambda name, **_kwargs: (_ for _ in ()).throw(AssertionError(name))
        if name == "CN_Equity_A" else [],
    )
    monkeypatch.setattr(
        "app.tickflow.pools.get_pool",
        lambda name, **_kwargs: (_ for _ in ()).throw(AssertionError(name))
        if name == "CN_Equity_A" else [],
    )
    capset = CapabilitySet()
    capset.grant(Cap.KLINE_DAILY_BATCH)

    assert isinstance(daily_pipeline._resolve_universe(capset), list)
    assert isinstance(extend_history._resolve_universe(capset), list)


class _IndexRepo:
    def __init__(self) -> None:
        self.index_rows = 0
        self.etf_rows = 0

    def append_index_daily(self, frame: pl.DataFrame) -> None:
        self.index_rows += frame.height

    def append_index_enriched(self, _frame: pl.DataFrame) -> None:
        return None

    def append_etf_daily(self, frame: pl.DataFrame) -> None:
        self.etf_rows += frame.height

    def append_etf_enriched(self, _frame: pl.DataFrame) -> None:
        return None

    def refresh_index_views(self) -> None:
        return None


def test_custom_daily_syncs_index_and_etf_without_tickflow_capability(monkeypatch, tmp_path):
    provider = _CustomDailyProvider()
    repo = _IndexRepo()
    repo.store = type("Store", (), {"data_dir": tmp_path})()
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "custom_only")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda _name, ds: ds == "daily")
    monkeypatch.setattr(
        custom_sources,
        "lease_provider",
        lambda _name: nullcontext((provider, 1)),
    )
    monkeypatch.setattr(custom_sources, "registry_generation", lambda: 1)
    monkeypatch.setattr(index_sync, "compute_enriched", lambda raw, **_kwargs: raw)
    monkeypatch.setattr(index_sync, "invalidate_benchmark_momentum_cache", lambda _path: None)
    monkeypatch.setattr(index_sync, "_load_etf_factors", lambda _repo: pl.DataFrame())

    assert index_sync.sync_and_persist_index_daily(
        repo, CapabilitySet(), symbols_override=["000001.SH"],
    ) == 1
    assert index_sync.sync_and_persist_etf_daily(
        repo, CapabilitySet(), symbols_override=["510300.SH"],
    ) == 1
    assert repo.index_rows == 1
    assert repo.etf_rows == 1
    assert provider.asset_types == ["index", "etf"]


def test_invalid_custom_daily_does_not_sync_index_or_etf(monkeypatch):
    repo = _IndexRepo()
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "broken_custom")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda _name, _ds: False)

    assert index_sync.sync_and_persist_index_daily(
        repo, CapabilitySet(), symbols_override=["000001.SH"],
    ) == 0
    assert index_sync.sync_and_persist_etf_daily(
        repo, CapabilitySet(), symbols_override=["510300.SH"],
    ) == 0


def test_custom_index_daily_partial_failure_publishes_nothing(monkeypatch, tmp_path):
    repo = _IndexRepo()
    repo.store = SimpleNamespace(data_dir=tmp_path)
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "custom_only")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda *_args: True)
    monkeypatch.setattr(
        custom_sources,
        "lease_provider",
        lambda _name: nullcontext((object(), 1)),
    )
    monkeypatch.setattr(custom_sources, "registry_generation", lambda: 1)
    def _partial(symbols, _capset, *, failed_out=None, **_kwargs):
        failed_out.append(symbols[-1])
        return pl.DataFrame({
            "symbol": [symbols[0]],
            "date": [date(2026, 8, 27)],
            "close": [10.0],
        })

    monkeypatch.setattr(kline_sync, "fetch_daily_routed", _partial)

    with pytest.raises(RuntimeError, match="指数日K同步不完整"):
        index_sync.sync_and_persist_index_daily(
            repo,
            CapabilitySet(),
            symbols_override=["000001.SH", "000002.SH"],
        )

    assert repo.index_rows == 0


def test_stock_daily_route_change_discards_inflight_result(monkeypatch, tmp_path):
    selected = ["custom_a"]

    class Provider(_CustomDailyProvider):
        def get_daily(self, *args, **kwargs):
            selected[0] = "custom_b"
            return super().get_daily(*args, **kwargs)

    provider = Provider()
    appended: list[pl.DataFrame] = []
    repo = SimpleNamespace(
        append_daily=appended.append,
        store=SimpleNamespace(data_dir=tmp_path),
        db=SimpleNamespace(execute=lambda *_args: None),
    )
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: selected[0])
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda *_args: True)
    monkeypatch.setattr(
        custom_sources,
        "lease_provider",
        lambda _name: nullcontext((provider, 1)),
    )
    monkeypatch.setattr(custom_sources, "registry_generation", lambda: 1)

    with pytest.raises(RuntimeError, match="daily provider changed"):
        kline_sync.sync_and_persist_daily_batch(
            ["600519.SH"], repo, CapabilitySet(),
        )
    assert appended == []


@pytest.mark.parametrize(
    ("sync", "rows_attr", "symbol"),
    [
        (index_sync.sync_and_persist_index_daily, "index_rows", "000001.SH"),
        (index_sync.sync_and_persist_etf_daily, "etf_rows", "510300.SH"),
    ],
)
def test_index_and_etf_route_change_discards_inflight_result(
    monkeypatch, tmp_path, sync, rows_attr, symbol,
):
    selected = ["custom_a"]
    repo = _IndexRepo()
    repo.store = SimpleNamespace(data_dir=tmp_path)
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: selected[0])
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda *_args: True)
    monkeypatch.setattr(
        custom_sources,
        "lease_provider",
        lambda _name: nullcontext((object(), 1)),
    )
    monkeypatch.setattr(custom_sources, "registry_generation", lambda: 1)

    def _fetch(symbols, _capset, **_kwargs):
        selected[0] = "custom_b"
        return pl.DataFrame({
            "symbol": symbols,
            "date": [date(2026, 8, 27)] * len(symbols),
            "close": [10.0] * len(symbols),
        })

    monkeypatch.setattr(kline_sync, "fetch_daily_routed", _fetch)
    with pytest.raises(RuntimeError, match="daily provider changed"):
        sync(repo, CapabilitySet(), symbols_override=[symbol])
    assert getattr(repo, rows_attr) == 0


def test_adj_factor_route_change_discards_inflight_result(monkeypatch, tmp_path):
    selected = ["custom_a"]

    class Provider:
        def get_adj_factors(self, symbols, **_kwargs):
            selected[0] = "custom_b"
            return pl.DataFrame({
                "symbol": symbols,
                "trade_date": [date(2026, 8, 27)] * len(symbols),
                "ex_factor": [1.0] * len(symbols),
            })

    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: selected[0])
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda *_args: True)
    monkeypatch.setattr(
        custom_sources,
        "lease_provider",
        lambda _name: nullcontext((Provider(), 1)),
    )
    monkeypatch.setattr(custom_sources, "registry_generation", lambda: 1)
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        _write_lock=Lock(),
    )

    assert kline_sync.sync_adj_factor(
        ["600519.SH"], repo, CapabilitySet(),
    ) == (0, [])
    assert not (tmp_path / "adj_factor" / "all.parquet").exists()


def test_instrument_route_change_discards_inflight_result(monkeypatch, tmp_path):
    selected = ["custom_a"]

    class Provider:
        def get_instruments(self, _asset_type):
            selected[0] = "custom_b"
            return [{"symbol": "600519.SH", "name": "贵州茅台"}]

    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: selected[0])
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda *_args: True)
    monkeypatch.setattr(
        custom_sources,
        "lease_provider",
        lambda _name: nullcontext((Provider(), 1)),
    )
    monkeypatch.setattr(custom_sources, "registry_generation", lambda: 1)

    assert instrument_sync.sync_instruments(tmp_path) == 0
    assert not (tmp_path / "instruments" / "instruments.parquet").exists()
