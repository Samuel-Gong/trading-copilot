"""回归测试: Free 档自选实时按 capability batch 上限分批请求。"""
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import polars as pl

from app.services.quote_service import QuoteService
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


def _make_svc(engine_rules: dict) -> QuoteService:
    """创建最小可用的 QuoteService 实例, 跳过 __init__。"""
    svc = QuoteService.__new__(QuoteService)
    svc._app_state = MagicMock()
    svc._repo = MagicMock()
    svc._lock = MagicMock()

    engine = MagicMock()
    engine.rules = engine_rules
    svc._app_state.monitor_engine = engine
    svc._repo.get_index_symbol_set.return_value = {"000001.SH"}
    svc._repo.get_etf_symbol_set.return_value = set()
    return svc


def _run_fetch(
    svc,
    tf,
    watchlist: list[str],
    capset: CapabilitySet,
    *,
    final_boundary_ms: int | None = None,
):
    """在完整 patch 环境下执行 _fetch_watchlist_quotes。"""
    with ExitStack() as stack:
        stack.enter_context(patch(
            "app.services.preferences.get_realtime_watchlist_symbols", return_value=watchlist,
        ))
        stack.enter_context(patch(
            "app.tickflow.client.get_paid_realtime_client", return_value=tf,
        ))
        stack.enter_context(patch(
            "app.tickflow.policy.detect_capabilities", return_value=capset,
        ))
        stack.enter_context(patch("app.tickflow.rate_limits.sleep_between_batches"))
        stack.enter_context(patch.object(
            QuoteService, "_build_daily", return_value=pl.DataFrame(),
        ))
        stack.enter_context(patch.object(
            QuoteService, "_build_quote_extra", return_value=pl.DataFrame(),
        ))
        stack.enter_context(patch.object(
            QuoteService, "_build_index_quotes", return_value=pl.DataFrame(),
        ))
        broadcast = stack.enter_context(patch.object(QuoteService, "_broadcast_quote_updated"))
        evaluate = stack.enter_context(patch.object(QuoteService, "_evaluate_monitors"))
        stack.enter_context(patch("app.services.quote_service._persist_last_fetch"))
        svc._fetch_watchlist_quotes(final_boundary_ms=final_boundary_ms)
        return broadcast, evaluate


def test_watchlist_batch_respects_capability_limit():
    """6 个标的 / batch 5 → 分 2 批请求, 不整轮失败。"""
    svc = _make_svc({
        "r_idx": {
            "enabled": True,
            "asset_type": "index",
            "scope": "symbols",
            "symbols": ["000001.SH"],
        },
    })
    tf = MagicMock()
    tf.quotes.get.return_value = [
        {"symbol": "600000.SH", "last_price": 10.0, "prev_close": 9.9, "ext": {}},
    ]
    capset = CapabilitySet({Cap.QUOTE_BY_SYMBOL: CapabilityLimits(batch=5, rpm=60)})

    _run_fetch(
        svc,
        tf,
        ["600000.SH", "600001.SH", "600002.SH", "600003.SH", "600004.SH"],
        capset,
    )

    assert tf.quotes.get.call_count == 2
    first_batch = tf.quotes.get.call_args_list[0].kwargs["symbols"]
    second_batch = tf.quotes.get.call_args_list[1].kwargs["symbols"]
    assert len(first_batch) == 5
    assert second_batch == ["000001.SH"]


def test_watchlist_batch_partial_failure_keeps_other_batches():
    """某一批拉取失败不影响其他批次。"""
    svc = _make_svc({
        "r_idx": {
            "enabled": True,
            "asset_type": "index",
            "scope": "symbols",
            "symbols": ["000001.SH"],
        },
    })
    tf = MagicMock()
    tf.quotes.get.side_effect = [
        [{"symbol": "600000.SH", "last_price": 10.0, "prev_close": 9.9, "ext": {}}],
        ConnectionError("timeout"),
    ]
    capset = CapabilitySet({Cap.QUOTE_BY_SYMBOL: CapabilityLimits(batch=5, rpm=60)})

    _run_fetch(
        svc,
        tf,
        ["600000.SH", "600001.SH", "600002.SH", "600003.SH", "600004.SH"],
        capset,
    )

    assert tf.quotes.get.call_count == 2


def test_watchlist_no_index_rules_no_extra_symbols():
    """无指数监控规则时不追加指数标的。"""
    svc = _make_svc({})
    tf = MagicMock()
    tf.quotes.get.return_value = [
        {"symbol": "600000.SH", "last_price": 10.0, "prev_close": 9.9, "ext": {}},
    ]
    capset = CapabilitySet({Cap.QUOTE_BY_SYMBOL: CapabilityLimits(batch=5, rpm=60)})

    _run_fetch(svc, tf, ["600000.SH", "600001.SH"], capset)

    assert tf.quotes.get.call_count == 1
    assert tf.quotes.get.call_args_list[0].kwargs["symbols"] == ["600000.SH", "600001.SH"]


def test_watchlist_final_snapshot_before_boundary_does_not_write():
    """Free 档定版快照未到边界时只通知展示, 不写盘或评估监控。"""
    svc = _make_svc({})
    tf = MagicMock()
    tf.quotes.get.return_value = [{
        "symbol": "600000.SH",
        "last_price": 10.0,
        "prev_close": 9.9,
        "timestamp": 1_000,
        "ext": {},
    }]
    capset = CapabilitySet({Cap.QUOTE_BY_SYMBOL: CapabilityLimits(batch=5, rpm=60)})

    broadcast, evaluate = _run_fetch(
        svc, tf, ["600000.SH"], capset, final_boundary_ms=10_000,
    )

    assert svc._last_final_confirmed is False
    svc._repo.merge_live_daily_asset.assert_not_called()
    broadcast.assert_called_once_with()
    evaluate.assert_not_called()
