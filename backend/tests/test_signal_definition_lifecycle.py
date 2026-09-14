"""自定义信号定义图、引用保护与运行时失效测试。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import signals as signals_api
from app.api.signals import router
from app.services import strategy_cache
from app.strategy import config as strategy_config
from app.strategy import monitor_rules


def _client(data_dir: Path, calls: list[str] | None = None) -> TestClient:
    calls = calls if calls is not None else []
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=data_dir),
        clear_cache=lambda: calls.append("repo"),
    )
    app.state.strategy_engine = SimpleNamespace(
        invalidate_realtime_matrices=lambda: calls.append("matrix"),
    )
    app.state.monitor_engine = SimpleNamespace(
        invalidate_strategy_state=lambda: calls.append("monitor"),
    )
    return TestClient(app)


def _payload(signal_id: str = "lifecycle") -> dict:
    return {
        "id": signal_id,
        "name": "生命周期信号",
        "kind": "entry",
        "enabled": True,
        "conditions": [{
            "left": "close",
            "op": ">",
            "right": "0",
            "leftDays": 0,
            "rightDays": 0,
        }],
    }


def test_save_signal_invalidates_all_runtime_layers(tmp_path) -> None:
    calls: list[str] = []
    client = _client(tmp_path, calls)
    before_generation = strategy_cache.cache_generation(tmp_path, [])[0]

    response = client.post("/api/custom-signals", json=_payload())

    assert response.status_code == 200
    assert strategy_cache.cache_generation(tmp_path, [])[0] == before_generation + 1
    assert calls == ["matrix", "monitor", "repo"]


def test_delete_signal_rejects_all_persisted_reference_kinds(tmp_path) -> None:
    client = _client(tmp_path)
    assert client.post("/api/custom-signals", json=_payload("referenced")).status_code == 200

    strategy_dir = tmp_path / "strategies" / "custom"
    strategy_dir.mkdir(parents=True)
    (strategy_dir / "source.py").write_text(
        'REQUIRED_FEATURES = {"csg_referenced"}\n',
        encoding="utf-8",
    )
    strategy_config.save_override(
        tmp_path,
        "override_user",
        {"entry_signals": ["csg_referenced"]},
    )
    monitor_rules.save_one(tmp_path, monitor_rules.normalize({
        "id": "signal_reference",
        "name": "信号引用",
        "type": "signal",
        "scope": "symbols",
        "symbols": ["600000.SH"],
        "conditions": [{"field": "csg_referenced", "op": "truth"}],
    }))

    blocked = client.delete("/api/custom-signals/referenced")

    assert blocked.status_code == 409
    assert blocked.json()["detail"]["references"] == [
        "strategies/custom/source.py",
        "user_data/strategy_overrides/override_user.json",
        "user_data/monitor_rules/signal_reference.json",
    ]
    signal_path = tmp_path / "user_data" / "custom_signals" / "referenced.json"
    assert signal_path.is_file()

    forced = client.delete("/api/custom-signals/referenced?force=true")
    assert forced.status_code == 200
    assert not signal_path.exists()


def test_invalidation_failure_still_attempts_every_runtime_layer(
    tmp_path, monkeypatch,
) -> None:
    calls: list[str] = []
    client = _client(tmp_path, calls)
    from app.indicators import pipeline

    monkeypatch.setattr(
        pipeline,
        "invalidate_custom_signals",
        lambda: (_ for _ in ()).throw(RuntimeError("pipeline failed")),
    )
    monkeypatch.setattr(
        strategy_cache,
        "clear_cache",
        lambda _data_dir: calls.append("strategy-cache"),
    )

    with pytest.raises(RuntimeError, match="pipeline failed"):
        signals_api._invalidate(SimpleNamespace(app=client.app))

    assert calls == ["matrix", "monitor", "strategy-cache", "repo"]


def test_save_signal_invalidation_failure_restores_previous_definition(
    tmp_path,
    monkeypatch,
) -> None:
    client = _client(tmp_path)
    assert client.post("/api/custom-signals", json=_payload("rollback")).status_code == 200
    previous = _payload("rollback")
    from app.indicators import pipeline
    from app.strategy import custom_signals

    monkeypatch.setattr(
        pipeline,
        "invalidate_custom_signals",
        lambda: (_ for _ in ()).throw(OSError("synthetic invalidation failure")),
    )
    changed = _payload("rollback")
    changed["name"] = "不应生效"

    with pytest.raises(OSError, match="synthetic invalidation failure"):
        client.post("/api/custom-signals", json=changed)

    restored = next(item for item in custom_signals.load_all(tmp_path) if item["id"] == "rollback")
    assert restored == previous
