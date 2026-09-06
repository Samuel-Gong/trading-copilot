from __future__ import annotations

import importlib

import pytest

from app.services import strategy_cache


def _result(*symbols: str, as_of: str = "2026-07-20") -> dict:
    return {
        "total": len(symbols),
        "as_of": as_of,
        "rows": [{"symbol": symbol, "close": index + 1.0} for index, symbol in enumerate(symbols)],
    }


def test_same_day_partial_writes_merge_strategy_results(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("000001.SZ")})
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_b": _result("600000.SH")})

    cached = strategy_cache.read_cache(tmp_path)

    assert set(cached["results"]) == {"strategy_a", "strategy_b"}
    assert cached["results"]["strategy_a"]["rows"][0]["symbol"] == "000001.SZ"
    assert cached["results"]["strategy_b"]["rows"][0]["symbol"] == "600000.SH"


def test_same_day_update_replaces_only_target_strategy_and_keeps_ever_rows(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {
        "strategy_a": _result("000001.SZ"),
        "strategy_b": _result("600000.SH"),
    })
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("000002.SZ")})

    cached = strategy_cache.read_cache(tmp_path)

    assert [row["symbol"] for row in cached["results"]["strategy_a"]["rows"]] == ["000002.SZ"]
    assert [row["symbol"] for row in cached["results"]["strategy_b"]["rows"]] == ["600000.SH"]
    assert set(cached["today_ever_rows"]["strategy_a"]) == {"000001.SZ", "000002.SZ"}


def test_selective_invalidation_keeps_unaffected_rows_from_inflight_batch_write(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {
        "alpha": _result("000001.SZ"),
        "beta": _result("600000.SH"),
    })
    batch_generation = strategy_cache.cache_generation(tmp_path, ["alpha", "beta"])

    strategy_cache.clear_strategy_results(tmp_path, {"alpha"})
    strategy_cache.write_cache(
        tmp_path,
        "2026-07-20",
        {
            "alpha": _result("000002.SZ"),
            "beta": _result("600001.SH"),
        },
        expected_generation=batch_generation,
    )

    cached = strategy_cache.read_cache(tmp_path)
    assert set(cached["results"]) == {"beta"}
    assert cached["results"]["beta"]["rows"][0]["symbol"] == "600001.SH"


def test_new_date_resets_results_and_ever_rows(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("000001.SZ")})
    next_day = _result("600000.SH")
    next_day["as_of"] = "2026-07-21"

    strategy_cache.write_cache(tmp_path, "2026-07-21", {"strategy_b": next_day})
    cached = strategy_cache.read_cache(tmp_path)

    assert cached["as_of"] == "2026-07-21"
    assert set(cached["results"]) == {"strategy_b"}
    assert set(cached["today_ever_rows"]) == {"strategy_b"}


def test_guarded_write_preserves_newer_date_but_allows_initial_same_and_next_day(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"a": _result("000001.SZ")}, preserve_newer=True)
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"b": _result("600000.SH")}, preserve_newer=True)
    original = strategy_cache.read_cache(tmp_path)
    assert set(original["results"]) == {"a", "b"}
    strategy_cache.write_cache(tmp_path, "2026-07-19", {
        "a": _result("000002.SZ", as_of="2026-07-19"),
    }, preserve_newer=True)
    assert strategy_cache.read_cache(tmp_path) == original
    strategy_cache.write_cache(tmp_path, "2026-07-21", {
        "a": _result("000003.SZ", as_of="2026-07-21"),
    }, preserve_newer=True)
    latest = strategy_cache.read_cache(tmp_path)
    assert latest["as_of"] == "2026-07-21"
    assert set(latest["results"]) == {"a"}


def test_guarded_write_can_replace_invalid_old_date(tmp_path):
    strategy_cache.write_cache(tmp_path, "invalid", {"a": _result("000001.SZ")})
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"a": _result("000002.SZ")}, preserve_newer=True)
    assert strategy_cache.read_cache(tmp_path)["as_of"] == "2026-07-20"


def test_guarded_write_replaces_future_cache_beyond_latest_available_data(tmp_path):
    strategy_cache.write_cache(tmp_path, "2099-01-01", {
        "a": _result("000001.SZ", as_of="2099-01-01"),
    })

    strategy_cache.write_cache(
        tmp_path,
        "2026-07-20",
        {"a": _result("000002.SZ")},
        preserve_newer=True,
        latest_available_as_of="2026-07-20",
    )

    cached = strategy_cache.read_cache(tmp_path)
    assert cached["as_of"] == "2026-07-20"
    assert cached["results"]["a"]["rows"][0]["symbol"] == "000002.SZ"


