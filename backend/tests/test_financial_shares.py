from __future__ import annotations

import threading
import time
from contextlib import nullcontext
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import data as data_api
from app.indicators import pipeline
from app.services import financial_sync
from app.tickflow.capabilities import CapabilitySet


def _write_instruments(data_dir, symbols: list[str]) -> None:
    path = data_dir / "instruments" / "instruments.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": symbols}).write_parquet(path)


def test_first_share_sync_fetches_complete_history(tmp_path, monkeypatch):
    _write_instruments(tmp_path, ["600000.SH"])
    calls: list[tuple[list[str], bool]] = []

    def fake_fetch(table, symbols, capset, latest_only=True, strict=False):
        del strict
        assert table == "shares"
        calls.append((symbols, latest_only))
        return pl.DataFrame({
            "symbol": ["600000.SH", "600000.SH"],
            "period_end": ["2023-12-31", "2024-06-30"],
            "float_shares": [10.0, 12.0],
        })

    monkeypatch.setattr(financial_sync, "_fetch_table", fake_fetch)

    rows = financial_sync.sync_shares(tmp_path, CapabilitySet())

    assert rows == 2
    assert calls == [(["600000.SH"], False)]
    stored = pl.read_parquet(tmp_path / "financials" / "shares" / "part.parquet")
    assert stored["period_end"].to_list() == ["2023-12-31", "2024-06-30"]


def test_first_metrics_sync_forward_fills_snapshot_only_bps_row(
    tmp_path,
    monkeypatch,
):
    """首次同步也要补齐同报告期后续观测事件的非 bps 指标。"""
    from app.backtest.fundamentals import attach_fundamental_factors

    symbol = "600000.SH"
    report_day = date(2026, 8, 20)
    observed_day = date(2026, 9, 7)
    _write_instruments(tmp_path, [symbol])

    def fake_fetch(table, symbols, capset, latest_only=True, strict=False):
        del strict
        assert table == "metrics"
        assert latest_only is False
        return pl.DataFrame({
            "symbol": [symbol, symbol],
            "period_end": ["2026-06-30", "2026-06-30"],
            "announce_date": [report_day.isoformat(), observed_day.isoformat()],
            "roe": [16.0, None],
            "bps": [None, 5.0],
        })

    monkeypatch.setattr(financial_sync, "_fetch_table", fake_fetch)

    assert financial_sync.sync_metrics(tmp_path, CapabilitySet()) == 2
    stored = pl.read_parquet(
        tmp_path / "financials" / "metrics" / "part.parquet"
    ).sort("announce_date")
    assert stored["roe"].to_list() == [16.0, 16.0]
    assert stored["bps"].to_list() == [None, 5.0]

    panel = pl.DataFrame({
        "symbol": [symbol, symbol],
        "date": [report_day + timedelta(days=1), observed_day + timedelta(days=1)],
        "raw_close": [10.0, 10.0],
    })
    attached = attach_fundamental_factors(
        panel,
        stored,
        {"roe_latest", "pb_latest"},
    )
    assert attached["roe_latest"].to_list() == [16.0, 16.0]
    assert attached["pb_latest"].to_list() == [None, pytest.approx(2.0)]


def test_financial_table_publish_keeps_old_snapshot_visible_until_commit(
    tmp_path,
    monkeypatch,
):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "financials" / "metrics" / "part.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["old"], "period_end": ["2025-12-31"]}).write_parquet(path)
    staged = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    original_write = pl.DataFrame.write_parquet

    def blocking_write(frame, target, *args, **kwargs):
        result = original_write(frame, target, *args, **kwargs)
        if str(target).endswith(".tmp"):
            staged.set()
            assert release.wait(timeout=2)
        return result

    monkeypatch.setattr(pl.DataFrame, "write_parquet", blocking_write)

    def publish() -> None:
        try:
            financial_sync._write_table(
                "metrics",
                pl.DataFrame({
                    "symbol": ["new"],
                    "period_end": ["2026-06-30"],
                }),
                tmp_path,
            )
        except BaseException as exc:  # pragma: no cover - 线程异常转交主线程
            errors.append(exc)

    worker = threading.Thread(target=publish)
    worker.start()
    assert staged.wait(timeout=2)
    with ThreadPoolExecutor(max_workers=1) as executor:
        read = executor.submit(financial_sync.get_financial_df, tmp_path, "metrics")
        try:
            assert read.result(timeout=0.5)["symbol"].to_list() == ["old"]
        finally:
            release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors == []
    assert pl.read_parquet(path)["symbol"].to_list() == ["new"]


