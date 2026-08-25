"""会推进 enriched generation 的长任务必须同步恢复 Repository 快照。"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.jobs import daily_pipeline
from app.jobs.daily_pipeline import PipelineStageError
from app.services.enriched_job import run_enriched_job_with_repository_refresh


class _Repo:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.generation = "before"

    def get_matrix_data_generation(self, asset_type: str) -> str:
        assert asset_type == "stock"
        return self.generation

    def refresh_cache(self) -> None:
        self.events.append("refresh")


class _QuoteService:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    @contextmanager
    def paused(self):
        self.events.append("pause")
        try:
            yield
        finally:
            self.events.append("resume")


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