@pytest.mark.parametrize(
    "latest_available",
    ["not-a-date", lambda: (_ for _ in ()).throw(ValueError("latest date unavailable"))],
)
def test_only_latest_write_rejects_unconfirmed_latest_date(tmp_path, latest_available):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"a": _result("000001.SZ")})

    strategy_cache.write_cache(
        tmp_path,
        "2026-07-19",
        {"a": _result("000002.SZ", as_of="2026-07-19")},
        latest_available_as_of=latest_available,
        only_latest_available=True,
    )

    cached = strategy_cache.read_cache(tmp_path)
    assert cached is not None
    assert cached["as_of"] == "2026-07-20"
    assert cached["results"]["a"]["rows"][0]["symbol"] == "000001.SZ"


def test_cache_write_failure_removes_previous_export_snapshot(tmp_path, monkeypatch):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"a": _result("000001.SZ")})
    monkeypatch.setattr(
        strategy_cache.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("synthetic failure")),
    )

    with pytest.raises(OSError, match="synthetic failure"):
        strategy_cache.write_cache(tmp_path, "2026-07-20", {"a": _result("000002.SZ")})

    assert strategy_cache.read_cache(tmp_path) is None


def test_cache_write_failure_hides_previous_snapshot_when_cleanup_fails(tmp_path, monkeypatch):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"a": _result("000001.SZ")})
    monkeypatch.setattr(
        strategy_cache.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(PermissionError("replace denied")),
    )
    monkeypatch.setattr(
        strategy_cache.Path,
        "unlink",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("unlink denied")),
    )

    with pytest.raises(PermissionError, match="replace denied"):
        strategy_cache.write_cache(tmp_path, "2026-07-20", {"a": _result("000002.SZ")})

    assert strategy_cache.read_cache(tmp_path) is None


def test_cache_tombstone_hides_previous_snapshot_after_module_reload(tmp_path, monkeypatch):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"a": _result("000001.SZ")})
    monkeypatch.setattr(
        strategy_cache.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(PermissionError("replace denied")),
    )
    monkeypatch.setattr(
        strategy_cache.Path,
        "unlink",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("unlink denied")),
    )

    with pytest.raises(PermissionError, match="replace denied"):
        strategy_cache.write_cache(tmp_path, "2026-07-20", {"a": _result("000002.SZ")})

    restarted_cache = importlib.reload(strategy_cache)
    assert restarted_cache.read_cache(tmp_path) is None


def test_persisted_generation_rejects_old_process_write_after_cache_clear(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"a": _result("000001.SZ")})
    old_generation = strategy_cache.cache_generation(tmp_path, ["a"])

    strategy_cache.clear_strategy_results(tmp_path, {"a"})
    restarted_cache = importlib.reload(strategy_cache)
    restarted_cache.write_cache(
        tmp_path,
        "2026-07-20",
        {"a": _result("000002.SZ")},
        expected_generation=old_generation,
    )

    assert restarted_cache.read_cache(tmp_path) is None


def test_fresh_generation_write_clears_tombstone_after_cache_clear(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"a": _result("000001.SZ")})
    strategy_cache.clear_cache(tmp_path)
    generation = strategy_cache.cache_generation(tmp_path, ["a"])

    strategy_cache.write_cache(
        tmp_path,
        "2026-07-20",
        {"a": _result("000002.SZ")},
        expected_generation=generation,
    )

    cached = strategy_cache.read_cache(tmp_path)
    assert cached is not None
    assert cached["results"]["a"]["rows"][0]["symbol"] == "000002.SZ"
    assert not strategy_cache._invalid_cache_path(strategy_cache._cache_path(tmp_path)).exists()


def test_corrupt_generation_state_hides_cache_and_rejects_old_write(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"a": _result("000001.SZ")})
    old_generation = strategy_cache.cache_generation(tmp_path, ["a"])
    generation_path = strategy_cache._generation_path(strategy_cache._cache_path(tmp_path))
    generation_path.write_text("{not-json", encoding="utf-8")
    restarted_cache = importlib.reload(strategy_cache)

    assert restarted_cache.read_cache(tmp_path) is None
    with pytest.raises(restarted_cache.CacheGenerationStateError):
        restarted_cache.write_cache(
            tmp_path,
            "2026-07-20",
            {"a": _result("000002.SZ")},
            expected_generation=old_generation,
        )
    assert restarted_cache.read_cache(tmp_path) is None


