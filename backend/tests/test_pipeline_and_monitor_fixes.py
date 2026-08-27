"""回归测试: 本轮修复的几处高风险行为(并发单飞 / 重任务槽 / sector fail-closed)。

均为纯逻辑, 不触网, 不依赖真实数据源。
"""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.jobs import daily_pipeline
from app.services import pipeline_jobs, preferences, quote_service, webhook_adapter
from app.services.pipeline_jobs import JobStore
from app.services.quote_service import QuoteService
from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine


class _AlertCapture:
    def __init__(self) -> None:
        self.alerts: list[dict] = []

    def push_alerts(self, alerts: list[dict]) -> None:
        self.alerts.extend(alerts)


def test_phase_alert_skips_historical_recompute(monkeypatch, tmp_path):
    """历史回填或 stale 重算不得重放早已发生的阶段切换。"""
    capture = _AlertCapture()
    monkeypatch.setattr(
        "app.services.regime_builder.latest_phase_transition",
        lambda data_dir: ("ebb", "ice", "2026-07-30"),
    )
    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: date(2026, 7, 31))
    monkeypatch.setattr(
        daily_pipeline,
        "_get_app_state",
        lambda: SimpleNamespace(quote_service=capture),
    )

    daily_pipeline._push_phase_change_alert(
        tmp_path,
        computed_dates={date(2026, 7, 30)},
    )

    assert capture.alerts == []


def test_phase_alert_pushes_current_recomputed_transition(monkeypatch, tmp_path):
    """仅本次确实重算的当前业务日切换可以推送。"""
    capture = _AlertCapture()
    monkeypatch.setattr(
        "app.services.regime_builder.latest_phase_transition",
        lambda data_dir: ("ebb", "ice", "2026-07-31"),
    )
    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: date(2026, 7, 31))
    monkeypatch.setattr(
        daily_pipeline,
        "_get_app_state",
        lambda: SimpleNamespace(quote_service=capture),
    )

    daily_pipeline._push_phase_change_alert(
        tmp_path,
        computed_dates={date(2026, 7, 31)},
    )

    assert len(capture.alerts) == 1
    assert capture.alerts[0]["type"] == "phase_change"

# ── JobStore 单飞 ────────────────────────────────────────────────────────

def test_create_singleflight_dedupes_pending_window(monkeypatch, tmp_path):
    """两次快速 create() 在 pending 窗口内应复用同一 job(is_new=False)。"""
    monkeypatch.setattr(preferences, "load", lambda: {"data_source_job_timeout_s": 3600})
    store = JobStore(store_dir=tmp_path / "jobs")

    jid1, new1 = store.create()
    assert new1 is True
    assert store.get(jid1)["timeout_s"] == 3600

    # 尚未 start(), job 仍是 pending —— 旧实现会在此另起新 job(并发双跑根因)
    jid2, new2 = store.create()
    assert jid2 == jid1
    assert new2 is False

    # start() 后仍复用同一活跃 job
    store.start(jid1)
    jid3, new3 = store.create()
    assert jid3 == jid1
    assert new3 is False


def test_create_new_after_terminal(monkeypatch, tmp_path):
    """job 终态(succeed/fail)后, create() 应给出新 job。"""
    monkeypatch.setattr(preferences, "load", lambda: {"data_source_long_job_timeout_s": 5400})
    store = JobStore(store_dir=tmp_path / "jobs")
    jid1, _ = store.create(long_running=True)
    assert store.get(jid1)["timeout_s"] == 5400
    store.start(jid1)
    store.succeed(jid1, {"ok": True})

    jid2, new2 = store.create()
    assert jid2 != jid1
    assert new2 is True


def test_run_slot_is_exclusive():
    """重任务执行槽同一时刻只允许一个持有者(防僵尸并发)。"""
    assert pipeline_jobs.try_acquire_run_slot() is True
    try:
        # 已被占用, 第二次获取失败
        assert pipeline_jobs.try_acquire_run_slot() is False
    finally:
        pipeline_jobs.release_run_slot()
    # 释放后可再次获取
    assert pipeline_jobs.try_acquire_run_slot() is True
    pipeline_jobs.release_run_slot()
    # 重复释放幂等, 不抛
    pipeline_jobs.release_run_slot()


# ── 监控 sector fail-closed ──────────────────────────────────────────────

