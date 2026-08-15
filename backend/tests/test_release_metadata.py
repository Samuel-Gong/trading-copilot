from __future__ import annotations

from app.api import data as data_api
from app.api import routes
from app.config import settings


def test_health_exposes_release_metadata(monkeypatch) -> None:
    monkeypatch.setattr(routes.tf_client, "current_mode", lambda: "api_key")
    monkeypatch.setattr(settings, "git_sha", "a" * 40)
    monkeypatch.setattr(settings, "build_time", "2026-08-15T04:00:00Z")

    result = routes.health()

    assert result["status"] == "ok"
    assert result["git_sha"] == "a" * 40
    assert result["build_time"] == "2026-08-15T04:00:00Z"


def test_version_exposes_release_metadata(monkeypatch) -> None:
    monkeypatch.setattr(settings, "git_sha", "b" * 40)
    monkeypatch.setattr(settings, "build_time", "2026-08-15T05:00:00Z")

    result = data_api.get_version(None)  # type: ignore[arg-type]

    assert result["version"].startswith("v")
    assert result["git_sha"] == "b" * 40
    assert result["build_time"] == "2026-08-15T05:00:00Z"


def test_release_metadata_is_omitted_during_local_development(monkeypatch) -> None:
    monkeypatch.setattr(routes.tf_client, "current_mode", lambda: "none")
    monkeypatch.setattr(settings, "git_sha", "")
    monkeypatch.setattr(settings, "build_time", "")

    result = routes.health()

    assert "git_sha" not in result
    assert "build_time" not in result
