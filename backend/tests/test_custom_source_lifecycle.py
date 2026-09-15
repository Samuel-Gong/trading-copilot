from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import settings as settings_api
from app.config import settings
from app.data_providers import custom as custom_sources


def _source(field_map: dict[str, str]) -> settings_api.CustomSourceIn:
    return settings_api.CustomSourceIn.model_validate({
        "name": "atomic_source",
        "display_name": "Atomic Source",
        "datasets": {
            "daily": {
                "url": "https://example.test/daily",
                "field_map": field_map,
                "volume_unit": "lots",
            },
        },
    })


def test_invalid_source_update_preserves_previous_yaml_and_provider(
    tmp_path,
    monkeypatch,
) -> None:
    original_data_dir = settings.data_dir
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    valid_map = {
        name: name
        for name in ("symbol", "date", "open", "high", "low", "close", "volume", "amount")
    }
    monkeypatch.setattr(settings_api, "_refresh_provider_runtime", lambda request: None)
    request = SimpleNamespace()
    try:
        settings_api.save_data_source(_source(valid_map), request)
        target = custom_sources.data_sources_dir() / "atomic_source.yaml"
        previous = target.read_bytes()
        assert custom_sources.provider_has_dataset("atomic_source", "daily")

        with pytest.raises(HTTPException) as exc_info:
            settings_api.save_data_source(_source({"code": "symbol"}), request)

        assert exc_info.value.status_code == 400
        assert target.read_bytes() == previous
        assert custom_sources.provider_has_dataset("atomic_source", "daily")
        assert custom_sources.get_provider("atomic_source").config.display_name == "Atomic Source"
    finally:
        monkeypatch.setattr(settings, "data_dir", original_data_dir)
        custom_sources.load_all()