def _base_price_rule(scope: str) -> dict:
    return {
        "id": "r_test",
        "name": "t",
        "type": "price",
        "conditions": [{"field": "close", "op": ">", "value": 10}],
        "logic": "and",
        "scope": scope,
    }


def test_validate_rejects_sector_scope():
    with pytest.raises(ValueError):
        monitor_rules.validate(_base_price_rule("sector"))


def test_validate_accepts_symbols_scope():
    rule = _base_price_rule("symbols")
    rule["symbols"] = ["600000.SH"]
    monitor_rules.validate(rule)  # 不应抛


def test_apply_scope_sector_fails_closed():
    """历史遗留 sector 规则在评估时应返回空(绝不退化为全市场)。"""
    df = pl.DataFrame({"symbol": ["600000.SH", "000001.SZ"], "close": [10.0, 20.0]})
    out = MonitorRuleEngine._apply_scope(df, {"id": "r_old", "scope": "sector"})
    assert out.is_empty()

    # 对照: scope=all 返回全量, symbols 过滤子集
    assert MonitorRuleEngine._apply_scope(df, {"scope": "all"}).height == 2
    picked = MonitorRuleEngine._apply_scope(
        df, {"scope": "symbols", "symbols": ["600000.SH"]}
    )
    assert picked.height == 1


def test_ladder_webhook_uses_chinese_title_without_brand(monkeypatch):
    calls = []

    class CaptureExecutor:
        def submit(self, fn, *args):
            calls.append((fn, args))

    monkeypatch.setattr(quote_service, "_WEBHOOK_EXECUTOR", CaptureExecutor())
    monkeypatch.setattr("app.services.preferences.get_feishu_webhook_url", lambda: "https://open.feishu.cn/open-apis/bot/v2/hook/test")
    monkeypatch.setattr("app.services.preferences.get_feishu_webhook_secret", lambda: "secret")
    monkeypatch.setattr("app.services.preferences.get_wecom_webhook_url", lambda: "wecom-key")

    engine = type("Engine", (), {
        "rules": {"r_ladder": {"webhook_channels": ["feishu", "wecom"]}},
    })()
    QuoteService._maybe_send_webhook(
        object.__new__(QuoteService),
        [{
            "rule_id": "r_ladder",
            "source": "ladder",
            "symbol": "600000.SH",
            "name": "浦发银行",
            "message": "炸板预警",
        }],
        engine,
    )

    titles = {fn: args[1] for fn, args in calls}
    assert titles[webhook_adapter.send_feishu_card] == "监控通知"
    assert titles[webhook_adapter.send_wecom] == "连板梯队"
    assert all("TickFlow" not in args[1] for _, args in calls)


def test_feishu_webhook_aggregates_alerts_by_symbol(monkeypatch):
    calls = []

    class CaptureExecutor:
        def submit(self, fn, *args):
            calls.append((fn, args))

    monkeypatch.setattr(quote_service, "_WEBHOOK_EXECUTOR", CaptureExecutor())
    monkeypatch.setattr(
        "app.services.preferences.get_feishu_webhook_url",
        lambda: "https://open.feishu.cn/open-apis/bot/v2/hook/test",
    )
    monkeypatch.setattr("app.services.preferences.get_feishu_webhook_secret", lambda: "secret")
    monkeypatch.setattr("app.services.preferences.get_wecom_webhook_url", lambda: "")

    engine = type("Engine", (), {
        "rules": {
            "macd": {"webhook_channels": ["feishu"]},
            "ma5": {"webhook_channels": ["feishu"]},
            "ma5_duplicate": {"webhook_channels": ["feishu"]},
            "other": {"webhook_channels": ["feishu"]},
            "wecom_only": {"webhook_channels": ["wecom"]},
        },
    })()
    QuoteService._maybe_send_webhook(
        object.__new__(QuoteService),
        [
            {
                "rule_id": "macd", "source": "signal", "symbol": "600000.SH",
                "name": "浦发银行", "price": 10.5, "message": "MACD 金叉",
            },
            {
                "rule_id": "ma5", "source": "signal", "symbol": "600000.SH",
                "name": "浦发银行", "price": 10.5, "message": "当前价>MA5",
            },
            {
                "rule_id": "ma5_duplicate", "source": "signal", "symbol": "600000.SH",
                "name": "浦发银行", "price": 10.5, "message": "当前价>MA5",
            },
            {
                "rule_id": "other", "source": "signal", "symbol": "600519.SH",
                "name": "贵州茅台", "price": 1600, "message": "突破 MA20",
            },
            {
                "rule_id": "wecom_only", "source": "signal", "symbol": "000001.SZ",
                "name": "平安银行", "price": 12.3, "message": "仅企业微信",
            },
        ],
        engine,
    )

    feishu_calls = [args for fn, args in calls if fn is webhook_adapter.send_feishu_card]
    assert len(feishu_calls) == 1
    assert feishu_calls[0] == (
        "https://open.feishu.cn/open-apis/bot/v2/hook/test",
        "监控通知",
        "",
        "浦发银行 600000.SH\n"
        "• 价格\uff1a10.50 元\n"
        "• 信号\uff1aMACD 金叉\n"
        "• 信号\uff1a当前价>MA5\n\n"
        "贵州茅台 600519.SH\n"
        "• 价格\uff1a1600.00 元\n"
        "• 信号\uff1a突破 MA20",
        "secret",
    )