def test_incremental_share_sync_updates_existing_and_backfills_new_symbols(tmp_path, monkeypatch):
    _write_instruments(tmp_path, ["600000.SH", "000001.SZ"])
    path = tmp_path / "financials" / "shares" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600000.SH"],
        "period_end": ["2024-06-30"],
        "float_shares": [10.0],
    }).write_parquet(path)
    calls: list[tuple[list[str], bool]] = []

    def fake_fetch(table, symbols, capset, latest_only=True, strict=False):
        del strict
        assert table == "shares"
        calls.append((symbols, latest_only))
        return pl.DataFrame({
            "symbol": ["600000.SH", "000001.SZ", "000001.SZ"],
            "period_end": ["2024-06-30", "2023-12-31", "2024-06-30"],
            "float_shares": [11.0, 20.0, 21.0],
        })

    monkeypatch.setattr(financial_sync, "_fetch_table", fake_fetch)

    rows = financial_sync.sync_shares(tmp_path, CapabilitySet())

    assert rows == 3
    assert calls == [(["600000.SH", "000001.SZ"], False)]
    stored = pl.read_parquet(path).sort(["symbol", "period_end"])
    assert stored.filter(pl.col("symbol") == "600000.SH")["float_shares"].to_list() == [11.0]
    assert stored.filter(pl.col("symbol") == "000001.SZ")["float_shares"].to_list() == [20.0, 21.0]


def test_incremental_financial_sync_discovers_older_period_revision(
    tmp_path,
    monkeypatch,
):
    """较新报告期公布后，后续同步仍要发现旧报告期修订公告。"""
    _write_instruments(tmp_path, ["600000.SH"])
    path = tmp_path / "financials" / "metrics" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH"],
        "period_end": ["2025-09-30", "2025-12-31"],
        "announce_date": ["2025-10-20", "2026-01-20"],
        "roe": [8.0, 9.0],
    }).write_parquet(path)
    calls: list[tuple[list[str], bool]] = []

    def fake_fetch(table, symbols, capset, latest_only=True, strict=False):
        del strict
        calls.append((symbols, latest_only))
        if latest_only:
            return pl.DataFrame()
        return pl.DataFrame({
            "symbol": ["600000.SH"],
            "period_end": ["2025-09-30"],
            "announce_date": ["2026-02-01"],
            "roe": [8.5],
        })

    monkeypatch.setattr(financial_sync, "_fetch_table", fake_fetch)

    rows = financial_sync.sync_metrics(tmp_path, CapabilitySet())

    assert calls == [(["600000.SH"], False)]
    assert rows == 3
    stored = pl.read_parquet(path).sort(["period_end", "announce_date"])
    revised = stored.filter(pl.col("period_end") == "2025-09-30")
    assert revised["roe"].to_list() == [8.0, 8.5]


def test_custom_financial_provider_receives_shares_contract(monkeypatch):
    received: list[tuple[str, list[str], bool]] = []

    class Provider:
        def get_financials(self, table, symbols, latest_only=True):
            received.append((table, symbols, latest_only))
            return pl.DataFrame({
                "symbol": symbols,
                "period_end": ["2024-06-30"],
                "float_shares": [10.0],
            })

    from app.data_providers import custom as custom_sources
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "custom-test")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, ds: True)
    monkeypatch.setattr(
        custom_sources,
        "lease_provider",
        lambda _name: nullcontext((Provider(), 1)),
    )

    with financial_sync._financial_provider_lease():
        result = financial_sync._fetch_table(
            "shares",
            ["600000.SH"],
            CapabilitySet(),
            latest_only=False,
        )

    assert result.height == 1
    assert received == [("shares", ["600000.SH"], False)]


def test_invalid_explicit_financial_provider_never_calls_tickflow(monkeypatch):
    from unittest.mock import MagicMock

    from app.data_providers import custom as custom_sources
    from app.services import preferences
    from app.tickflow.capabilities import Cap

    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "broken")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, ds: False)
    get_client = MagicMock()
    monkeypatch.setattr("app.tickflow.client.get_client", get_client)
    capset = CapabilitySet()
    capset.grant(Cap.FINANCIAL)

    result = financial_sync._fetch_table("metrics", ["600000.SH"], capset)

    assert result.is_empty()
    get_client.assert_not_called()


