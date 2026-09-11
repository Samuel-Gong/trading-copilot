"""自定义信号表达式缓存的并发失效回归测试。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

from app.indicators import pipeline
from app.strategy import custom_signals


def _signal(signal_id: str) -> dict:
    return {
        "id": signal_id,
        "name": signal_id,
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


def test_invalidation_discards_inflight_old_signal_snapshot(monkeypatch) -> None:
    """旧读取跨过失效点后不得重新提交为无限期缓存。"""
    first_load_started = Event()
    release_first_load = Event()
    call_lock = Lock()
    load_count = 0

    def controlled_load(_data_dir):
        nonlocal load_count
        with call_lock:
            load_count += 1
            current = load_count
        if current == 1:
            first_load_started.set()
            assert release_first_load.wait(timeout=2)
            return [_signal("old")]
        return [_signal("new")]

    monkeypatch.setattr(custom_signals, "load_all", controlled_load)
    pipeline.invalidate_custom_signals()
    with ThreadPoolExecutor(max_workers=1) as pool:
        loading = pool.submit(pipeline._get_custom_signal_exprs)
        assert first_load_started.wait(timeout=1)
        pipeline.invalidate_custom_signals()
        release_first_load.set()
        loaded = loading.result(timeout=2)

    assert set(loaded) == {"csg_new"}
    assert set(pipeline._get_custom_signal_exprs()) == {"csg_new"}
    assert load_count == 2
    pipeline.invalidate_custom_signals()
