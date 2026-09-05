from __future__ import annotations

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
