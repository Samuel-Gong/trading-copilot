import math
import os
import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import polars as pl
import pytest
import yaml
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.settings import (
    CustomSourceIn,
    CustomSourceTestIn,
    DatasetConfigIn,
)
from app.api import settings as settings_api
from app.api.settings import (
    test_data_source as run_data_source_test,
)
from app.data_providers.custom.config import (
    MAX_TIMEOUT,
    AuthConfig,
    CustomSourceConfig,
    DatasetConfig,
    _dataset_from_dict,
    config_from_dict,
    load_config,
)
from app.data_providers.custom.loader import _config_to_dict, _sanitize_for_yaml
from app.data_providers.custom.provider import GenericHTTPProvider


def test_minute_request_parameter_names_survive_config_round_trip():
    dataset = DatasetConfigIn(
        url="https://example.test/minute",
        method="GET",
        asset_type_param="asset",
        freq_param="period",
    ).model_dump()

    cleaned = _sanitize_for_yaml({
        "name": "test_source",
        "display_name": "Test Source",
        "datasets": {"minute": dataset},
    })
    parsed = _dataset_from_dict(cleaned["datasets"]["minute"])
    exposed = _config_to_dict(CustomSourceConfig(
        name="test_source",
        display_name="Test Source",
        datasets={"minute": parsed},
    ))

    assert parsed.asset_type_param == "asset"
    assert parsed.freq_param == "period"
    assert exposed["datasets"]["minute"]["asset_type_param"] == "asset"
    assert exposed["datasets"]["minute"]["freq_param"] == "period"


def test_plugin_uninstall_resets_every_selected_provider_route(monkeypatch) -> None:
    from app.data_providers import custom as custom_sources
    from app.data_providers.capabilities import CAPABILITY_REGISTRY
    from app.services import preferences

    selected = {
        item["field"]: "fuyao"
        for item in CAPABILITY_REGISTRY
        if item.get("field")
    }
    expected = {
        item["field"]: item["default"]
        for item in CAPABILITY_REGISTRY
        if item.get("field")
    }
    saved: list[dict[str, str]] = []
    monkeypatch.setattr(preferences, "load", lambda: selected)
    monkeypatch.setattr(preferences, "save", lambda updates: saved.append(updates))
    monkeypatch.setattr(custom_sources, "is_builtin", lambda _name: True)
    monkeypatch.setattr(
        custom_sources,
        "uninstall_plugin",
        lambda _name: (True, "ok"),
    )
    monkeypatch.setattr(custom_sources, "load_all", lambda: None)
    monkeypatch.setattr(settings_api, "_refresh_provider_runtime", lambda _request: None)
    monkeypatch.setattr(settings_api, "list_data_sources", lambda: {})

    result = settings_api.uninstall_plugin("fuyao", SimpleNamespace())

    assert saved == [expected]
    assert result["uninstall_ok"] is True


def test_full_minute_survives_round_trip_and_routes_capability(monkeypatch):
    from app.data_providers.custom import loader
    from app.services import preferences
    from app.tickflow.capabilities import Cap, CapabilitySet
    from app.tickflow.policy import _augment_custom_sources

    required = ("symbol", "datetime", "open", "high", "low", "close", "volume", "amount")
    cleaned = _sanitize_for_yaml({
        "name": "full_roundtrip",
        "display_name": "Full Roundtrip",
        "datasets": {
            "full_minute": {
                "url": "https://example.test/full-minute",
                "field_map": {name: name for name in required},
                "asset_type_param": "asset",
                "freq_param": "period",
                "volume_unit": "lots",
            },
        },
    })
    parsed = config_from_dict(cleaned)
    exposed = _config_to_dict(parsed)
    reparsed = config_from_dict(_sanitize_for_yaml(exposed))
    provider = GenericHTTPProvider(parsed)
    monkeypatch.setitem(loader._PROVIDERS, "full_roundtrip", provider)
    monkeypatch.setattr(
        preferences,
        "get_full_minute_data_provider",
        lambda: "full_roundtrip",
    )

    assert loader.provider_has_dataset("full_roundtrip", "full_minute")
    assert parsed.datasets["full_minute"].asset_type_param == "asset"
    assert parsed.datasets["full_minute"].freq_param == "period"
    assert reparsed.datasets["full_minute"].asset_type_param == "asset"
    assert reparsed.datasets["full_minute"].freq_param == "period"
    capset = CapabilitySet()
    _augment_custom_sources(capset)
    assert capset.has(Cap.INTRADAY_UNIVERSE)
    provider.close()


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="平台不支持进程时区切换")
def test_full_minute_window_uses_beijing_day_under_utc_process_timezone(
    monkeypatch,
):
    previous_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    provider = GenericHTTPProvider(CustomSourceConfig(
        name="beijing_window",
        display_name="Beijing Window",
        datasets={"full_minute": _dataset_config("full_minute")},
    ))
    captured = {}

    def capture(dataset, symbols, start_time, end_time, asset_type, freq, callback):
        captured.update({
            "dataset": dataset,
            "symbols": symbols,
            "start": start_time,
            "end": end_time,
            "asset_type": asset_type,
            "freq": freq,
            "callback": callback,
        })
        return pl.DataFrame()

    try:
        monkeypatch.setattr(provider, "_fetch_minute_dataset", capture)
        provider.get_intraday_batch(["600000.SH"])
    finally:
        provider.close()
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()

    from app.market_time import cn_today

    assert captured["dataset"] == "full_minute"
    assert captured["start"].date() == cn_today()
    assert captured["end"].date() == cn_today()
    assert captured["start"].hour == captured["start"].minute == 0