def _financial_frame(version: str) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600000.SH"],
        "period_end": ["2026-06-30"],
        "version": [version],
    })


def test_sync_all_fetch_failure_keeps_complete_previous_snapshot(tmp_path, monkeypatch):
    _write_instruments(tmp_path, ["600000.SH"])
    for table in financial_sync.FINANCIAL_TABLES:
        path = tmp_path / "financials" / table / "part.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        _financial_frame("old").write_parquet(path)
    monkeypatch.setattr(
        financial_sync,
        "_resolve_financial_provider",
        lambda _name=None: ("custom", object()),
    )

    def fetch(table, _symbols, _capset, latest_only=True, strict=False):
        assert latest_only is False and strict is True
        if table == "balance_sheet":
            raise RuntimeError("upstream failed")
        return _financial_frame("new")

    monkeypatch.setattr(financial_sync, "_fetch_table", fetch)

    with pytest.raises(RuntimeError, match="upstream failed"):
        financial_sync.sync_all(tmp_path, CapabilitySet())

    for table in financial_sync.FINANCIAL_TABLES:
        stored = pl.read_parquet(
            tmp_path / "financials" / table / "part.parquet"
        )
        assert stored["version"].item() == "old"


def test_sync_all_empty_table_keeps_complete_previous_snapshot(tmp_path, monkeypatch):
    _write_instruments(tmp_path, ["600000.SH"])
    before: dict[str, bytes] = {}
    for table in financial_sync.FINANCIAL_TABLES:
        path = tmp_path / "financials" / table / "part.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        _financial_frame("old").write_parquet(path)
        before[table] = path.read_bytes()
    monkeypatch.setattr(
        financial_sync,
        "_resolve_financial_provider",
        lambda _name=None: ("custom", object()),
    )

    def fetch(table, _symbols, _capset, latest_only=True, strict=False):
        assert latest_only is False and strict is True
        return pl.DataFrame() if table == "shares" else _financial_frame("new")

    monkeypatch.setattr(financial_sync, "_fetch_table", fetch)

    with pytest.raises(RuntimeError, match="sync_shares provider returned no usable rows"):
        financial_sync.sync_all(tmp_path, CapabilitySet())

    for table in financial_sync.FINANCIAL_TABLES:
        path = tmp_path / "financials" / table / "part.parquet"
        assert path.read_bytes() == before[table]


def test_fuyao_sync_all_preserves_unsupported_shares(tmp_path, monkeypatch):
    from app.plugins.fuyao.provider import FuyaoProvider

    _write_instruments(tmp_path, ["600000.SH"])
    shares_path = tmp_path / "financials" / "shares" / "part.parquet"
    shares_path.parent.mkdir(parents=True, exist_ok=True)
    _financial_frame("existing-shares").write_parquet(shares_path)
    before = shares_path.read_bytes()
    provider = FuyaoProvider()
    monkeypatch.setattr(
        financial_sync,
        "_resolve_financial_provider",
        lambda _name=None: ("custom", provider),
    )

    def fetch(table, _symbols, _capset, latest_only=True, strict=False):
        assert table != "shares"
        assert latest_only is False and strict is True
        return _financial_frame("fuyao-new")

    monkeypatch.setattr(financial_sync, "_fetch_table", fetch)

    result = financial_sync.sync_all(tmp_path, CapabilitySet())

    assert result == {
        "metrics": 1,
        "income": 1,
        "balance_sheet": 1,
        "cash_flow": 1,
    }
    assert shares_path.read_bytes() == before


