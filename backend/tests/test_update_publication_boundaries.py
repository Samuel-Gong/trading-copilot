"""更新资格、无锁暂存与来源复验的并发回归测试。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.enriched_generation import EnrichedGenerationUnavailableError, bump_enriched_generation
from app.services import preferences
from app.services.environment_sources import (
    EnvironmentSourceChangedError,
    ext_source_update,
)
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField
from app.services.market_environment_lock import (
    market_environment_journal_path,
    market_environment_snapshot,
    replace_market_parquet_set,
    serialized_market_environment_update,
)
from app.services.update_slots import UpdateBusyError, update_slot


def test_update_slot_is_reentrant_nonblocking_and_directory_scoped(tmp_path):
    def acquire(path):
        with update_slot("财务", path):
            return True

    with ThreadPoolExecutor(max_workers=1) as pool:
        with update_slot("财务", tmp_path), update_slot("财务", tmp_path):
            with pytest.raises(UpdateBusyError):
                pool.submit(acquire, tmp_path).result(timeout=0.5)
            assert pool.submit(acquire, tmp_path / "other").result(timeout=0.5)
        assert pool.submit(acquire, tmp_path).result(timeout=0.5)
        with pytest.raises(ValueError), update_slot("财务", tmp_path):
            raise ValueError("取消更新")
        assert pool.submit(acquire, tmp_path).result(timeout=0.5)


@pytest.mark.parametrize("source", ["stock", "index", "ext", "filters"])
def test_market_staging_allows_readers_and_rejects_changed_source(tmp_path, monkeypatch, source):
    target = tmp_path / "regime_history" / "part.parquet"
    target.parent.mkdir()
    pl.DataFrame({"value": [1]}).write_parquet(target)
    filters = {"min_members": 4}
    monkeypatch.setattr(preferences, "get_mainline_filter_config", lambda: dict(filters))
    original_write = pl.DataFrame.write_parquet

    def read_old():
        with market_environment_snapshot(tmp_path):
            return pl.read_parquet(target)["value"].to_list()

    def mutate_source():
        if source in {"stock", "index"}:
            bump_enriched_generation(tmp_path, source)
        elif source == "ext":
            ExtConfigStore(tmp_path).create(ExtConfig(
                id="new_source", label="新概念", mode="snapshot",
                fields=[ExtField("symbol")],
            ))
        else:
            with preferences.provider_route_lock():
                filters["min_members"] = 5

    def stage(frame, path, *args, **kwargs):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(read_old).result(timeout=0.5) == [1]
            pool.submit(mutate_source).result(timeout=0.5)
        return original_write(frame, path, *args, **kwargs)

    monkeypatch.setattr(pl.DataFrame, "write_parquet", stage)

    @serialized_market_environment_update
    def publish(data_dir: Path):
        replace_market_parquet_set(
            [(target, pl.DataFrame({"value": [2]}))],
            journal_path=market_environment_journal_path(data_dir),
        )

    with pytest.raises(EnvironmentSourceChangedError):
        publish(tmp_path)
    assert read_old() == [1]
    assert not market_environment_journal_path(tmp_path).exists()
    assert list(target.parent.iterdir()) == [target]


def test_market_update_rejects_inflight_ext_mutation(tmp_path):
    @serialized_market_environment_update
    def publish(data_dir: Path):
        pytest.fail("来源尚未稳定时不应开始计算")

    with ext_source_update(tmp_path), pytest.raises(EnvironmentSourceChangedError):
        publish(tmp_path)


def test_clear_rejects_active_financial_writer_before_deleting(tmp_path, monkeypatch):
    from app.api import data

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
    )))
    monkeypatch.setattr(data, "_clear_data_impl", lambda _: pytest.fail("不能开始清库"))
    with (
        ThreadPoolExecutor(max_workers=1) as pool,
        update_slot("财务", tmp_path),
        pytest.raises(UpdateBusyError),
    ):
        pool.submit(data.clear_data, request).result(timeout=0.5)


def test_financial_staging_allows_route_change_and_rejects_stale_publish(tmp_path, monkeypatch):
    from app.services import financial_sync

    target = tmp_path / "financials" / "metrics" / "part.parquet"
    target.parent.mkdir(parents=True)
    pl.DataFrame({"value": [1]}).write_parquet(target)
    route = ["tickflow"]
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: route[0])
    original_write = pl.DataFrame.write_parquet

    def change_route():
        with preferences.provider_route_lock():
            route[0] = "replacement"
        return financial_sync.get_financial_df(tmp_path, "metrics")["value"].to_list()

    def stage(frame, path, *args, **kwargs):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(change_route).result(timeout=0.5) == [1]
        return original_write(frame, path, *args, **kwargs)

    monkeypatch.setattr(pl.DataFrame, "write_parquet", stage)
    with pytest.raises(RuntimeError, match="provider changed"):
        financial_sync._write_table(
            "metrics", pl.DataFrame({"symbol": ["600000.SH"], "value": [2]}), tmp_path,
            lease=("tickflow", "tickflow", None),
        )
    assert pl.read_parquet(target)["value"].to_list() == [1]


@pytest.mark.parametrize("error", [
    UpdateBusyError, EnvironmentSourceChangedError, EnrichedGenerationUnavailableError,
])
def test_update_conflicts_are_http_409(error):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.main import app

    isolated_app = FastAPI(exception_handlers={error: app.exception_handlers[error]})

    @isolated_app.get("/update")
    def update():
        raise error("请稍后重试")

    with TestClient(isolated_app) as client:
        response = client.get("/update")
    assert response.status_code == 409
    assert response.json() == {"detail": "请稍后重试"}
