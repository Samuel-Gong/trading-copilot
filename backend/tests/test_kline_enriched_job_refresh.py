"""会推进 enriched generation 的长任务必须同步恢复 Repository 快照。"""
from __future__ import annotations

from contextlib import contextmanager

from app.api.kline import _run_enriched_job_with_repository_refresh


class _Repo:
    def __init__(self, events: list[str]) -> None:
        self.events = events

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

    result = _run_enriched_job_with_repository_refresh(
        repo,
        lambda: events.append("publish") or {"rows": 1},
        quotes,
    )

    assert result == {"rows": 1}
    assert events == ["pause", "publish", "refresh", "resume"]


def test_failed_enriched_job_does_not_replace_repository_snapshot() -> None:
    events: list[str] = []
    repo = _Repo(events)

    result = _run_enriched_job_with_repository_refresh(
        repo,
        lambda: events.append("failed") or {"error": "injected"},
    )

    assert result == {"error": "injected"}
    assert events == ["failed"]
