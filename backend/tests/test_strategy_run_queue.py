"""strategy_run_queue 单测 — 耗时排序、耗时落盘、handle 状态、单飞管理器。"""
from __future__ import annotations

import threading
import time

from app.services import strategy_run_queue as q


def test_order_strategy_ids_fast_first_unknown_last():
    ids = ["slow_a", "mid_b", "fast_c", "new_d", "new_e"]
    timings = {"slow_a": 5000.0, "fast_c": 100.0, "mid_b": 1000.0}
    assert q.order_strategy_ids(ids, timings) == ["fast_c", "mid_b", "slow_a", "new_d", "new_e"]


def test_run_timings_record_merge_and_load(tmp_path):
    assert q.load_run_timings(tmp_path) == {}
    q.record_run_timings(tmp_path, {"a": 100.0})
    q.record_run_timings(tmp_path, {"b": 200.0, "a": 50.0})
    assert q.load_run_timings(tmp_path) == {"a": 50.0, "b": 200.0}


def test_run_timings_survives_corrupt_file(tmp_path):
    path = q._timings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert q.load_run_timings(tmp_path) == {}
    q.record_run_timings(tmp_path, {"a": 1.0})
    assert q.load_run_timings(tmp_path) == {"a": 1.0}


def test_handle_snapshot_lifecycle():
    h = q.StrategyRunHandle(("k",), ["s1", "s2"])
    snap = h.snapshot()
    assert snap["pending"] == ["s1", "s2"]
    assert snap["done"] is False

    h.complete("s1", {"total": 3})
    snap = h.snapshot()
    assert snap["results"] == {"s1": {"total": 3}}
    assert snap["pending"] == ["s2"]

    h.fail("boom")
    snap = h.snapshot()
    assert snap["error"] == "boom"
    assert snap["done"] is True


def _wait_done(handle, timeout=5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if handle.snapshot()["done"]:
            return
        time.sleep(0.01)
    raise AssertionError("handle 未在时限内完成")


def test_manager_piggybacks_same_running_key():
    mgr = q.StrategyRunManager()
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def job(handle):
        calls.append(1)
        started.set()
        assert release.wait(timeout=5)

    h1 = mgr.get_or_submit(("k",), ["s"], job)
    assert started.wait(timeout=5)
    # 执行中: 相同 key 搭车, 不重复提交
    h2 = mgr.get_or_submit(("k",), ["s"], job)
    assert h2 is h1

    release.set()
    _wait_done(h1)
    assert calls == [1]

    # 已完成后: 同 key 再来 → 新执行 (重跑语义)
    h3 = mgr.get_or_submit(("k",), ["s"], job)
    assert h3 is not h1
    _wait_done(h3)
    assert calls == [1, 1]


def test_manager_piggybacks_same_queued_key():
    mgr = q.StrategyRunManager()
    started = threading.Event()
    release = threading.Event()

    def job(handle):
        started.set()
        assert release.wait(timeout=5)

    h1 = mgr.get_or_submit(("a",), ["s"], job)  # 占住唯一 worker
    assert started.wait(timeout=5)
    # key b 排队 (未开始), 此时 b 的重复请求应搭车排队中的 handle
    h2 = mgr.get_or_submit(("b",), ["s"], job)
    h3 = mgr.get_or_submit(("b",), ["s"], job)
    assert h2 is h3

    release.set()
    _wait_done(h1)
    _wait_done(h2, timeout=10)


def test_manager_serializes_different_keys():
    mgr = q.StrategyRunManager()
    lock = threading.Lock()
    active: list[str] = []
    overlap: list[list[str]] = []

    def make_job(name):
        def job(handle):
            with lock:
                active.append(name)
                if len(active) > 1:
                    overlap.append(list(active))
            time.sleep(0.1)
            with lock:
                active.remove(name)

        return job

    h1 = mgr.get_or_submit(("a",), ["s"], make_job("a"))
    h2 = mgr.get_or_submit(("b",), ["s"], make_job("b"))
    _wait_done(h1, timeout=10)
    _wait_done(h2, timeout=10)
    assert overlap == []


def test_completed_status_survives_next_run_then_expires():
    manager = q.StrategyRunManager()
    first = manager.get_or_submit(("scope", "a"), ["a"], lambda handle: handle.fail_one("a", "失败"))
    _wait_done(first)
    second = manager.get_or_submit(("scope", "b"), ["b"], lambda handle: handle.complete("b", {"total": 0}))
    _wait_done(second)
    assert manager.get_status(first.run_id, "scope")["errors"] == {"a": "失败"}
    assert manager.get_status(first.run_id, "other") is None
    first.finished_at -= 601
    assert manager.get_status(first.run_id, "scope") is None
    assert manager.get_status(second.run_id, "scope")["done"] is True


def test_long_queued_run_keeps_status_after_completion(monkeypatch):
    manager = q.StrategyRunManager()
    entered = threading.Event()
    release = threading.Event()
    clock = {"now": 0.0}
    monkeypatch.setattr(q.time, "monotonic", lambda: clock["now"])

    def job(handle):
        entered.set()
        assert release.wait(timeout=5)

    handle = manager.get_or_submit(("scope",), ["slow"], job)
    assert entered.wait(timeout=5)
    clock["now"] = 1200.0
    release.set()
    _wait_done(handle)
    assert manager.get_status(handle.run_id, "scope")["done"] is True
    clock["now"] = 1799.0
    assert manager.get_status(handle.run_id, "scope") is not None
    clock["now"] = 1801.0
    assert manager.get_status(handle.run_id, "scope") is None
