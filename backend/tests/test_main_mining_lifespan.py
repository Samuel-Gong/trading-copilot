from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import app.main as main_module


def test_cold_start_loads_custom_sources_before_capability_detection(monkeypatch) -> None:
    from app.data_providers import custom as custom_sources
    from app.tickflow.capabilities import CapabilitySet

    loaded = False
    sentinel = CapabilitySet()

    def _load_all() -> None:
        nonlocal loaded
        loaded = True

    def _detect():
        assert loaded
        return sentinel

    monkeypatch.setattr(custom_sources, "load_all", _load_all)
    monkeypatch.setattr(custom_sources, "list_sources", lambda: [])
    monkeypatch.setattr(main_module, "detect_capabilities", _detect)
    app = SimpleNamespace(state=SimpleNamespace())

    assert main_module._load_data_sources_and_capabilities(app) is sentinel
    assert app.state.capabilities is sentinel


def test_lifespan_holds_mining_process_lock_around_application(monkeypatch) -> None:
    events: list[str] = []

    class LockStub:
        def __init__(self, data_dir) -> None:
            del data_dir
            events.append("lock_created")

        def acquire(self) -> None:
            events.append("lock_acquired")

        def release(self) -> None:
            events.append("lock_released")

    @asynccontextmanager
    async def application_lifespan(_app):
        events.append("application_started")
        try:
            yield
        finally:
            events.append("application_stopped")

    monkeypatch.setattr(main_module, "MiningProcessLock", LockStub)
    monkeypatch.setattr(main_module, "_application_lifespan", application_lifespan)

    async def exercise() -> None:
        async with main_module.lifespan(SimpleNamespace()):
            events.append("request_serving")

    asyncio.run(exercise())

    assert events == [
        "lock_created",
        "lock_acquired",
        "application_started",
        "request_serving",
        "application_stopped",
        "lock_released",
    ]


def test_lifespan_releases_lock_when_application_shutdown_raises(monkeypatch) -> None:
    events: list[str] = []

    class LockStub:
        def __init__(self, data_dir) -> None:
            del data_dir

        def acquire(self) -> None:
            events.append("acquired")

        def release(self) -> None:
            events.append("released")

    @asynccontextmanager
    async def application_lifespan(_app):
        try:
            yield
        finally:
            raise RuntimeError("shutdown failed")

    monkeypatch.setattr(main_module, "MiningProcessLock", LockStub)
    monkeypatch.setattr(main_module, "_application_lifespan", application_lifespan)

    async def exercise() -> None:
        async with main_module.lifespan(SimpleNamespace()):
            pass

    with pytest.raises(RuntimeError, match="shutdown failed"):
        asyncio.run(exercise())

    assert events == ["acquired", "released"]