@pytest.mark.parametrize("all_tables", [False, True])
def test_financial_network_fetch_does_not_block_snapshot_reader(tmp_path, monkeypatch, all_tables):
    """同步等待上游时，读者仍能立即读取完整旧快照。"""
    from concurrent.futures import ThreadPoolExecutor

    _write_instruments(tmp_path, ["600000.SH"])
    for table in financial_sync.FINANCIAL_TABLES:
        financial_sync._write_table(table, _financial_frame("old"), tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def fetch(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return _financial_frame("new")

    monkeypatch.setattr(financial_sync, "_fetch_table", fetch)
    monkeypatch.setattr("app.services.preferences.get_financial_provider", lambda: "tickflow")
    monkeypatch.setattr(financial_sync, "_financial_provider_lease", lambda: nullcontext(("tickflow", None, None)))
    monkeypatch.setattr(financial_sync, "_validate_financial_provider_lease", lambda *_args: None)
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(
            financial_sync.sync_all if all_tables else lambda path, caps: financial_sync._sync_table(
                "metrics", ["600000.SH"], path, caps,
            ),
            tmp_path, CapabilitySet(),
        )
        try:
            assert entered.wait(5)
            reader = executor.submit(financial_sync.get_financial_df, tmp_path, "metrics")
            assert reader.result(timeout=1)["version"].item() == "old"
        finally:
            release.set()
        writer.result(timeout=5)
    assert financial_sync.get_financial_df(tmp_path, "metrics")["version"].item() == "new"


def test_financial_api_read_recovers_interrupted_snapshot(tmp_path) -> None:
    from app.api import financials as financials_api
    from app.services import atomic_parquet
    from app.tickflow.capabilities import Cap

    target = tmp_path / "financials" / "metrics" / "part.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.with_name(".part.parquet.interrupted.bak")
    _financial_frame("old").write_parquet(backup)
    _financial_frame("partial-new").write_parquet(target)
    journal = tmp_path / "financials" / ".financial_publish.json"
    atomic_parquet._write_recovery_journal(journal, {target: backup})
    capset = CapabilitySet()
    capset.grant(Cap.FINANCIAL)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                capabilities=capset,
                repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
            )
        )
    )

    response = financials_api.get_metrics(request)

    assert [row["version"] for row in response["data"]] == ["old"]
    assert not journal.exists()
    assert not backup.exists()


def test_financial_report_reader_never_observes_partial_set_publish(tmp_path, monkeypatch):
    from app.services import financial_analyzer

    _write_instruments(tmp_path, ["600000.SH"])
    for table in financial_sync.FINANCIAL_TABLES:
        path = tmp_path / "financials" / table / "part.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        _financial_frame("old").write_parquet(path)
    monkeypatch.setattr(
        financial_sync,
        "_resolve_financial_provider",
        lambda _name=None: ("custom", object()),
    )
    monkeypatch.setattr(
        financial_sync,
        "_fetch_table",
        lambda _table, _symbols, _capset, latest_only=True, strict=False: (
            _financial_frame("new")
        ),
    )
    first_published = threading.Event()
    release_publish = threading.Event()

    def slow_publish(entries, *, journal_path=None, commit_guard=None):
        del journal_path
        entries = list(entries)
        with commit_guard() if commit_guard is not None else nullcontext():
            for index, (path, frame) in enumerate(entries):
                path.parent.mkdir(parents=True, exist_ok=True)
                frame.write_parquet(path)
                if index == 0:
                    first_published.set()
                    assert release_publish.wait(timeout=2)

    monkeypatch.setattr(financial_sync, "replace_parquet_set", slow_publish)
    writer = threading.Thread(
        target=financial_sync.sync_all,
        args=(tmp_path, CapabilitySet()),
    )
    writer.start()
    assert first_published.wait(timeout=2)

    report: list[dict[str, list[dict]]] = []
    reader = threading.Thread(
        target=lambda: report.append(
            financial_analyzer._load_stock_financials(tmp_path, "600000.SH")
        )
    )
    reader.start()
    time.sleep(0.05)
    assert reader.is_alive()
    release_publish.set()
    writer.join(timeout=2)
    reader.join(timeout=2)

    assert not writer.is_alive() and not reader.is_alive()
    assert report
    assert {
        report[0][table][0]["version"]
        for table in financial_sync.FINANCIAL_TABLES
    } == {"new"}