def test_feishu_webhook_formats_index_price_as_points(monkeypatch):
    calls = []

    class CaptureExecutor:
        def submit(self, fn, *args):
            calls.append((fn, args))

    monkeypatch.setattr(quote_service, "_WEBHOOK_EXECUTOR", CaptureExecutor())
    monkeypatch.setattr(
        "app.services.preferences.get_feishu_webhook_url",
        lambda: "https://open.feishu.cn/open-apis/bot/v2/hook/test",
    )
    monkeypatch.setattr("app.services.preferences.get_feishu_webhook_secret", lambda: "")
    monkeypatch.setattr("app.services.preferences.get_wecom_webhook_url", lambda: "")

    engine = type("Engine", (), {
        "rules": {"index": {"asset_type": "index", "webhook_channels": ["feishu"]}},
    })()
    QuoteService._maybe_send_webhook(
        object.__new__(QuoteService),
        [{
            "rule_id": "index", "source": "signal", "symbol": "000001.SH",
            "name": "上证指数", "price": 3421.25, "message": "突破前高",
        }],
        engine,
    )

    feishu_calls = [args for fn, args in calls if fn is webhook_adapter.send_feishu_card]
    assert feishu_calls[0][3] == (
        "上证指数 000001.SH\n"
        "• 价格\uff1a3421.25 点\n"
        "• 信号\uff1a突破前高"
    )


def test_feishu_card_limits_complete_payload_by_utf8_bytes(monkeypatch):
    captured = {}

    def capture_post(webhook_url, payload, secret):
        captured.update(url=webhook_url, payload=payload, secret=secret)
        return True

    monkeypatch.setattr(webhook_adapter, "_post_feishu", capture_post)

    assert webhook_adapter.send_feishu_card(
        "https://open.feishu.cn/open-apis/bot/v2/hook/test",
        "监控通知",
        "",
        "监控信号\n" * 10_000,
        "secret",
    ) is True

    encoded = json.dumps(
        captured["payload"],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert len(encoded) <= 30_000
    assert captured["payload"]["card"]["elements"][-1]["text"]["content"].endswith("…")


def test_review_webhooks_use_title_without_brand(monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.preferences.get_review_push_channels", lambda: ["feishu", "wecom"])
    monkeypatch.setattr("app.services.preferences.get_feishu_webhook_url", lambda: "feishu-url")
    monkeypatch.setattr("app.services.preferences.get_feishu_webhook_secret", lambda: "secret")
    monkeypatch.setattr("app.services.preferences.get_wecom_webhook_url", lambda: "wecom-url")
    monkeypatch.setattr(
        "app.services.webhook_adapter.send_feishu_card",
        lambda *args: calls.append(("feishu", args)) or True,
    )
    monkeypatch.setattr(
        "app.services.webhook_adapter.send_wecom_markdown",
        lambda *args: calls.append(("wecom", args)) or True,
    )

    daily_pipeline._maybe_push_review("复盘正文", {"as_of": "2026-07-18"})

    assert [args[1] for _, args in calls] == ["每日复盘", "每日复盘"]
    assert all("TickFlow" not in args[1] for _, args in calls)