def test_persisted_generation_hides_cache_when_marker_and_delete_both_fail(tmp_path, monkeypatch):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"a": _result("000001.SZ")})
    old_generation = strategy_cache.cache_generation(tmp_path, ["a"])
    cache_path = strategy_cache._cache_path(tmp_path)
    marker_path = strategy_cache._invalid_cache_path(cache_path)
    original_write_text = strategy_cache.Path.write_text
    original_unlink = strategy_cache.Path.unlink

    failures = {"marker_write": 0, "cache_delete": 0}

    def fail_marker_write(path, *args, **kwargs):
        if path == marker_path:
            failures["marker_write"] += 1
            raise PermissionError("marker write denied")
        return original_write_text(path, *args, **kwargs)

    def fail_cache_delete(path, *args, **kwargs):
        if path in {cache_path, cache_path.with_name(cache_path.name + ".tmp")}:
            failures["cache_delete"] += 1
            raise PermissionError("cache delete denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(strategy_cache.Path, "write_text", fail_marker_write)
    monkeypatch.setattr(strategy_cache.Path, "unlink", fail_cache_delete)

    strategy_cache.clear_cache(tmp_path)
    assert cache_path.exists()
    restarted_cache = importlib.reload(strategy_cache)
    restarted_cache.write_cache(
        tmp_path,
        "2026-07-20",
        {"a": _result("000002.SZ")},
        expected_generation=old_generation,
    )

    assert restarted_cache.read_cache(tmp_path) is None
    assert failures["marker_write"] == 1
    assert failures["cache_delete"] == 2


def test_cache_lock_rejects_platform_without_cross_process_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(strategy_cache, "fcntl", None)
    monkeypatch.setattr(strategy_cache, "msvcrt", None)

    with pytest.raises(RuntimeError, match="跨进程锁"):
        strategy_cache.cache_generation(tmp_path, ["a"])


def test_cache_lock_uses_windows_fallback_when_fcntl_is_unavailable(tmp_path, monkeypatch):
    calls: list[tuple[int, int]] = []

    class FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(fd, mode, length):
            calls.append((mode, length))

    monkeypatch.setattr(strategy_cache, "fcntl", None)
    monkeypatch.setattr(strategy_cache, "msvcrt", FakeMsvcrt)

    assert strategy_cache.cache_generation(tmp_path, ["a"]) == (0, {"a": 0})
    assert calls == [(FakeMsvcrt.LK_LOCK, 1), (FakeMsvcrt.LK_UNLCK, 1)]


def test_cache_write_fails_when_tombstone_cannot_be_removed(tmp_path, monkeypatch):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"a": _result("000001.SZ")})
    strategy_cache.clear_cache(tmp_path)
    generation = strategy_cache.cache_generation(tmp_path, ["a"])
    marker = strategy_cache._invalid_cache_path(strategy_cache._cache_path(tmp_path))
    original_unlink = strategy_cache.Path.unlink

    def fail_marker_unlink(path, *args, **kwargs):
        if path == marker:
            raise PermissionError("marker unlink denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(strategy_cache.Path, "unlink", fail_marker_unlink)

    with pytest.raises(PermissionError, match="marker unlink denied"):
        strategy_cache.write_cache(
            tmp_path,
            "2026-07-20",
            {"a": _result("000002.SZ")},
            expected_generation=generation,
        )

    assert strategy_cache.read_cache(tmp_path) is None


def test_selective_clear_hides_previous_snapshot_when_cleanup_fails(tmp_path, monkeypatch):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {
        "alpha": _result("000001.SZ"),
        "beta": _result("600000.SH"),
    })
    monkeypatch.setattr(
        strategy_cache.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(PermissionError("replace denied")),
    )
    monkeypatch.setattr(
        strategy_cache.Path,
        "unlink",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("unlink denied")),
    )

    with pytest.raises(PermissionError, match="replace denied"):
        strategy_cache.clear_strategy_results(tmp_path, {"alpha"})

    assert strategy_cache.read_cache(tmp_path) is None
