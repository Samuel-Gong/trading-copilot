"""每日复盘新闻证据的时间边界测试。"""
from __future__ import annotations

import asyncio
from datetime import date

from app.services import review_news


def test_historical_query_excludes_future_and_unknown_timestamp_items(tmp_path):
    review_news.store_items(
        tmp_path,
        [
            {
                "title": "截止日前消息",
                "summary": "贵州茅台发布历史公告",
                "url": "https://example.com/past",
                "source": "测试源",
                "published_at": "2026-07-31T08:00:00+08:00",
            },
            {
                "title": "未来哨兵消息 FUTURE_SENTINEL",
                "summary": "不应进入历史上下文",
                "url": "https://example.com/future",
                "source": "测试源",
                "published_at": "2026-08-01T08:00:00+08:00",
            },
            {
                "title": "时间未知消息 UNKNOWN_SENTINEL",
                "summary": "不能证明发布时间",
                "url": "https://example.com/unknown",
                "source": "测试源",
                "published_at": None,
            },
        ],
    )

    result = review_news.query_items(tmp_path, as_of=date(2026, 7, 31), lookback_days=7)

    assert [item["title"] for item in result] == ["截止日前消息"]
    assert result[0]["published_date"] == "2026-07-31"


def test_symbol_news_is_filtered_from_same_point_in_time_archive(tmp_path):
    review_news.store_items(
        tmp_path,
        [
            {
                "title": "贵州茅台 600519 年度事项",
                "summary": "公司历史消息",
                "url": "https://example.com/maotai",
                "source": "测试源",
                "published_at": "2026-07-30T09:00:00+08:00",
            },
            {
                "title": "其他公司事项",
                "summary": "与目标无关",
                "url": "https://example.com/other",
                "source": "测试源",
                "published_at": "2026-07-30T10:00:00+08:00",
            },
        ],
    )

    result = review_news.query_items(
        tmp_path,
        as_of=date(2026, 7, 31),
        symbol="600519.SH",
        name="贵州茅台",
    )

    assert [item["url"] for item in result] == ["https://example.com/maotai"]


def test_newsnow_payload_is_persisted_and_refresh_failure_is_fail_open(tmp_path, monkeypatch):
    async def fake_fetch(*args, **kwargs):
        return [
            {
                "title": "财联社历史消息",
                "summary": "盘面记录",
                "url": "https://example.com/cls",
                "source": "NewsNow 财联社热门",
                "published_at": "2026-07-31T10:00:00+08:00",
            }
        ], ["NewsNow 雪球热门股票: timeout"]

    monkeypatch.setattr(review_news, "fetch_newsnow_items", fake_fetch)
    context = asyncio.run(review_news.collect_review_news(tmp_path, date(2026, 7, 31)))

    assert context["status"] == "completed"
    assert context["source_status"] == "degraded"
    assert context["cutoff_at"].startswith("2026-07-31T23:59:59")
    assert [item["title"] for item in context["items"]] == ["财联社历史消息"]
    assert context["errors"] == ["NewsNow 雪球热门股票: timeout"]
