"""会推进 enriched generation 的长任务必须同步恢复 Repository 快照。"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from app.jobs import daily_pipeline
from app.jobs.daily_pipeline import PipelineStageError
from app.services import enriched_job
from app.services.enriched_job import (
    EnrichedRepositoryRefreshError,
    run_enriched_job_with_repository_refresh,
)
from app.tickflow.repository import DataStore, KlineRepository


class _Repo:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.generation = "before"

    def get_matrix_data_generation(self, asset_type: str) -> str:
        assert asset_type == "stock"
        return self.generation

    def refresh_cache(self) -> str:
        self.events.append("refresh")
        return self.generation


class _QuoteService:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.is_paused = False

    def pause(self) -> None:
        self.events.append("pause")
        self.is_paused = True

    def resume(self) -> None:
        self.events.append("resume")
        self.is_paused = False


def test_enriched_job_refreshes_repository_before_realtime_resumes() -> None:
    events: list[str] = []
    repo = _Repo(events)
    quotes = _QuoteService(events)

    result = run_enriched_job_with_repository_refresh(
        repo,
        lambda: events.append("publish") or {"rows": 1},
        quotes,
    )

    assert result == {"rows": 1}
    assert events == ["pause", "publish", "refresh", "resume"]


def test_failed_enriched_job_without_publication_does_not_replace_snapshot() -> None:
    events: list[str] = []
    repo = _Repo(events)

    result = run_enriched_job_with_repository_refresh(
        repo,
        lambda: events.append("failed") or {"error": "injected"},
    )

    assert result == {"error": "injected"}
    assert events == ["failed"]


def test_error_result_refreshes_snapshot_after_partial_publication() -> None:
    events: list[str] = []
    repo = _Repo(events)

    def operation() -> dict:
        events.append("publish")
        repo.generation = "after"
        return {"error": "later stage failed"}

    result = run_enriched_job_with_repository_refresh(repo, operation)

    assert result == {"error": "later stage failed"}
    assert events == ["publish", "refresh"]


def test_pipeline_stage_error_refreshes_partial_publication_before_resume() -> None:
    events: list[str] = []
    repo = _Repo(events)
    quotes = _QuoteService(events)

    def operation() -> dict:
        events.append("publish")
        repo.generation = "after"
        raise PipelineStageError(["compute_regime: injected"])

    with pytest.raises(PipelineStageError, match="compute_regime: injected"):
        run_enriched_job_with_repository_refresh(repo, operation, quotes)

    assert events == ["pause", "publish", "refresh", "resume"]


def test_scheduled_pipeline_refreshes_before_realtime_resumes(monkeypatch) -> None:
    events: list[str] = []
    repo = _Repo(events)
    quotes = _QuoteService(events)
    live_capset = object()
    monkeypatch.setattr(
        daily_pipeline,
        "_get_app_state",
        lambda: SimpleNamespace(
            capabilities=live_capset,
            quote_service=quotes,
        ),
    )

    def run_now(repo_arg, capset_arg, on_progress=None) -> dict:
        assert repo_arg is repo
        assert capset_arg is live_capset
        assert on_progress is None
        events.append("publish")
        repo.generation = "after"
        raise PipelineStageError(["compute_mainline: injected"])

    monkeypatch.setattr(daily_pipeline, "run_now", run_now)

    with pytest.raises(PipelineStageError, match="compute_mainline: injected"):
        daily_pipeline._run_scheduled_pipeline_with_refresh(repo, object())

    assert events == ["pause", "publish", "refresh", "resume"]


def test_unknown_generation_refreshes_before_original_error_and_resume(
    monkeypatch,
) -> None:
    events: list[str] = []
    repo = _Repo(events)
    quotes = _QuoteService(events)
    reads = 0

    def generation(asset_type: str) -> str:
        nonlocal reads
        assert asset_type == "stock"
        reads += 1
        if reads == 2:
            raise OSError("temporary generation read failure")
        return repo.generation

    monkeypatch.setattr(repo, "get_matrix_data_generation", generation)

    def operation() -> dict:
        events.append("publish")
        repo.generation = "after"
        raise PipelineStageError(["compute_regime: injected"])

    with pytest.raises(PipelineStageError, match="compute_regime: injected"):
        run_enriched_job_with_repository_refresh(repo, operation, quotes)

    assert events == ["pause", "publish", "refresh", "resume"]
    assert quotes.is_paused is False


def test_refresh_exhaustion_keeps_realtime_paused(monkeypatch) -> None:
    events: list[str] = []
    repo = _Repo(events)
    quotes = _QuoteService(events)
    monkeypatch.setattr(enriched_job, "_REPOSITORY_REFRESH_WAIT_SECONDS", 0.0)

    def refresh_cache() -> str:
        events.append("refresh")
        raise OSError("injected refresh failure")

    monkeypatch.setattr(repo, "refresh_cache", refresh_cache)

    def operation() -> dict:
        events.append("publish")
        repo.generation = "after"
        raise PipelineStageError(["compute_mainline: injected"])

    with pytest.raises(EnrichedRepositoryRefreshError) as captured:
        run_enriched_job_with_repository_refresh(repo, operation, quotes)

    assert isinstance(captured.value.operation_error, PipelineStageError)
    assert events == ["pause", "publish", "refresh"]
    assert quotes.is_paused is True


def test_transient_refresh_failure_retries_before_realtime_resumes(
    monkeypatch,
) -> None:
    events: list[str] = []
    repo = _Repo(events)
    quotes = _QuoteService(events)
    attempts = 0
    monkeypatch.setattr(enriched_job, "_REPOSITORY_REFRESH_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(enriched_job, "_REPOSITORY_REFRESH_POLL_SECONDS", 0.0)

    def refresh_cache() -> str:
        nonlocal attempts
        attempts += 1
        events.append("refresh")
        if attempts == 1:
            raise OSError("temporary refresh failure")
        return repo.generation

    monkeypatch.setattr(repo, "refresh_cache", refresh_cache)

    def operation() -> dict:
        events.append("publish")
        repo.generation = "after"
        return {"rows": 1}

    result = run_enriched_job_with_repository_refresh(repo, operation, quotes)

    assert result == {"rows": 1}
    assert events == ["pause", "publish", "refresh", "refresh", "resume"]
    assert quotes.is_paused is False


def test_swallowed_repository_refresh_failure_keeps_realtime_paused(
    tmp_path,
    monkeypatch,
) -> None:
    events: list[str] = []
    repo = KlineRepository(DataStore(tmp_path))
    quotes = _QuoteService(events)
    monkeypatch.setattr(enriched_job, "_REPOSITORY_REFRESH_WAIT_SECONDS", 0.0)

    def fail_latest_date():
        raise OSError("injected swallowed refresh failure")

    monkeypatch.setattr(repo, "_latest_enriched_date_duckdb", fail_latest_date)

    with pytest.raises(EnrichedRepositoryRefreshError) as captured:
        run_enriched_job_with_repository_refresh(
            repo,
            lambda: events.append("publish") or {"rows": 1},
            quotes,
        )

    assert "未确认装载当前 ready enriched generation" in str(
        captured.value.refresh_error
    )
    assert events == ["pause", "publish"]
    assert quotes.is_paused is True


def test_sync_refresh_waits_for_startup_warmup_before_installing_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    repo = KlineRepository(DataStore(tmp_path))
    events: list[str] = []
    warmup_started = threading.Event()
    release_warmup = threading.Event()
    sync_attempted = threading.Event()
    sync_entered = threading.Event()

    monkeypatch.setattr(repo, "_refresh_instruments", lambda: None)
    monkeypatch.setattr(repo, "_refresh_index_instruments", lambda: None)
    monkeypatch.setattr(repo, "_refresh_etf_instruments", lambda: None)

    def refresh_locked() -> str:
        if threading.current_thread().name == "enriched-warmup":
            events.append("warmup-start")
            warmup_started.set()
            assert release_warmup.wait(timeout=1.0)
            events.append("warmup-done")
            return "generation-a"
        events.append("sync-start")
        sync_entered.set()
        return "generation-b"

    monkeypatch.setattr(repo, "_refresh_enriched_locked", refresh_locked)
    repo._start_enriched_warmup()
    assert warmup_started.wait(timeout=1.0)

    result: list[str | None] = []

    def sync_refresh() -> None:
        sync_attempted.set()
        result.append(repo.refresh_cache())

    sync_thread = threading.Thread(target=sync_refresh)
    sync_thread.start()
    assert sync_attempted.wait(timeout=1.0)
    assert not sync_entered.wait(timeout=0.05)

    release_warmup.set()
    sync_thread.join(timeout=1.0)
    assert not sync_thread.is_alive()
    assert repo._warmup_thread is not None
    repo._warmup_thread.join(timeout=1.0)

    assert events == ["warmup-start", "warmup-done", "sync-start"]
    assert result == ["generation-b"]
