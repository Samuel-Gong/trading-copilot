"""AI 大盘复盘的历史日 point-in-time 边界。"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from types import SimpleNamespace

from app.services import ai_provider, auction_benchmark, dragon_tiger, market_recap


def test_recap_context_uses_overview_as_of(tmp_path, monkeypatch):
    overview = {
        "as_of": "2026-08-27",
        "indices": [],
        "emotion": {"score": 50, "label": "中性"},
        "limit": {},
        "amount": {},
    }
    captured: dict[str, date] = {}
    monkeypatch.setattr(market_recap, "build_market_overview", lambda *_args: overview)
    monkeypatch.setattr(
        dragon_tiger,
        "build_recap_context",
        lambda _data_dir, target: captured.setdefault("dragon_tiger", target) and "",
    )
    monkeypatch.setattr(
        auction_benchmark,
        "build_recap_context",
        lambda _data_dir, target: captured.setdefault("auction_benchmark", target) and "",
    )

    async def _stream(*_args, **_kwargs):
        yield "已完成"

    monkeypatch.setattr(ai_provider, "stream_ai_text", _stream)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    async def _collect() -> list[dict]:
        return [json.loads(item) async for item in market_recap.recap_market_stream(repo)]

    events = asyncio.run(_collect())

    assert captured == {
        "dragon_tiger": date(2026, 8, 27),
        "auction_benchmark": date(2026, 8, 27),
    }
    assert events[-1]["type"] == "done"