def test_single_table_writer_waits_for_complete_snapshot_reader(tmp_path, monkeypatch):
    for table in financial_sync.FINANCIAL_TABLES:
        path = tmp_path / "financials" / table / "part.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        _financial_frame("old").write_parquet(path)

    from app.services import financial_analyzer

    original_get = financial_sync.get_financial_df
    first_read = threading.Event()
    release_reader = threading.Event()
    read_count = 0

    def slow_get(data_dir, table):
        nonlocal read_count
        frame = original_get(data_dir, table)
        read_count += 1
        if read_count == 1:
            first_read.set()
            assert release_reader.wait(timeout=2)
        return frame

    monkeypatch.setattr(financial_sync, "get_financial_df", slow_get)
    report: list[dict[str, list[dict]]] = []
    reader = threading.Thread(
        target=lambda: report.append(
            financial_analyzer._load_stock_financials(tmp_path, "600000.SH")
        )
    )
    reader.start()
    assert first_read.wait(timeout=2)

    writer = threading.Thread(
        target=financial_sync._write_table,
        args=("income", _financial_frame("new"), tmp_path),
    )
    writer.start()
    time.sleep(0.05)
    assert writer.is_alive()
    release_reader.set()
    reader.join(timeout=2)
    writer.join(timeout=2)

    assert not reader.is_alive() and not writer.is_alive()
    assert {
        report[0][table][0]["version"]
        for table in financial_sync.FINANCIAL_TABLES
    } == {"old"}
    assert pl.read_parquet(
        tmp_path / "financials" / "income" / "part.parquet"
    )["version"].item() == "new"


def test_historical_turnover_uses_only_available_share_capital(monkeypatch):
    monkeypatch.setattr(pipeline, "cn_today", lambda: date(2026, 7, 18))
    bars = pl.DataFrame({
        "symbol": ["600000.SH"] * 6,
        "date": [
            date(2024, 3, 31),
            date(2024, 4, 14),
            date(2024, 4, 15),
            date(2024, 4, 16),
            date(2024, 6, 30),
            date(2026, 7, 18),
        ],
        "volume": [10_000.0] * 6,
    })
    instruments = pl.DataFrame({
        "symbol": ["600000.SH"],
        "float_shares": [200_000_000.0],
    })
    shares = pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH"],
        "period_end": ["2023-12-31", "2024-06-30"],
        "announce_date": ["2024-04-15", None],
        "float_shares": [100_000_000.0, 50_000_000.0],
    })

    result = pipeline.compute_limit_signals(
        bars,
        instruments,
        needed={"turnover_rate"},
        historical_shares=shares,
    )

    assert result["turnover_rate"].to_list()[:3] == [None, None, None]
    assert result["turnover_rate"].to_list()[3:] == pytest.approx([1.0, 1.0, 0.5])


def test_turnover_without_share_history_fails_closed_for_history(monkeypatch):
    monkeypatch.setattr(pipeline, "cn_today", lambda: date(2026, 7, 18))
    bars = pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH"],
        "date": [date(2024, 4, 15), date(2026, 7, 18)],
        "volume": [10_000.0, 10_000.0],
    })
    instruments = pl.DataFrame({
        "symbol": ["600000.SH"],
        "float_shares": [200_000_000.0],
    })

    result = pipeline.compute_limit_signals(
        bars,
        instruments,
        needed={"turnover_rate"},
    )

    assert result["turnover_rate"].to_list() == pytest.approx([None, 0.5])


@pytest.mark.parametrize(
    "shares",
    [
        pl.DataFrame({"symbol": ["600000.SH"], "float_shares": [100_000_000.0]}),
        pl.DataFrame({
            "symbol": ["600000.SH"],
            "period_end": ["2024-03-31"],
            "announce_date": [None],
            "float_shares": [100_000_000.0],
        }),
    ],
)
def test_malformed_or_unannounced_share_history_does_not_leak_current_shares(
    monkeypatch,
    shares,
):
    monkeypatch.setattr(pipeline, "cn_today", lambda: date(2026, 7, 18))
    bars = pl.DataFrame({
        "symbol": ["600000.SH"],
        "date": [date(2024, 4, 15)],
        "volume": [10_000.0],
    })
    instruments = pl.DataFrame({
        "symbol": ["600000.SH"],
        "float_shares": [200_000_000.0],
    })

    result = pipeline.compute_limit_signals(
        bars,
        instruments,
        needed={"turnover_rate"},
        historical_shares=shares,
    )

    assert result["turnover_rate"][0] is None


def test_data_status_includes_share_history(tmp_path):
    path = tmp_path / "financials" / "shares" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH", "000001.SZ"],
        "period_end": ["2023-12-31", "2024-06-30", "2024-06-30"],
    }).write_parquet(path)

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    result = data_api._safe_aggregate_financials(repo)

    assert result is not None
    assert result["rows"] == 3
    assert result["tables"]["shares"] == {"rows": 3, "symbols": 2}