def test_timeout_survives_config_round_trip():
    """timeout 必须在 UI 保存往返中保留 (核心修复), 且默认 30 不污染 YAML。"""
    dataset = DatasetConfigIn(
        url="https://example.test/daily",
        method="POST",
        timeout=120.0,
    ).model_dump()

    cleaned = _sanitize_for_yaml({
        "name": "test_source",
        "display_name": "Test Source",
        "datasets": {"daily": dataset},
    })
    parsed = _dataset_from_dict(cleaned["datasets"]["daily"])
    exposed = _config_to_dict(CustomSourceConfig(
        name="test_source",
        display_name="Test Source",
        datasets={"daily": parsed},
    ))

    assert parsed.timeout == 120.0
    assert exposed["datasets"]["daily"]["timeout"] == 120.0

    # 默认 30 不 emit, 保持 YAML 干净
    default_dataset = DatasetConfigIn(url="https://example.test/realtime", method="GET").model_dump()
    cleaned2 = _sanitize_for_yaml({
        "name": "test_source",
        "display_name": "Test Source",
        "datasets": {"realtime": default_dataset},
    })
    parsed2 = _dataset_from_dict(cleaned2["datasets"]["realtime"])
    exposed2 = _config_to_dict(CustomSourceConfig(
        name="test_source",
        display_name="Test Source",
        datasets={"realtime": parsed2},
    ))
    assert parsed2.timeout == 30.0
    realtime = exposed2["datasets"]["realtime"]
    assert "timeout" not in realtime
    assert "symbols_param" not in realtime
    assert "start_param" not in realtime
    assert "end_param" not in realtime

    explicit_default = _sanitize_for_yaml({
        "name": "test_source",
        "datasets": {
            "daily": {
                "url": "https://example.test/daily",
                "timeout": 30.0,
            },
        },
    })
    assert "timeout" not in explicit_default["datasets"]["daily"]


@pytest.mark.parametrize(
    "timeout",
    [0, -1, math.nan, math.inf, -math.inf, MAX_TIMEOUT + 1],
)
def test_timeout_api_rejects_out_of_range_or_non_finite_values(timeout):
    with pytest.raises(ValidationError):
        DatasetConfigIn(url="https://example.test/daily", timeout=timeout)


def test_invalid_yaml_timeout_is_a_load_error(tmp_path: Path):
    for index, timeout in enumerate((0, -1, math.nan, math.inf, -math.inf, "invalid")):
        path = tmp_path / f"invalid_{index}.yaml"
        path.write_text(
            "\n".join([
                "name: invalid",
                "datasets:",
                "  daily:",
                "    url: https://example.test/daily",
                f"    timeout: {timeout}",
            ]),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="timeout must be"):
            load_config(path)


@pytest.mark.parametrize("timeout", [0, -1, math.nan, math.inf, -math.inf, "invalid"])
def test_sanitizer_rejects_invalid_timeout(timeout):
    with pytest.raises(ValueError, match="timeout must be"):
        _sanitize_for_yaml({
            "name": "test_source",
            "datasets": {
                "realtime": {
                    "url": "https://example.test/realtime",
                    "timeout": timeout,
                    "symbols_param": "codes",
                    "start_param": "from",
                    "end_param": "to",
                },
            },
        })


