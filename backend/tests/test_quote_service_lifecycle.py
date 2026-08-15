"""实时行情服务生命周期测试。"""

import threading

import pytest

from app.services import preferences
from app.services.quote_service import QuoteService


def _running_service() -> QuoteService:
    service = QuoteService()
    service._running = True
    service._enabled = True
    service._thread = None
    return service


def test_stop_only_stops_runtime_and_preserves_user_preference(monkeypatch):
    saved: list[bool] = []
    monkeypatch.setattr(QuoteService, "_save_enabled", staticmethod(saved.append))
    service = _running_service()

    service.stop()

    assert service._running is False
    assert service._enabled is False
    assert saved == []


def test_disable_stops_runtime_and_persists_user_preference(monkeypatch):
    saved: list[bool] = []
    monkeypatch.setattr(QuoteService, "_save_enabled", staticmethod(saved.append))
    service = _running_service()

    service.disable()

    assert service._running is False
    assert service._enabled is False
    assert saved == [False]


def test_disable_stops_runtime_even_if_preference_write_fails(monkeypatch):
    def fail_to_save(enabled):  # noqa: ARG001
        raise OSError("disk full")

    monkeypatch.setattr(QuoteService, "_save_enabled", staticmethod(fail_to_save))
    service = _running_service()

    with pytest.raises(OSError, match="disk full"):
        service.disable()

    assert service._running is False
    assert service._enabled is False


def test_boot_check_restores_enabled_service_after_runtime_stop(monkeypatch):
    persisted = {"enabled": True}
    starts: list[float] = []
    monkeypatch.setattr(
        QuoteService,
        "_save_enabled",
        staticmethod(lambda enabled: persisted.update(enabled=enabled)),
    )
    monkeypatch.setattr(
        preferences,
        "get_realtime_quotes_enabled",
        lambda: persisted["enabled"],
    )

    service = _running_service()
    service.stop()

    restarted = QuoteService()
    monkeypatch.setattr(restarted, "is_realtime_allowed", lambda: True)
    monkeypatch.setattr(restarted, "_start_runtime_locked", starts.append)
    monkeypatch.setattr(preferences, "get_realtime_quote_interval", lambda: 6.0)
    restarted.boot_check()

    assert persisted["enabled"] is True
    assert starts == [6.0]


def test_boot_check_preserves_preference_until_capability_recovers(monkeypatch):
    persisted = {"enabled": True}
    starts: list[float] = []
    availability = iter([False, True])
    monkeypatch.setattr(
        QuoteService,
        "_save_enabled",
        staticmethod(lambda enabled: persisted.update(enabled=enabled)),
    )
    monkeypatch.setattr(
        preferences,
        "get_realtime_quotes_enabled",
        lambda: persisted["enabled"],
    )

    service = QuoteService()
    monkeypatch.setattr(service, "is_realtime_allowed", lambda: next(availability))
    monkeypatch.setattr(service, "_start_runtime_locked", starts.append)
    monkeypatch.setattr(preferences, "get_realtime_quote_interval", lambda: 6.0)

    service.boot_check()
    service.boot_check()

    assert persisted["enabled"] is True
    assert starts == [6.0]


def test_concurrent_boot_checks_start_only_one_poll_thread(monkeypatch):
    service = QuoteService()
    release = threading.Event()
    started = 0
    started_lock = threading.Lock()

    def poll(stop_event):
        nonlocal started
        with started_lock:
            started += 1
        release.wait(timeout=1)

    monkeypatch.setattr(service, "_poll_loop", poll)
    monkeypatch.setattr(service, "is_realtime_allowed", lambda: True)
    monkeypatch.setattr(preferences, "get_realtime_quotes_enabled", lambda: True)
    monkeypatch.setattr(preferences, "get_realtime_quote_interval", lambda: 6.0)

    barrier = threading.Barrier(9)

    def reconcile():
        barrier.wait()
        service.boot_check()

    callers = [threading.Thread(target=reconcile) for _ in range(8)]
    for caller in callers:
        caller.start()
    barrier.wait()
    for caller in callers:
        caller.join(timeout=1)

    assert started == 1
    release.set()
    service.stop()


def test_capability_recovery_waits_for_blocked_poll_thread_to_exit(monkeypatch):
    service = QuoteService()
    service.STOP_JOIN_TIMEOUT = 0.01
    first_fetch_started = threading.Event()
    release_first_fetch = threading.Event()
    recovered_fetch_started = threading.Event()
    fetch_count = 0
    fetch_count_lock = threading.Lock()
    allowed = {"value": True}

    def fetch_quotes(*, final=False):  # noqa: ARG001
        nonlocal fetch_count
        with fetch_count_lock:
            fetch_count += 1
            current = fetch_count
        if current == 1:
            first_fetch_started.set()
            release_first_fetch.wait(timeout=1)
        else:
            recovered_fetch_started.set()
        return False

    monkeypatch.setattr(service, "_fetch_quotes", fetch_quotes)
    monkeypatch.setattr(service, "_market_phase", lambda: "morning")
    monkeypatch.setattr(service, "_should_fetch_for_phase", lambda phase: True)
    monkeypatch.setattr(service, "is_realtime_allowed", lambda: allowed["value"])
    monkeypatch.setattr(preferences, "get_realtime_quotes_enabled", lambda: True)
    monkeypatch.setattr(preferences, "get_realtime_quote_interval", lambda: 0.01)

    service.boot_check()
    assert first_fetch_started.wait(timeout=1)
    blocked_thread = service._thread

    allowed["value"] = False
    service.boot_check()
    assert blocked_thread is not None and blocked_thread.is_alive()

    allowed["value"] = True
    service.boot_check()
    assert service._thread is blocked_thread
    assert service._restart_pending is True

    release_first_fetch.set()
    assert recovered_fetch_started.wait(timeout=1)
    assert service._thread is not blocked_thread

    service.stop()
