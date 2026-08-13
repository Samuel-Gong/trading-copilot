"""每日复盘使用的可归档、可按时间截断的新闻证据源。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

import httpx

from app.config import settings

_TIMEZONE = ZoneInfo("Asia/Shanghai")
_MAX_ARCHIVE_ITEMS = 2000
_LOCK = threading.RLock()
_SOURCE_NAMES = {
    "cls-hot": "NewsNow 财联社热门",
    "xueqiu-hotstock": "NewsNow 雪球热门股票",
    "wallstreetcn-quick": "NewsNow 华尔街见闻快讯",
    "jin10": "NewsNow 金十数据",
    "gelonghui": "NewsNow 格隆汇事件",
}


def _path(data_dir: Path) -> Path:
    path = data_dir / "user_data" / "review_news.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _now() -> datetime:
    return datetime.now(_TIMEZONE)


def _parse_datetime(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        raw = float(value)
        if raw > 10_000_000_000:
            raw /= 1000
        try:
            return datetime.fromtimestamp(raw, UTC).astimezone(_TIMEZONE)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_TIMEZONE)
    return parsed.astimezone(_TIMEZONE)


def _clean_text(value, max_length: int) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(value or ""))).strip()[
        :max_length
    ]


def _normalize_item(raw: dict, fetched_at: datetime | None = None) -> dict | None:
    title = _clean_text(raw.get("title"), 300)
    summary = _clean_text(raw.get("summary") or raw.get("snippet"), 2000)
    url = str(raw.get("url") or "").strip()
    source = _clean_text(raw.get("source"), 120)
    published = _parse_datetime(raw.get("published_at") or raw.get("published_date"))
    if not title and not url:
        return None
    if not url:
        digest = hashlib.sha256(
            f"{source}|{title}|{published.isoformat() if published else ''}".encode()
        ).hexdigest()[:24]
        url = f"no-url:review-news:{digest}"
    identity = hashlib.sha256(url.encode()).hexdigest()[:24]
    fetched = fetched_at or _now()
    return {
        "id": identity,
        "title": title or url,
        "summary": summary,
        "snippet": summary,
        "url": url,
        "source": source,
        "published_at": published.isoformat(timespec="seconds") if published else None,
        "fetched_at": fetched.isoformat(timespec="seconds"),
    }


def _read_items(data_dir: Path) -> list[dict]:
    path = _path(data_dir)
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def store_items(data_dir: Path, items: list[dict]) -> int:
    """幂等合并新闻项;未知发布时间保留在归档,但永不进入历史上下文。"""
    with _LOCK:
        existing = {
            str(
                item.get("id")
                or hashlib.sha256(str(item.get("url")).encode()).hexdigest()[:24]
            ): item
            for item in _read_items(data_dir)
            if isinstance(item, dict)
        }
        saved = 0
        fetched_at = _now()
        for raw in items:
            normalized = _normalize_item(raw, fetched_at)
            if normalized is None:
                continue
            existing[normalized["id"]] = normalized
            saved += 1
        merged = sorted(
            existing.values(),
            key=lambda item: (
                str(item.get("published_at") or ""),
                str(item.get("fetched_at") or ""),
            ),
            reverse=True,
        )[:_MAX_ARCHIVE_ITEMS]
        path = _path(data_dir)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return saved


def _cutoff(as_of: date) -> datetime:
    return datetime.combine(as_of, time(23, 59, 59, 999999), tzinfo=_TIMEZONE)


def query_items(
    data_dir: Path,
    *,
    as_of: date,
    lookback_days: int = 7,
    symbol: str | None = None,
    name: str | None = None,
    limit: int = 40,
) -> list[dict]:
    cutoff = _cutoff(as_of)
    earliest = cutoff - timedelta(days=max(1, lookback_days))
    needles = []
    if symbol:
        needles.extend([symbol.upper(), symbol.split(".", 1)[0]])
    if name:
        needles.append(name.strip())
    output = []
    for raw in _read_items(data_dir):
        published = _parse_datetime(raw.get("published_at"))
        if published is None or not earliest <= published <= cutoff:
            continue
        haystack = f"{raw.get('title', '')} {raw.get('summary', '')}".upper()
        if needles and not any(needle and needle.upper() in haystack for needle in needles):
            continue
        item = dict(raw)
        item["published_date"] = published.date().isoformat()
        output.append(item)
    output.sort(key=lambda item: str(item.get("published_at")), reverse=True)
    return output[: max(1, limit)]


def _parse_newsnow_payload(payload, source_name: str) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("NewsNow 返回格式无效")
    output = []
    for raw in payload["items"][: settings.review_news_max_items_per_source]:
        if not isinstance(raw, dict):
            continue
        extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
        item = _normalize_item(
            {
                "title": raw.get("title"),
                "summary": extra.get("info") or extra.get("hover"),
                "url": raw.get("url") or raw.get("mobileUrl"),
                "source": source_name,
                "published_at": raw.get("pubDate") or extra.get("date"),
            }
        )
        if item is not None:
            output.append(item)
    return output


async def fetch_newsnow_items() -> tuple[list[dict], list[str]]:
    """并发抓取参考实现中的五个默认 NewsNow 源,单源失败不影响其他源。"""
    if not settings.review_news_enabled:
        return [], []
    base_url = settings.newsnow_base_url.strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return [], ["NewsNow: base URL 配置无效"]
    source_ids = [
        item.strip()
        for item in settings.review_news_source_ids.split(",")
        if item.strip()
    ]
    timeout = max(1.0, min(float(settings.review_news_timeout_seconds), 30.0))
    headers = {"Accept": "application/json", "User-Agent": settings.ai_user_agent}

    async with httpx.AsyncClient(timeout=timeout, headers=headers, trust_env=False) as client:
        async def fetch_one(source_id: str):
            name = _SOURCE_NAMES.get(source_id, f"NewsNow {source_id}")
            try:
                response = await client.get(f"{base_url}/api/s?{urlencode({'id': source_id})}")
                response.raise_for_status()
                return _parse_newsnow_payload(response.json(), name), None
            except Exception as exc:  # 单源必须 fail-open
                return [], f"{name}: {str(exc)[:180] or exc.__class__.__name__}"

        results = await asyncio.gather(*(fetch_one(source_id) for source_id in source_ids))
    items = [item for batch, _ in results for item in batch]
    errors = [error for _, error in results if error]
    return items, errors


async def collect_review_news(data_dir: Path, as_of: date) -> dict:
    fetched, errors = await fetch_newsnow_items()
    if fetched:
        store_items(data_dir, fetched)
    items = query_items(data_dir, as_of=as_of)
    return {
        "status": "completed",
        "source_status": (
            "skipped"
            if not settings.review_news_enabled
            else "degraded" if errors else "completed"
        ),
        "as_of": as_of.isoformat(),
        "cutoff_at": _cutoff(as_of).isoformat(timespec="seconds"),
        "unknown_timestamp_policy": "excluded",
        "items": items,
        "item_count": len(items),
        "errors": errors,
        "updated_at": _now().isoformat(timespec="seconds"),
    }