def test_sanitizer_drops_realtime_request_params():
    cleaned = _sanitize_for_yaml({
        "name": "test_source",
        "datasets": {
            "realtime": {
                "url": "https://example.test/realtime",
                "symbols_param": "codes",
                "start_param": "from",
                "end_param": "to",
            },
        },
    })

    dataset = cleaned["datasets"]["realtime"]
    assert "symbols_param" not in dataset
    assert "start_param" not in dataset
    assert "end_param" not in dataset


def test_empty_request_parameter_names_restore_defaults():
    cleaned = _sanitize_for_yaml({
        "name": "test_source",
        "datasets": {
            "minute": {
                "url": "https://example.test/minute",
                "symbols_param": " ",
                "start_param": "\t",
                "end_param": "",
            },
        },
    })
    parsed = _dataset_from_dict(cleaned["datasets"]["minute"])

    assert parsed.symbols_param == "symbols"
    assert parsed.start_param == "start_time"
    assert parsed.end_param == "end_time"


def _dataset_config(dataset: str = "minute", **overrides) -> DatasetConfig:
    required = {
        "daily": ("symbol", "date", "open", "high", "low", "close", "volume", "amount"),
        "adj_factor": ("symbol", "trade_date", "ex_factor"),
        "realtime": ("symbol", "last_price", "prev_close", "open", "high", "low", "volume"),
        "minute": ("symbol", "datetime", "open", "high", "low", "close", "volume", "amount"),
        "full_minute": ("symbol", "datetime", "open", "high", "low", "close", "volume", "amount"),
    }
    values = {
        "url": f"https://example.test/{dataset}",
        "field_map": {name: name for name in required[dataset]},
        **({"volume_unit": "lots"} if "volume" in required[dataset] else {}),
        **({"adj_factor_kind": "event_ratio"} if dataset == "adj_factor" else {}),
        **overrides,
    }
    return DatasetConfig(**values)


def _capture_test_request(dataset: str, **overrides):
    provider = GenericHTTPProvider(CustomSourceConfig(
        name="test_source",
        display_name="Test Source",
        datasets={dataset: _dataset_config(dataset, **overrides)},
    ))
    captured = {}

    def request_rows(cfg, **kwargs):
        captured.update(kwargs)
        return []

    provider._request_rows = request_rows
    provider.test_dataset(dataset, ["600000.SH"])
    provider.close()
    return captured


def test_test_dataset_realtime_omits_symbol_and_time_parameters():
    assert _capture_test_request("realtime") == {}


@pytest.mark.parametrize("dataset", ["daily", "adj_factor"])
def test_test_dataset_history_uses_symbol_and_short_time_range(dataset):
    captured = _capture_test_request(dataset)

    assert captured["symbols"] == ["600000.SH"]
    assert isinstance(captured["start_time"], datetime)
    assert isinstance(captured["end_time"], datetime)
    assert (captured["end_time"] - captured["start_time"]).days == 7


def test_test_dataset_minute_injects_production_overrides():
    captured = _capture_test_request(
        "minute",
        asset_type_param="asset",
        freq_param="period",
    )

    assert captured["symbols"] == ["600000.SH"]
    assert captured["override_params"] == {"asset": "stock", "period": "1m"}
    assert captured["override_body"] == {"asset": "stock", "period": "1m"}


@pytest.mark.parametrize("dataset", ["daily", "minute"])
def test_duplicate_dynamic_parameter_names_are_rejected(dataset):
    config = _dataset_config(dataset, symbols_param="range", start_param="range")
    provider = GenericHTTPProvider(CustomSourceConfig(
        name="test_source",
        display_name="Test Source",
        datasets={dataset: config},
    ))

    try:
        assert provider.validate() == [
            f"{dataset}: duplicate request parameter names: range"
        ]
    finally:
        provider.close()

    with pytest.raises(ValueError, match="duplicate request parameter names: range"):
        _sanitize_for_yaml({
            "name": "test_source",
            "datasets": {
                dataset: {
                    "url": config.url,
                    "symbols_param": "range",
                    "start_param": "range",
                },
            },
        })


@pytest.mark.parametrize("auth_type", ["bearer", "header", "query"])
def test_authenticated_source_requires_valid_token_env(auth_type):
    dataset = _dataset_config("daily")
    missing = GenericHTTPProvider(CustomSourceConfig(
        name="missing_auth_env",
        display_name="Missing Auth Env",
        auth=AuthConfig(type=auth_type),
        datasets={"daily": dataset},
    ))
    invalid = GenericHTTPProvider(CustomSourceConfig(
        name="invalid_auth_env",
        display_name="Invalid Auth Env",
        auth=AuthConfig(type=auth_type, token_env="NOT VALID"),
        datasets={"daily": dataset},
    ))
    try:
        assert missing.validate() == [f"auth: token_env is required for {auth_type}"]
        assert invalid.validate() == [
            "auth: token_env must be a valid environment variable name"
        ]
    finally:
        missing.close()
        invalid.close()


@pytest.mark.parametrize("auth_type", ["bearer", "header", "query"])
def test_missing_auth_secret_stops_before_http_request(monkeypatch, auth_type):
    provider = GenericHTTPProvider(CustomSourceConfig(
        name="missing_secret",
        display_name="Missing Secret",
        auth=AuthConfig(type=auth_type, token_env="TEST_MISSING_CUSTOM_TOKEN"),
        datasets={"daily": _dataset_config("daily")},
    ))
    request = Mock()
    provider._client.request = request
    monkeypatch.setattr(
        "app.data_providers.custom.provider._token_from_env", lambda name: None,
    )
    try:
        with pytest.raises(RuntimeError, match="auth token is not set"):
            provider._request_rows(provider.config.datasets["daily"])
        request.assert_not_called()
    finally:
        provider.close()


def test_query_auth_http_error_never_exposes_secret(monkeypatch, caplog):
    from app.data_providers import custom as custom_sources

    secret = "must-not-leak-secret"
    provider = GenericHTTPProvider(CustomSourceConfig(
        name="query_auth",
        display_name="Query Auth",
        auth=AuthConfig(
            type="query",
            param="api_key",
            token_env="TEST_CUSTOM_QUERY_TOKEN",
        ),
        datasets={"daily": _dataset_config("daily")},
    ))
    request = httpx.Request(
        "GET", f"https://example.test/daily?api_key={secret}",
    )
    provider._client.request = Mock(return_value=httpx.Response(401, request=request))
    monkeypatch.setattr(
        "app.data_providers.custom.provider._token_from_env", lambda _name: secret,
    )
    monkeypatch.setattr(
        custom_sources,
        "lease_provider",
        Mock(return_value=nullcontext((provider, 1))),
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            run_data_source_test(CustomSourceTestIn(
                provider="query_auth",
                dataset="daily",
                symbols=["600000.SH"],
            ))
        assert exc_info.value.status_code == 400
        assert "HTTP 401" in exc_info.value.detail
        assert secret not in exc_info.value.detail
        assert secret not in caplog.text
    finally:
        provider.close()


def test_data_source_trial_uses_unsaved_config_and_closes_provider(monkeypatch):
    from app.data_providers import custom as custom_sources

    provider = Mock()
    provider.test_dataset.return_value = {
        "provider": "draft",
        "dataset": "realtime",
        "rows": 0,
        "columns": [],
        "preview": [],
    }
    create_provider = Mock(return_value=provider)
    monkeypatch.setattr(custom_sources, "create_provider", create_provider)
    config = CustomSourceIn(
        name="draft",
        datasets={
            "daily": DatasetConfigIn(url="https://unfinished.test"),
            "realtime": DatasetConfigIn(url="https://example.test/realtime"),
        },
    )

    result = run_data_source_test(CustomSourceTestIn(
        provider="draft",
        dataset="realtime",
        config=config,
    ))

    assert result["provider"] == "draft"
    tested = create_provider.call_args.args[0]
    assert list(tested["datasets"]) == ["realtime"]
    provider.test_dataset.assert_called_once_with("realtime", None)
    provider.close.assert_called_once_with()


def test_data_source_trial_wraps_missing_saved_provider_as_http_400(monkeypatch):
    from app.data_providers import custom as custom_sources

    monkeypatch.setattr(
        custom_sources,
        "lease_provider",
        Mock(side_effect=ValueError("not found")),
    )

    with pytest.raises(HTTPException) as exc_info:
        run_data_source_test(CustomSourceTestIn(provider="missing", dataset="daily"))

    assert exc_info.value.status_code == 400
    assert "not found" in exc_info.value.detail


def test_documented_mock_source_satisfies_realtime_timestamp_contract():
    example = (
        Path(__file__).resolve().parents[2]
        / "docs/examples/custom-data-source/mock_source.yaml"
    )
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    provider = GenericHTTPProvider(config_from_dict(raw))
    try:
        assert provider.validate() == []
        realtime_map = raw["datasets"]["realtime"]["field_map"]
        assert realtime_map["timestamp"] == "timestamp"
    finally:
        provider.close()
